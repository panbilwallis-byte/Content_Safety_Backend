from copy import deepcopy
import json

import pytest

from app.services.agent_trace_ingest import normalize
from app.services.agent_trace_trajectory import EXAMPLE
from app.services.agent_trace_generation import generated_sample, validate_process
from test_agent_traces import env


def adapt(record):
    return normalize(record, "fixture_file", 0, json.dumps(record), 1, "auto")


def test_trajectory_keeps_event_identity_and_recorded_fields():
    r = deepcopy(EXAMPLE)
    r["tokens"]["estimated"] = True
    result = adapt(r)
    p = result["execution"]
    assert p["source"]["format_code"] == "agent_trajectory"
    assert len(p["events"]) == 5 and len(p["calls"]) == 1
    assert len({e["event_id"] for e in p["events"]}) == 5
    assert p["events"][1]["reasoning_summary"] == "需要读取报告"
    assert p["events"][1]["relative_time_ms"] == 100
    assert p["calls"][0]["duration_ms"] == 100
    assert p["calls"][0]["execution_status"] == "succeeded"
    assert p["token_usage"]["estimated"] is True
    assert p["task_status"] == "completed"
    assert result["source_record"] == r
    _, payload, events, calls, _ = generated_sample("upload", result, 0)
    validate_process(payload, events, calls)


@pytest.mark.parametrize("status,expected", [("blocked", "not_executed"), ("error", "failed"), ("unknown", "unknown")])
def test_trajectory_call_status_and_policy_duration(status, expected):
    r = deepcopy(EXAMPLE)
    r["events"][3]["tool_status"] = status
    r["tool_events"][0].update(status=status, error_code="POLICY_DENIED" if status == "blocked" else "HTTP_404")
    p = adapt(r)["execution"]
    c = p["calls"][0]
    assert c["execution_status"] == expected
    if status == "blocked":
        assert c["duration_ms"] is None
        assert c["policy_duration_ms"] == 100
        assert c["permission_decision"] == "denied"
    validate_process(p, p["events"], p["calls"])


def test_duplicate_event_index_and_orphan_details_rejected():
    r = deepcopy(EXAMPLE)
    r["events"][1]["idx"] = 0
    with pytest.raises(ValueError):
        adapt(r)
    r = deepcopy(EXAMPLE)
    r["tool_events"][0]["step_id"] = "missing"
    with pytest.raises(ValueError):
        adapt(r)


def test_anthropic_is_not_silently_treated_as_openai():
    with pytest.raises(ValueError):
        adapt({"messages": [{"role": "assistant", "content": [{"type": "tool_use", "id": "x", "name": "search"}]}]})


def test_upload_publish_and_four_projections_without_http(env, tmp_path):
    from app.services import agent_trace_ingest as ingest, agent_trace_statistics as stats, agent_trace_query as q
    from app.models.tables import Task
    import hashlib
    from uuid import uuid4

    user = {"uid": 1, "tier": "admin"}
    raw = json.dumps(EXAMPLE, ensure_ascii=False).encode()
    path = tmp_path / "trajectory.jsonl"
    path.write_bytes(raw)
    with env["sessions"]() as db:
        b = ingest.create_batch(db, user, "trajectory adapter test", uuid4().hex)
        ingest.register_asset(db, user, "fixture_file", path.name, path, len(raw), hashlib.sha256(raw).hexdigest(), uuid4().hex)
        bound = ingest.bind(db, user, b["batch_id"], {"expected_version": 1,
            "files": [{"file_id": "fixture_file", "format_override": "agent_trajectory"}]}, uuid4().hex)
        task, _ = ingest.submit(db, {"batch_id": b["batch_id"], "expected_version": bound["batch"]["batch_version"]}, user, uuid4().hex, "parse")
        tid = task["task_id"]
    ingest.run(tid, env["sessions"])
    with env["sessions"]() as db:
        assert db.get(Task, tid).status == "succeeded"
        stats.materialize(db)
        db.commit()
        w = ingest.workspace(db, {"batch_id": b["batch_id"]}, user)
        assert w["summary"]["parsed_trace_count"] == 1
        assert w["summary"]["by_format"][0]["code"] == "agent_trajectory"
        trace = w["resolved_selection"]["trace_id"]
        batch, scope = q.resolve(db, b["batch_id"], {})
        assert ingest.detail(db, scope, trace, {}, user)["events"]["total"] == 5
        tools = q.tool_detail(db, batch, scope, trace, {}, user)
        assert tools["calls"]["total"] == 1
        assert tools["calls"]["items"][0]["execution_status"] == "succeeded"
        execution = q.execution_detail(db, scope, trace, {}, user)
        assert execution["events"]["total"] == 5
        assert stats.workspace(db, {"batch_id": b["batch_id"]}, user)["kpis"]["recorded_tokens"]["value"] == 150
