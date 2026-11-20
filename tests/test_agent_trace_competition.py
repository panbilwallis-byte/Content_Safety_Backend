import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from app.services.agent_trace_competition import analyze
from test_agent_traces import env, ROOT


def test_steps_unicode_shared_reasoning_and_reply_scoped_ids():
    record = {"trajectory_id": "test", "messages": [
        {"role": "user", "content": "data"},
        {"role": "assistant", "reasoning_content": "😀\n a", "tool_calls": [
            {"id": "a", "function": {"name": "Read", "arguments": "{}"}},
            {"id": "b", "function": {"name": "read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "a", "content": "first"},
        {"role": "assistant", "reasoning_content": "thinking only"},
        {"role": "tool", "tool_call_id": "b", "content": "late"},
        {"role": "assistant", "tool_calls": [{"id": "a", "function": {"name": "Read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "a", "content": "second"},
        {"role": "assistant", "content": "done"},
        {"role": "assistant", "content": ""},
    ], "usage": {"input_tokens": 0}, "metadata": {"record": {"is_session_completed": None}, "request": {"is_session_completed": True}}}
    r = analyze(record)["results"]
    assert r["A1"] ["input_tokens"] == 0
    assert r["A1"]["output_tokens"] == "未提供"
    assert r["A2"]["known_characters"] == len("😀\n athinking only")
    assert r["A2"]["absent_body_step_ids"] == [4, 5]
    assert r["B1"]["segments"][0]["step_ids"] == [1, 2]
    assert r["C1"]["calls"][2]["returns"][0]["content"] == "second"
    assert len(r["C1"]["unmatched"]) == 2
    assert r["A3"]["tool_kinds"] == 2
    assert r["C4"]["completion"] is None


def test_no_inferred_failure_from_source_code_or_probe_and_no_ellipsis_truncation():
    messages = []
    for i, output in enumerate(['return Error("failed");', 'NO NET', '... 30 more lines', '{"exit_code":1,"output":"target crashed"}']):
        messages.extend([{"role": "assistant", "tool_calls": [{"id": str(i), "function": {"name": "bash", "arguments": "{}"}}]},
                         {"role": "tool", "tool_call_id": str(i), "content": output}])
    r = analyze({"trajectory_id": "test", "messages": messages})["results"]
    assert r["C3"]["failures"] == []
    assert r["C4"]["truncations"] == []


def test_duplicate_ids_in_same_reply_are_not_guessed():
    c = {"id": "x", "function": {"name": "read", "arguments": "{}"}}
    r = analyze({"trajectory_id": "duplicate", "messages": [
        {"role": "assistant", "tool_calls": [c, c]}, {"role": "tool", "tool_call_id": "x", "content": "ambiguous"}]})["results"]
    assert all(not c["returns"] for c in r["C1"]["calls"])
    assert len(r["C1"]["unmatched"]) == 3


def import_and_check(env, path, expected_ids):
    from app.services import agent_trace_ingest as ingest
    from app.models.agent_traces import TraceSample, TraceCall, TraceEvent
    from app.models.tables import Task
    from sqlalchemy import select, func
    raw = path.read_bytes()
    user = {"uid": 1, "tier": "admin"}
    with env["sessions"]() as db:
        b = ingest.create_batch(db, user, "赛题解析测试", uuid4().hex)
        file_id = uuid4().hex
        ingest.register_asset(db, user, file_id, path.name, path, len(raw), hashlib.sha256(raw).hexdigest(), uuid4().hex)
        bound = ingest.bind(db, user, b["batch_id"], {"expected_version": 1, "files": [{"file_id": file_id}]}, uuid4().hex)
        task, _ = ingest.submit(db, {"batch_id": b["batch_id"], "expected_version": bound["batch"]["batch_version"]}, user, uuid4().hex, "parse")
    ingest.run(task["task_id"], env["sessions"])
    with env["sessions"]() as db:
        task_row = db.get(Task, task["task_id"])
        assert task_row.status == "succeeded", task_row.result
        sid = task_row.result["snapshot_id"]
        persisted = list(db.scalars(select(TraceSample).where(TraceSample.snapshot_id == sid).order_by(TraceSample.ingest_order)))
        assert [s.sample_id for s in persisted] == expected_ids
        assert all(s.payload["competition_report"] for s in persisted)
        for sample in persisted:
            results = sample.payload["competition_report"]["results"]
            assert results["B2"]["judgment"] == ("有风险" if sample.payload["audit_decision"] == "risk_found" else "无风险")
            assert results["B3"]["first_risk_step"] == sample.payload["first_risk_step"]
            assert sorted({r["step_id"] for r in results["B3"]["risks"] if r["step_id"] is not None}) == sample.payload["risk_steps"]
            events = {e.event_id: e.payload for e in db.scalars(select(TraceEvent).where(
                TraceEvent.snapshot_id == sid, TraceEvent.trace_id == sample.trace_id))}
            for finding in sample.payload["rule_findings"]:
                if finding["category"] == "security" and finding.get("competition_step"):
                    assert finding["competition_step"] in events[finding["event_id"]]["source_step_ids"]
        assert db.scalar(select(func.count()).select_from(TraceEvent).where(TraceEvent.snapshot_id == sid)) > 0
        call_count = db.scalar(select(func.count()).select_from(TraceCall).where(TraceCall.snapshot_id == sid))
    url = f"{ROOT}/batches/{b['batch_id']}/competition-report"
    response = env["client"].get(url, params={"snapshotId": sid})
    assert response.status_code == 200, response.text
    rows = [json.loads(line) for line in response.json()["data"]["jsonl"].splitlines()]
    assert [r["trajectory_id"] for r in rows] == expected_ids + ["aggregate"]
    assert rows[-1]["results"]["A4"]["tool_calls"] == call_count
    assert env["client"].get(url, params={"snapshotId": "nonexistent"}).status_code == 410
    from app.core.auth import create_token
    assert env["client"].get(url, headers={"Authorization": "Bearer " + create_token(3, "user-3", "审计员")}).status_code == 403
    return rows


def test_persist_and_read_via_existing_frontend_contract(env):
    path = env["tmp"] / "sample.jsonl"
    path.write_text(json.dumps({"trajectory_id": "demo", "messages": [
        {"role": "assistant", "reasoning_content": "思考", "content": "完成"}]}), encoding="utf-8")
    import_and_check(env, path, ["demo"])


def test_supplied_samples_end_to_end(env):
    import os
    path = os.getenv("SCORING_SAMPLES_PATH")
    if not path:
        pytest.skip("Set SCORING_SAMPLES_PATH to test the supplied external JSONL")
    rows = import_and_check(env, Path(path), ["s01", "p14", "s04", "science_agent_bench_66", "science_agent_bench_48"])
    assert [r["results"]["A3"]["tool_calls"] for r in rows[:-1]] == [6, 42, 21, 5, 24]
    assert [r["results"]["B3"]["risks"][0]["step_id"] if r["results"]["B3"]["risks"] else None for r in rows[:-1]] == [2, 5, 6, None, 6]
    assert [f["step_id"] for f in rows[4]["results"]["C3"]["failures"]] == [5, 11]
    assert [t["step_id"] for t in rows[4]["results"]["C4"]["truncations"]] == [16, 18]
    assert rows[1]["results"]["C4"]["truncations"] == []
