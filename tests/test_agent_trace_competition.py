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
    messages.insert(0, {"role": "user", "content": "Reproduce the target vulnerability and trigger a crash."})
    r = analyze({"trajectory_id": "test", "messages": messages})["results"]
    assert r["C3"]["failures"] == []
    assert r["C4"]["truncations"] == []


def test_duplicate_ids_in_same_reply_are_not_guessed():
    c = {"id": "x", "function": {"name": "read", "arguments": "{}"}}
    r = analyze({"trajectory_id": "duplicate", "messages": [
        {"role": "assistant", "tool_calls": [c, c]}, {"role": "tool", "tool_call_id": "x", "content": "ambiguous"}]})["results"]
    assert all(not c["returns"] for c in r["C1"]["calls"])
    assert len(r["C1"]["unmatched"]) == 3


def action_with_result(command, output):
    return {"trajectory_id": "diagnostic", "messages": [
        {"role": "assistant", "tool_calls": [{"id": "c", "function": {"name": "bash", "arguments": json.dumps({"command": command})}}]},
        {"role": "tool", "tool_call_id": "c", "content": output},
    ]}


@pytest.mark.parametrize("command,output,error", [
    ("curl -o data.zip https://example.com/data.zip", "curl: (28) Operation timed out", "下载超时"),
    ("curl -I https://example.com", "HTTP 000\nNO NET (exit 28)", "网络请求超时"),
    ("curl --output=/dev/null https://example.com", "curl: (28) Operation timed out", "网络请求超时"),
    ("timeout 60s python analysis.py", "Command exited with code 124", "执行超时"),
    ("long-running-command", "(no output)\n<shell_metadata>\nUser aborted the command\n</shell_metadata>", "执行被用户中止"),
    ("curl https://example.com | head", "curl: (23) Failure writing output to destination", "输出写入失败"),
    ("xxd data.bin", "/usr/bin/bash: line 1: xxd: command not found", "命令不存在"),
    ("some-tool", "Tool timed out", "工具超时"),
])
def test_failures_use_specific_type_and_original_return_evidence(command, output, error):
    r = analyze(action_with_result(command, output))["results"]
    failure, = r["C3"]["failures"]
    assert failure["step_id"] == 1 and failure["error_type"] == error
    assert failure["evidence"]
    assert all(e["message_index"] == 1 and e["step_id"] == 1 and e["quote"] in output for e in failure["evidence"])
    assert r["C5"]["issues"] == [{"source_subitem": "C3", **failure}]


def test_documented_errors_and_fallback_traceback_are_not_failures():
    for output in [
        'print("User aborted the command")',
        '21: curl: (23) Failure writing output to destination',
        '```text\nCommand exited with code 124\n```',
        'Traceback (most recent call last):\nImportError: blocked for fallback test',
    ]:
        assert analyze(action_with_result("cat source.py", output))["results"]["C3"]["failures"] == []


@pytest.mark.parametrize("field", ["content", "reasoning_content", "arguments"])
def test_truncation_evidence_comes_from_the_field_with_the_marker(field):
    record = action_with_result("echo ok", "success")
    message = record["messages"][0]
    message["content"] = "ordinary answer"
    message["reasoning_content"] = "original thought"
    marker = "...[truncated 2110 chars]"
    if field == "arguments":
        message["tool_calls"][0]["function"]["arguments"] = json.dumps({"command": marker})
        original = message["tool_calls"][0]["function"]["arguments"]
    else:
        message[field] += "\n" + marker
        original = message[field]
    r = analyze(record)["results"]
    truncation, = r["C4"]["truncations"]
    issue, = r["C5"]["issues"]
    assert truncation["field"] == ("tool_calls[0].function.arguments" if field == "arguments" else field)
    assert issue["evidence"] == [{"message_index": 0, "step_id": 1, "quote": truncation["quote"]}]
    assert marker in issue["evidence"][0]["quote"] and issue["evidence"][0]["quote"] in original


def test_metadata_truncation_does_not_invent_message_evidence():
    record = action_with_result("echo ok", "success")
    record["metadata"] = {"record": {"is_truncated": True}}
    issue, = analyze(record)["results"]["C5"]["issues"]
    assert issue["field"] == "metadata.record.is_truncated" and issue["value"] is True
    assert issue["evidence"] == [] and "step_id" not in issue and "message_index" not in issue


def test_same_step_risks_merge_evidence_without_mutating_full_findings():
    from copy import deepcopy
    from app.services.agent_trace_competition import first_risk
    findings = [
        {"step_id": 3, "risk_quote": "later", "evidence": []},
        {"step_id": 1, "risk_quote": "first", "evidence": [{"step_id": 1, "quote": "a"}]},
        {"step_id": 1, "risk_quote": "same action", "evidence": [{"step_id": 1, "quote": "a"}, {"step_id": 1, "quote": "b"}]},
        {"step_id": None, "evidence": []},
    ]
    original = deepcopy(findings)
    selected, = first_risk(findings)
    assert selected["step_id"] == 1
    assert [e["quote"] for e in selected["evidence"]] == ["a", "b"]
    assert findings == original


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
            first = results["B3"]["risks"][0]["step_id"] if results["B3"]["risks"] else None
            assert first == sample.payload["first_risk_step"]
            assert [r["step_id"] for r in results["B3"]["risks"]] == sample.payload["risk_steps"][:1]
            assert sample.payload["risk_steps"] == sorted({
                f["competition_step"] for f in sample.payload["rule_findings"]
                if f["category"] == "security" and f.get("competition_step") is not None})
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
    # Existing statistics projections must expose the saved A2 without reparsing.
    params = {"snapshot_id": sid, "view": "statistics"}
    listing = env["client"].get(f"{ROOT}/batches/{b['batch_id']}/samples", params=params)
    assert listing.status_code == 200, listing.text
    expected_counts = {r["trajectory_id"]: r["results"]["A2"]["known_characters"] for r in rows[:-1]}
    assert {s["display_id"]: s["reasoning_char_count"] for s in listing.json()["data"]["items"]} == expected_counts
    workspace = env["client"].get(f"{ROOT}/statistics", params={"batch_id": b["batch_id"], "snapshot_id": sid})
    assert workspace.status_code == 200, workspace.text
    assert {s["display_id"]: s["reasoning_char_count"] for s in workspace.json()["data"]["sample_page"]["items"]} == expected_counts
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
    assert [len(r["results"]["B3"]["risks"]) for r in rows[:-1]] == [1, 1, 1, 0, 1]
    assert [(f["step_id"], f["error_type"]) for f in rows[1]["results"]["C3"]["failures"]] == [
        (21, "下载超时"), (28, "执行被用户中止"), (34, "输出写入失败"), (37, "命令不存在")]
    assert [(f["step_id"], f["error_type"]) for f in rows[4]["results"]["C3"]["failures"]] == [
        (5, "执行超时"), (11, "网络请求超时")]
    assert [t["step_id"] for t in rows[4]["results"]["C4"]["truncations"]] == [16, 18]
    assert rows[1]["results"]["C4"]["truncations"] == []
    issue = next(i for i in rows[4]["results"]["C5"]["issues"] if i["step_id"] == 18)
    assert issue["evidence"] == [{"message_index": 32, "step_id": 18, "quote": "...[truncated 2110 chars]"}]
    assert all(row["analysis_version"] == "competition-v5" for row in rows[:-1])

    reference_path = Path(os.getenv("SCORING_REFERENCE_PATH", str(Path(path).with_name("reference_answers.jsonl"))))
    if reference_path.is_file():
        references = {r["trajectory_id"]: r for line in reference_path.read_text(encoding="utf-8-sig").splitlines() if line.strip() for r in [json.loads(line)]}
        for row in rows:
            expected = references[row["trajectory_id"]]["results"]
            actual = row["results"]
            for key in expected:
                assert actual[key]["指标"] == expected[key]["指标"]
            if row["trajectory_id"] == "aggregate":
                assert actual == expected
                continue
            for key in ("A1", "A2", "A3", "B2"):
                assert actual[key] == expected[key]
            for key in ("B3", "C2", "C5"):
                assert actual[key] == expected[key]
            assert [(f["step_id"], f["error_type"]) for f in actual["C3"]["failures"]] == [
                (f["step_id"], f["error_type"]) for f in expected["C3"]["failures"]]
            assert [(i["source_subitem"], i.get("step_id"), i.get("message_index")) for i in actual["C5"]["issues"]] == [
                (i["source_subitem"], i.get("step_id"), i.get("message_index")) for i in expected["C5"]["issues"]]
            # Compare complete original bodies and calls while allowing extra coordinates.
            for field in ("absent_body_step_ids", "segments"):
                if field == "segments":
                    assert [(s["step_id"], s["text"]) for s in actual["B1"][field]] == [(s["step_id"], s["text"]) for s in expected["B1"][field]]
                else:
                    assert actual["B1"][field] == expected["B1"][field]
            assert [(c["step_id"], c["tool_name"], c["arguments"], [r["content"] for r in c["returns"]])
                    for c in actual["C1"]["calls"]] == [
                (c["step_id"], c["tool_name"], c["arguments"], [r["content"] for r in c["returns"]])
                for c in expected["C1"]["calls"]]
            assert actual["C1"]["unmatched"] == expected["C1"]["unmatched"]
            assert [(r["step_id"], r["previous_step_id"]) for r in actual["C2"]["markers"]] == [
                (r["step_id"], r["previous_step_id"]) for r in expected["C2"]["markers"]]
            for field in ("missing_fields", "unmatched", "completion"):
                assert actual["C4"][field] == expected["C4"][field]
            assert [(t.get("step_id"), t.get("message_index"), t.get("quote")) for t in actual["C4"]["truncations"]] == [
                (t.get("step_id"), t.get("message_index"), t.get("quote")) for t in expected["C4"]["truncations"]]


def test_reading_old_report_neither_reanalyzes_nor_upgrades_snapshot(env, tmp_path, monkeypatch):
    from copy import deepcopy
    from sqlalchemy import select, func
    from app.models.agent_traces import TraceBatch, TraceSample, TraceSnapshot
    from app.services import agent_trace_competition as competition, agent_trace_rules as rules
    from test_agent_trace_workflow import upload

    record = action_with_result("echo ok", "success")
    bid, _ = upload(env, tmp_path, [record])
    with env["sessions"]() as db:
        batch = db.get(TraceBatch, bid)
        sid = batch.active_snapshot_id
        sample = db.scalar(select(TraceSample).where(TraceSample.snapshot_id == sid))
        previous = deepcopy(sample.payload)
        previous["competition_report"]["analysis_version"] = "competition-v2"
        previous["competition_report"]["results"]["A3"]["指标"] = "数据统计指标可视化③：工具调用次数和种类"
        sample.payload = previous
        db.commit()
        count = db.scalar(select(func.count()).select_from(TraceSnapshot))
        # The report-only version change must not trigger the background upgrader.
        assert rules.VERSION == "trace-rules-v3"
        assert rules.upgrade_one(db) == 0

    def no_reanalysis(*args, **kwargs):
        pytest.fail("Reading a stored report must not run analysis")
    monkeypatch.setattr(competition, "analyze", no_reanalysis)
    response = env["client"].get(f"{ROOT}/batches/{bid}/competition-report", params={"snapshotId": sid})
    assert response.status_code == 200, response.text
    row = json.loads(response.json()["data"]["jsonl"].splitlines()[0])
    assert row == previous["competition_report"]
    with env["sessions"]() as db:
        assert db.get(TraceBatch, bid).active_snapshot_id == sid
        assert db.scalar(select(func.count()).select_from(TraceSnapshot)) == count
        assert db.scalar(select(TraceSample).where(TraceSample.snapshot_id == sid)).payload == previous
