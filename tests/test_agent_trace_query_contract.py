"""Read-only query contracts for batch tool counts and audit availability."""
from collections import Counter
from copy import deepcopy

import pytest
from sqlalchemy import select

from test_agent_traces import env, ROOT
from app.models.agent_traces import TraceBatch, TraceCall, TraceProjection, TraceSample, TraceSnapshot
from app.services import agent_trace_query as q, agent_trace_statistics as stats
from app.services import agent_trace_sync as sync


def workspace(env, **params):
    response = env["client"].get(ROOT + "/workspace", params={
        "view": "execution", "include": "dashboard", "batch_id": env["batch"], **params})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_batch_tool_counts_ignore_sample_selection_filters_and_pagination(env):
    with env["sessions"]() as db:
        calls = list(db.scalars(select(TraceCall).where(
            TraceCall.snapshot_id == env["scope"]["snapshot_id"])))
        expected = Counter(c.payload["tool_name"] for c in calls)
        assert {c.payload["execution_status"] for c in calls} >= {"succeeded", "failed", "not_executed"}
    summary = workspace(env)["tool_call_summary"]
    assert summary == {"total_call_count": 56, "items": sorted(
        [{"tool_name": name, "call_count": count} for name, count in expected.items()],
        key=lambda item: (-item["call_count"], item["tool_name"]))}
    assert workspace(env, page=2)["tool_call_summary"] == summary
    selected = workspace(env, trace_id=env["rows"][-1].trace_id)
    assert selected["sample_page"]["page"] == 2
    assert selected["tool_call_summary"] == summary
    filtered = workspace(env, q="AUD-0008", language="en")
    assert filtered["sample_page"]["total"] == 1
    assert filtered["tool_call_summary"] == summary


def test_tool_counts_respect_batch_and_historical_snapshot(env):
    with env["sessions"]() as db:
        other = sync.ingest(db, {"system": "business", "task_id": "other", "result_id": "other"},
                            [{"id": "other", "score": 10}])
        db.add(TraceSnapshot(snapshot_id="new-empty", batch_id=env["batch"], payload={}))
        db.add(TraceProjection(projection_id="new-empty-projection", snapshot_id="new-empty",
                               version=env["scope"]["projection_version"], payload={}))
        db.get(TraceBatch, env["batch"]).active_snapshot_id = "new-empty"
        db.commit()
    assert workspace(env)["tool_call_summary"] == {"total_call_count": 0, "items": []}
    assert workspace(env, snapshot_id=env["scope"]["snapshot_id"])["tool_call_summary"]["total_call_count"] == 56
    assert workspace(env, batch_id=other)["tool_call_summary"]["total_call_count"] == 7


def test_zero_calls_and_unpublished_batch_are_distinct(env):
    with env["sessions"]() as db:
        bid = sync.ingest(db, {"system": "business", "task_id": "zero", "result_id": "zero"},
            [{"id": "zero", "execution": {"events": [], "calls": [], "duration_ms": 0}}])
        db.add(TraceBatch(batch_id="unpublished", source_system="upload", source_task_id="unpublished",
                          display_id="unpublished", active_snapshot_id="", payload={}))
        db.commit()
    zero = workspace(env, batch_id=bid)
    assert zero["batch_summary"]["sample_count"] == 1
    assert zero["tool_call_summary"] == {"total_call_count": 0, "items": []}
    unavailable = workspace(env, batch_id="unpublished")
    assert unavailable["scope"] is None
    assert unavailable["availability"] == "unavailable"
    assert "tool_call_summary" not in unavailable


@pytest.mark.parametrize("payload,state,count,message", [
    ({"rule_audit": {"status": "completed"}, "rule_findings": []},
     "completed_empty", 0, "审计完成，未发现命中项"),
    ({"rule_audit": {"status": "completed"}, "rule_findings": [{"event_id": "event-1"}]},
     "completed_findings", 1, "审计完成，发现 1 项"),
    *[({"rule_audit": {"status": status}}, "pending", None, "等待审计")
      for status in ("pending", "queued", "running")],
    ({}, "missing", None, "审计结果缺失"),
    ({"rule_audit": {"status": "completed"}}, "missing", None, "审计结果缺失"),
    ({"rule_audit": {"status": "completed"}, "rule_findings": None}, "missing", None, "审计结果缺失"),
    ({"rule_audit": {"status": "completed"}, "rule_findings": {}}, "missing", None, "审计结果缺失"),
    ({"rule_audit": "invalid", "rule_findings": []}, "missing", None, "审计结果缺失"),
    ({"rule_audit": {"version": "trace-rules-v1"}, "rule_findings": []}, "missing", None, "审计结果缺失"),
    ({"rule_audit": {"status": "error"}}, "missing", None, "审计结果缺失"),
    ({"rule_audit": {"status": []}}, "missing", None, "审计结果缺失"),
])
def test_audit_availability_projection(payload, state, count, message):
    original = deepcopy(payload)
    result = q.rule_audit_projection(payload)
    audit = result["rule_audit"]
    assert (audit["result_state"], audit["finding_count"], audit["display_message"]) == (state, count, message)
    assert isinstance(result["rule_findings"], list)
    assert payload == original
    if isinstance(payload.get("rule_audit"), dict):
        for key, value in payload["rule_audit"].items():
            assert audit[key] == value
    if not isinstance(payload.get("rule_audit"), dict) or "version" not in payload["rule_audit"]:
        assert "version" not in audit


@pytest.mark.parametrize("audit,findings,state", [
    ({"version": "trace-rules-v1", "status": "completed", "coverage": "limited"}, [], "completed_empty"),
    ({"version": "trace-rules-v1", "status": "completed"},
     [{"finding_id": "finding-1", "event_id": "event-1", "evidence_text": "person@example.com"}], "completed_findings"),
    ({"version": "trace-rules-v1", "status": "running"}, None, "pending"),
    ({"version": "trace-rules-v1", "status": "completed"}, None, "missing"),
    (None, None, "missing"),
])
def test_audit_contract_consistent_across_query_endpoints_without_writes(env, audit, findings, state):
    with env["sessions"]() as db:
        row = db.scalar(select(TraceSample).where(TraceSample.trace_id == env["trace"]))
        payload = deepcopy(row.payload)
        payload.pop("rule_audit", None)
        payload.pop("rule_findings", None)
        if audit is not None:
            payload["rule_audit"] = audit
        if findings is not None:
            payload["rule_findings"] = findings
        row.payload = payload
        while stats.materialize(db):
            db.flush()
        db.commit()
    url = f"{ROOT}/batches/{env['batch']}/samples/{env['trace']}"
    projections = []
    for view in ("execution", "tools", "statistics"):
        response = env["client"].get(url, params={"view": view, "batch_id": env["batch"]})
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        projections.append(data["summary"] if view == "statistics" else data)
        if view == "execution":
            projections.append(data["summary"])
    response = env["client"].get(f"{ROOT}/batches/{env['batch']}/samples", params={"view": "execution"})
    assert response.status_code == 200, response.text
    projections.append(response.json()["data"]["items"][0])
    expected = q.rule_audit_projection(payload)
    assert expected["rule_audit"]["result_state"] == state
    for projection in projections:
        assert projection["rule_audit"] == expected["rule_audit"]
        assert projection["rule_findings"] == expected["rule_findings"]
        if findings:
            assert projection["rule_findings"][0]["event_id"] == "event-1"
            assert projection["rule_findings"][0]["evidence_text"] == "[邮箱已脱敏]"
    with env["sessions"]() as db:
        row = db.scalar(select(TraceSample).where(TraceSample.trace_id == env["trace"]))
        assert row.payload == payload
