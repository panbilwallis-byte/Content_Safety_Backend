"""Current frontend report contract, using isolated real import snapshots."""
from copy import deepcopy
import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from test_agent_traces import env, ROOT
from test_agent_trace_workflow import upload, USER
from app.core.auth import create_token
from app.models.agent_traces import TraceBatch, TraceSample, TraceEvent, TraceCall, TraceEvidence, TraceSnapshot, TraceProjection
from app.services import agent_trace_competition as competition, agent_trace_query as q, agent_trace_ingest as ingest
from app.services.agent_trace_trajectory import EXAMPLE


MESSAGES = {"messages": [
    {"role": "user", "content": "查询报告"},
    {"role": "assistant", "reasoning": "相同推理😀", "content": "查询两个来源", "tool_calls": [
        {"id": "a", "function": {"name": "query_database", "arguments": '{"q":"a"}'}},
        {"id": "b", "function": {"name": "fetch_page", "arguments": '{"q":"b"}'}}]},
    {"role": "tool", "tool_call_id": "a", "content": "数据库结果"},
    {"role": "tool", "tool_call_id": "b", "content": "网页结果"},
    {"role": "assistant", "content": "完成"},
], "usage": {"prompt_tokens": 0, "completion_tokens": 12}}
LANGCHAIN = {"runs": [
    {"id": "root", "run_type": "chain", "inputs": {"question": "report"}, "outputs": {"answer": "done"}},
    {"id": "tool", "parent_run_id": "root", "run_type": "tool", "name": "fetch_page",
     "inputs": {"url": "https://example.com"}, "outputs": {"answer": "complete result"}},
]}


def fetch(env, bid, **params):
    return env["client"].get(f"{ROOT}/batches/{bid}/competition-report", params=params)


def rows(response):
    assert response.status_code == 200, response.text
    return [json.loads(line) for line in response.json()["data"]["jsonl"].splitlines()]


@pytest.mark.parametrize("record,expected_calls", [(MESSAGES, 2), (LANGCHAIN, 1), (EXAMPLE, 1)])
def test_all_ordinary_formats_render_complete_frontend_sections(env, tmp_path, monkeypatch, record, expected_calls):
    monkeypatch.delenv("AGENT_TRACE_ENABLE_LEGACY_VALUE_SYNC", raising=False)
    bid, _ = upload(env, tmp_path, [record])
    result, aggregate = rows(fetch(env, bid))
    assert result["report_kind"] == "ordinary"
    assert set(result["results"]) == {"A1", "A2", "A3", "B1", "B2", "B3", "C1", "C2", "C3", "C4", "C5"}
    r = result["results"]
    assert r["A3"]["tool_calls"] == expected_calls
    assert aggregate["results"]["A4"]["tool_calls"] == expected_calls
    assert len(r["C1"]["calls"]) == expected_calls
    assert all(call["returns"] for call in r["C1"]["calls"])
    assert r["B2"]["judgment"] == "无风险"
    assert r["B3"]["risks"] == []
    assert isinstance(r["C4"]["missing_fields"], list)
    if record is MESSAGES:
        assert r["A1"] == {"指标": competition.NAMES["A1"], "input_tokens": 0, "output_tokens": 12}
        assert r["A2"]["known_characters"] == len("相同推理😀")
        assert len(r["B1"]["segments"]) == 1
        assert r["B1"]["segments"][0]["step_ids"] == [2, 3, 4]
        assert [call["step_id"] for call in r["C1"]["calls"]] == [3, 4]
        assert r["C1"]["calls"][0]["returns"][0]["content"] == "数据库结果"
    if record is LANGCHAIN:
        assert r["A1"]["input_tokens"] == "未提供"
        assert r["B1"]["segments"] == []
        assert "message_index" not in r["C1"]["calls"][0]
    if record is EXAMPLE:
        assert result["trajectory_id"] == EXAMPLE["sample_id"]
        assert r["C4"]["completion"] is True
        assert r["B1"]["segments"][0]["text"] == EXAMPLE["events"][1]["reasoning"]


def test_mixed_report_is_snapshot_scoped_and_read_only(env, tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_TRACE_ENABLE_LEGACY_VALUE_SYNC", raising=False)
    scoring = {"trajectory_id": "scored", "messages": [{"role": "assistant", "content": "done"}]}
    bid, _ = upload(env, tmp_path, [MESSAGES, LANGCHAIN, EXAMPLE, scoring])
    with env["sessions"]() as db:
        _, scope = q.resolve(db, bid, {})
        models = (TraceSample, TraceEvent, TraceCall, TraceEvidence, TraceSnapshot, TraceProjection)
        saved = {model: [deepcopy(row.__dict__) for row in db.scalars(select(model))] for model in models}
        for values in saved.values():
            for value in values:
                value.pop("_sa_instance_state", None)
        original_scored = db.scalar(select(TraceSample).where(TraceSample.snapshot_id == scope["snapshot_id"], TraceSample.sample_id == "scored")).payload["competition_report"]
    original_analyze = competition.analyze
    monkeypatch.setattr(competition, "analyze", lambda *_: pytest.fail("GET must not rerun audit"))
    response = fetch(env, bid, snapshot_id=scope["snapshot_id"], page=2, trace_id="ignored-filter")
    result = rows(response)
    assert len(result) == 5
    assert result[3] == original_scored
    assert result[-1]["results"]["A4"]["tool_calls"] == 4
    summary = env["client"].get(ROOT + "/workspace", params={"view": "execution", "include": "dashboard", **scope}).json()["data"]["tool_call_summary"]
    assert result[-1]["results"]["A4"]["tool_calls"] == summary["total_call_count"]
    assert result[-1]["results"]["A4"]["tool_counts"] == {i["tool_name"]: i["call_count"] for i in summary["items"]}
    with env["sessions"]() as db:
        for model in models:
            current = [{k: deepcopy(v) for k, v in row.__dict__.items() if k != "_sa_instance_state"} for row in db.scalars(select(model))]
            assert current == saved[model]
        batch = db.get(TraceBatch, bid)
        task, _ = ingest.submit(db, {"batch_id": bid, "expected_version": batch.payload["batch_version"], "mode": "reparse_all"}, USER, uuid4().hex, "reparse")
    # Allow normal parsing again, but the old report must remain unchanged.
    monkeypatch.setattr(competition, "analyze", original_analyze)
    ingest.run(task["task_id"], env["sessions"])
    assert fetch(env, bid, snapshot_id=scope["snapshot_id"]).json()["data"] == response.json()["data"]


def test_duplicate_ordinary_ids_and_competition_ids_do_not_collide(env, tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_TRACE_ENABLE_LEGACY_VALUE_SYNC", raising=False)
    records = [deepcopy(EXAMPLE), deepcopy(EXAMPLE), deepcopy(EXAMPLE),
               {"trajectory_id": "scored", "messages": [{"role": "assistant", "content": "done"}]}]
    records[2]["sample_id"] = "scored"
    bid, _ = upload(env, tmp_path, records)
    result = rows(fetch(env, bid))
    identities = [r["trajectory_id"] for r in result]
    assert len(set(identities)) == 5
    assert all(identity.startswith("trc_") for identity in identities[:3])
    assert identities[3:] == ["scored", "aggregate"]
    assert rows(fetch(env, bid)) == result


def test_tool_failures_retries_unmatched_and_unknown_names_are_not_security(env, tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_TRACE_ENABLE_LEGACY_VALUE_SYNC", raising=False)
    record = deepcopy(EXAMPLE)
    record["events"][3].update(tool_status="error", retry=True, duplicate=True)
    record["tool_events"][0].update(status="error", error_code="TIMEOUT")
    unpaired = {"messages": [{"role": "assistant", "tool_calls": [{"id": "unpaired", "function": {"arguments": "{}"}}]}]}
    bid, _ = upload(env, tmp_path, [record, unpaired])
    first, second, aggregate = rows(fetch(env, bid))
    r = first["results"]
    assert r["B2"]["judgment"] == "无风险" and not r["B3"]["risks"]
    assert r["C2"]["markers"] == []
    assert r["C3"]["failures"]
    assert "超时" in r["C3"]["failures"][0]["error_type"]
    assert r["C5"]["issues"][0]["evidence"]
    assert second["results"]["A3"]["tool_counts"] == {"unknown": 1}
    assert second["results"]["C1"]["unmatched"][0]["content"] == "{}"
    assert aggregate["results"]["A4"]["tool_calls"] == 2


def test_risk_on_return_locates_request_and_non_tool_risk_has_no_fake_call(env, tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_TRACE_ENABLE_LEGACY_VALUE_SYNC", raising=False)
    first, second = deepcopy(EXAMPLE), deepcopy(EXAMPLE)
    for record, idx in ((first, 3), (second, 1)):
        record["audit"] = {"decision": "risk_found", "risk_level": "high", "evidence": [
            {"event_idx": idx, "risk_type": "privacy_leak", "label": "敏感内容", "matched_text": "private text", "severity": "high"}]}
    bid, _ = upload(env, tmp_path, [first, second])
    results = rows(fetch(env, bid))
    for result in results[:2]:
        assert result["results"]["B2"]["judgment"] == "有风险"
        assert result["results"]["B3"]["risks"][0]["risk_quote"] == "private text"
    tool_risk = results[0]["results"]["B3"]["risks"][0]
    assert tool_risk["event_ordinal"] == 4 and tool_risk["step_id"] == 3
    assert tool_risk["step_id"] == results[0]["results"]["C1"]["calls"][0]["step_id"]
    non_tool_risk = results[1]["results"]["B3"]["risks"][0]
    assert "step_id" not in non_tool_risk
    assert non_tool_risk["field"] == "events[1]"


def test_report_permissions_empty_batch_and_pending_audit(env, tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_TRACE_ENABLE_LEGACY_VALUE_SYNC", raising=False)
    bid, _ = upload(env, tmp_path, [EXAMPLE])
    reader = create_token(3, "user-3", "审计员")
    assert env["client"].get(f"{ROOT}/batches/{bid}/competition-report", headers={"Authorization": "Bearer " + reader}).status_code == 403
    with env["sessions"]() as db:
        _, scope = q.resolve(db, bid, {})
        sample = db.scalar(select(TraceSample).where(TraceSample.snapshot_id == scope["snapshot_id"]))
        sample.payload = {**sample.payload, "audit_decision": "pending"}
        db.add(TraceSnapshot(snapshot_id="empty-report", batch_id=bid, payload={}))
        db.add(TraceProjection(projection_id="empty-report-projection", snapshot_id="empty-report", version=scope["projection_version"], payload={}))
        db.commit()
    assert rows(fetch(env, bid))[0]["results"]["B2"]["judgment"] == "未完成审计"
    assert fetch(env, bid, snapshot_id="empty-report").status_code == 422
    assert fetch(env, bid, snapshot_id="missing").status_code == 410


def test_completion_and_explicit_truncation_preserve_missing_values(env, tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_TRACE_ENABLE_LEGACY_VALUE_SYNC", raising=False)
    record = deepcopy(MESSAGES)
    record.pop("usage")
    record["metadata"] = {"record": {"is_session_completed": None, "is_truncated": True}}
    bid, _ = upload(env, tmp_path, [record])
    result = rows(fetch(env, bid))[0]["results"]
    assert result["A1"]["input_tokens"] == "未提供"
    assert result["C4"]["completion"] is None
    assert result["C4"]["truncations"][0]["field"] == "metadata.record.is_truncated"


def test_full_chain_shared_task_api_can_read_more_than_one_page(env, monkeypatch):
    from app.models.tables import Task
    from app.repositories import task_repository
    monkeypatch.setattr(task_repository, "SessionLocal", env["sessions"])
    with env["sessions"]() as db:
        db.add_all(Task(task_id=f"frontend-page-{i:03}", name=f"任务{i}", capability_code="data_ingest",
                        status="succeeded", input_data={}, config={}, result={}) for i in range(105))
        db.commit()
    result = []
    for page in (1, 2):
        response = env["client"].get("/api/v1/tasks", params={"page": page, "page_size": 100})
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["total"] == 105 and data["page_size"] == 100
        result.extend(row["task_id"] for row in data["items"])
    assert len(result) == len(set(result)) == 105
