"""Full source evidence and event risk flags through the existing detail route."""
from copy import deepcopy
import json

import pytest
from sqlalchemy import select

from test_agent_traces import env, ROOT
from test_agent_trace_workflow import upload, USER
from app.core.auth import create_token
from app.models.agent_traces import TraceBatch, TraceEvent, TraceEvidence, TraceSample, TraceSnapshot
from app.models.tables import SystemUser
from app.services import agent_trace_query as q
from app.services.agent_trace_trajectory import EXAMPLE


@pytest.fixture
def actual(env, tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_TRACE_ENABLE_LEGACY_VALUE_SYNC", raising=False)
    before = json.dumps({"password": "before-secret", "email": "before@example.com",
                         "text": "前文😀" * 6000}, ensure_ascii=False)
    original = json.dumps({"command": "cat /other/file", "api_key": "call-secret",
                           "text": "原文😀" * 6000}, ensure_ascii=False)
    after = json.dumps({"status": "success", "access_token": "after-secret", "email": "after@example.com",
                        "text": "后文😀" * 6000}, ensure_ascii=False)
    record = {"trajectory_id": "evidence-risk", "messages": [
        {"role": "system", "content": "Working directory: /workspace\nNever access files outside of the working directory."},
        {"role": "user", "content": before},
        {"role": "assistant", "tool_calls": [{"id": "source-call", "function": {"name": "bash", "arguments": original}}]},
        {"role": "tool", "tool_call_id": "source-call", "content": after},
        {"role": "assistant", "content": "完成"},
    ]}
    operation = deepcopy(EXAMPLE)
    operation["events"][3].update(tool_status="error", retry=True, duplicate=True)
    operation["tool_events"][0].update(status="error", error_code="TIMEOUT")
    bid, _ = upload(env, tmp_path, [record, operation])
    with env["sessions"]() as db:
        _, scope = q.resolve(db, bid, {})
        samples = list(db.scalars(select(TraceSample).where(TraceSample.snapshot_id == scope["snapshot_id"])
                                  .order_by(TraceSample.ingest_order)))
        events = q.process_records(db, scope, samples[0].trace_id, TraceEvent)
        other_events = q.process_records(db, scope, samples[1].trace_id, TraceEvent)
        trace = samples[0].trace_id
        other_trace = samples[1].trace_id
    return {**env, "batch": bid, "scope": scope, "trace": trace, "events": events,
            "other_trace": other_trace, "other_events": other_events,
            "original": original, "before": before, "after": after, "record": record}


def fetch(actual, **params):
    return actual["client"].get(f"{ROOT}/batches/{actual['batch']}/samples/{actual['trace']}", params={
        **actual["scope"], "view": "execution", "section": "evidence",
        "event_id": actual["events"][2]["event_id"], "permissions_version": "platform-v1", **params})


def test_full_evidence_not_a_truncated_summary_and_no_database_writes(actual):
    with actual["sessions"]() as db:
        stored = q.get_sample(db, actual["scope"], actual["trace"])
        saved = deepcopy(stored.payload)
    response = fetch(actual, redaction_mode="raw")
    assert response.status_code == 200, response.text
    document = response.json()["data"]
    assert response.json()["code"] == 0
    assert document["scope"] == {**actual["scope"], "permissions_version": "platform-v1"}
    assert document["trace_id"] == actual["trace"]
    assert document["event_id"] == actual["events"][2]["event_id"]
    assert document["source_location"] == actual["events"][2]["source_ref"]
    assert isinstance(document["source_ref"], str)
    assert actual["events"][2]["source_ref"]["source_path"] in document["source_ref"]
    assert document["original_text"] == actual["original"]
    assert document["before_text"] == actual["before"]
    assert document["after_text"] == actual["after"]
    assert document["complete"] is True
    assert len(document["original_text"]) > 16384
    overview = fetch(actual, section="overview").json()["data"]
    selected = overview["selection"]
    assert selected["content"]["source_excerpt"]["truncated"] is True
    assert len(selected["content"]["source_excerpt"]["text"]) < len(document["original_text"])
    with actual["sessions"]() as db:
        assert q.get_sample(db, actual["scope"], actual["trace"]).payload == saved
        assert q.process_records(db, actual["scope"], actual["trace"], TraceEvent) == actual["events"]


@pytest.mark.parametrize("mode", ["raw", "redacted"])
def test_full_description_and_evidence_summary_for_hover(actual, mode):
    response = fetch(actual, section="overview", redaction_mode=mode)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    event = data["events"]["items"][2]
    expected = actual["original"] if mode == "raw" else q.redact(actual["original"])
    assert event["description"] == expected
    assert len(event["description"]) > 512
    evidence = event["selection_preview"]["risk_evidence"]["items"]
    assert all(item["match_pattern"] in event["evidence_summary"] for item in evidence)
    assert len(event["evidence_summary"]) > 2000
    assert event["evidence_summary"] == data["selection"]["evidence_summary"]
    if mode == "redacted":
        assert "call-secret" not in response.text


@pytest.mark.parametrize("independent", [False, True])
def test_historical_description_recovery_preserves_independent_description(actual, independent):
    with actual["sessions"]() as db:
        event = db.scalar(select(TraceEvent).where(TraceEvent.snapshot_id == actual["scope"]["snapshot_id"],
                                                  TraceEvent.event_id == actual["events"][2]["event_id"]))
        description = "独立编写的事件描述" if independent else actual["original"][:512]
        event.payload = {**event.payload, "description": description}
        saved = deepcopy(event.payload)
        db.commit()
    data = fetch(actual, section="overview", redaction_mode="raw").json()["data"]
    assert data["events"]["items"][2]["description"] == (description if independent else actual["original"])
    with actual["sessions"]() as db:
        assert db.scalar(select(TraceEvent).where(TraceEvent.snapshot_id == actual["scope"]["snapshot_id"],
                                                 TraceEvent.event_id == actual["events"][2]["event_id"])).payload == saved


def test_source_string_keeps_structured_locations_on_other_sections(actual):
    data = fetch(actual, redaction_mode="raw").json()["data"]
    location = data["source_location"]
    assert location["source_id"] in data["source_ref"]
    assert f"记录 {location['record_index']}" in data["source_ref"]
    assert location["source_path"] in data["source_ref"]
    assert f"行 {location['line_start']}" in data["source_ref"]
    selection = fetch(actual, section="selection", redaction_mode="raw").json()["data"]["selection"]
    assert selection["content"]["source_ref"] == location


def test_source_string_missing_and_line_range(actual):
    with actual["sessions"]() as db:
        event = db.scalar(select(TraceEvent).where(TraceEvent.snapshot_id == actual["scope"]["snapshot_id"],
                                                  TraceEvent.event_id == actual["events"][2]["event_id"]))
        original_ref = deepcopy(event.payload["source_ref"])
        event.payload = {**event.payload, "source_ref": {**original_ref, "line_start": 4, "line_end": 9}}
        db.commit()
    assert "行 4–9" in fetch(actual).json()["data"]["source_ref"]
    with actual["sessions"]() as db:
        event = db.scalar(select(TraceEvent).where(TraceEvent.snapshot_id == actual["scope"]["snapshot_id"],
                                                  TraceEvent.event_id == actual["events"][2]["event_id"]))
        event.payload = {**event.payload, "source_ref": {}}
        db.commit()
    document = fetch(actual).json()["data"]
    assert document["source_ref"] is None and document["source_location"] == {}


@pytest.mark.parametrize("reader", [False, True])
def test_source_and_context_redacted_using_current_permissions(actual, reader):
    if reader:
        actual["client"].headers["Authorization"] = "Bearer " + create_token(3, "user-3", "审计员")
    response = fetch(actual)
    assert response.status_code == 200, response.text
    document = response.json()["data"]
    assert document["redaction_mode"] == "redacted"
    for field, raw in (("before_text", actual["before"]), ("original_text", actual["original"]), ("after_text", actual["after"])):
        assert document[field] == q.redact(raw)
    for secret in ("before-secret", "call-secret", "after-secret", "before@example.com", "after@example.com"):
        assert secret not in response.text
    assert document["complete"] is True
    if reader:
        assert fetch(actual, redaction_mode="raw").status_code == 403


def test_evidence_authentication_rechecked_on_each_request(actual):
    assert fetch(actual, redaction_mode="raw").status_code == 200
    with actual["sessions"]() as db:
        db.get(SystemUser, 1).role = "审计员"
        db.commit()
    assert fetch(actual, redaction_mode="raw").status_code == 403
    with actual["sessions"]() as db:
        db.get(SystemUser, 1).status = "disabled"
        db.commit()
    assert fetch(actual).status_code == 403
    response = actual["client"].get(f"{ROOT}/batches/{actual['batch']}/samples/{actual['trace']}",
        params={"view": "execution", "section": "evidence", "event_id": actual["events"][2]["event_id"]},
        headers={"Authorization": ""})
    assert response.status_code == 401


@pytest.mark.parametrize("params,status", [
    ({"event_id": ""}, 422), ({"event_id": "no-such-event"}, 404),
    ({"redaction_mode": "unsupported"}, 422), ({"permissions_version": "stale"}, 409),
    ({"snapshot_id": "missing"}, 410), ({"projection_version": "missing"}, 410),
    ({"section": "unsupported"}, 422),
])
def test_evidence_invalid_parameters_keep_error_envelope(actual, params, status):
    response = fetch(actual, **params)
    assert response.status_code == status, response.text
    assert response.json()["code"] == status


def test_evidence_rejects_events_from_other_samples_and_snapshots(actual):
    response = fetch(actual, event_id=actual["other_events"][0]["event_id"])
    assert response.status_code == 404
    with actual["sessions"]() as db:
        db.add(TraceSnapshot(snapshot_id="foreign-snapshot", batch_id="different-batch", payload={}))
        db.commit()
    assert fetch(actual, snapshot_id="foreign-snapshot").status_code == 409


def test_first_and_last_context_never_crosses_sample_boundary(actual):
    first = fetch(actual, event_id=actual["events"][0]["event_id"], redaction_mode="raw").json()["data"]
    assert first["before_text"] == ""
    assert first["original_text"] == actual["events"][0]["source_excerpt"]
    assert first["after_text"] == actual["before"]
    last = fetch(actual, event_id=actual["events"][-1]["event_id"], redaction_mode="raw").json()["data"]
    assert last["after_text"] == ""
    assert last["original_text"] == "完成"


@pytest.mark.parametrize("change", [{"source_excerpt": None}, {"source_excerpt": 12}, {"data_origin": "generated"}])
def test_missing_original_never_falls_back_to_display_summary(actual, change):
    with actual["sessions"]() as db:
        event = db.scalar(select(TraceEvent).where(TraceEvent.snapshot_id == actual["scope"]["snapshot_id"],
                                                  TraceEvent.event_id == actual["events"][2]["event_id"]))
        event.payload = {**event.payload, "description": "仅有摘要", **change}
        db.commit()
    response = fetch(actual)
    assert response.status_code == 404
    assert "完整原文" in response.json()["message"]


def test_empty_original_is_valid_complete_source(actual):
    with actual["sessions"]() as db:
        event = db.scalar(select(TraceEvent).where(TraceEvent.snapshot_id == actual["scope"]["snapshot_id"],
                                                  TraceEvent.event_id == actual["events"][2]["event_id"]))
        event.payload = {**event.payload, "source_excerpt": ""}
        db.commit()
    document = fetch(actual).json()["data"]
    assert document["original_text"] == "" and document["complete"] is True


@pytest.mark.parametrize("section", ["overview", "events", "selection"])
def test_event_risk_flag_agrees_with_evidence_in_all_event_lists(actual, section):
    response = fetch(actual, section=section)
    assert response.status_code == 200, response.text
    document = response.json()["data"]
    assert document["first_risk_event_id"] == actual["events"][2]["event_id"]
    assert document["selection"]["has_risk"] is True
    for event in document["events"]["items"]:
        assert type(event["has_risk"]) is bool
        assert event["has_risk"] is (event["event_id"] == actual["events"][2]["event_id"])
        assert event["selection_preview"]["has_risk"] is event["has_risk"]
        assert bool(event["selection_preview"]["risk_evidence"]["items"]) is event["has_risk"]


def test_operations_retries_and_duplicate_findings_are_not_security_flags(actual):
    url = f"{ROOT}/batches/{actual['batch']}/samples/{actual['other_trace']}"
    response = actual["client"].get(url, params={**actual["scope"], "view": "execution"})
    assert response.status_code == 200, response.text
    document = response.json()["data"]
    assert document["rule_findings"]
    assert {f["category"] for f in document["rule_findings"]} == {"operation", "efficiency"}
    assert all(event["has_risk"] is False for event in document["events"]["items"])
    assert document["first_risk_event_id"] is None


def test_dismissed_evidence_overrides_stale_event_risk_level(actual):
    with actual["sessions"]() as db:
        for item in db.scalars(select(TraceEvidence).where(TraceEvidence.snapshot_id == actual["scope"]["snapshot_id"])):
            item.payload = {**item.payload, "review_status": "dismissed"}
        db.commit()
    document = fetch(actual, section="overview").json()["data"]
    assert document["events"]["items"][2]["risk_level"] == "none"
    assert document["events"]["items"][2]["evidence_summary"] == ""
    assert document["graph"]["nodes"][2]["risk_level"] == "none"
    assert all(e["has_risk"] is False for e in document["events"]["items"])
    assert document["first_risk_event_id"] is None
    with actual["sessions"]() as db:
        assert q.process_records(db, actual["scope"], actual["trace"], TraceEvent)[2]["risk_level"] == "high"


def test_evidence_and_event_flags_work_beyond_first_event_page(actual):
    document = fetch(actual, section="selection", event_page_size=1).json()["data"]
    assert document["events"]["page"] == 3
    assert document["events"]["items"][0]["has_risk"] is True
    evidence = fetch(actual, redaction_mode="raw").json()["data"]
    assert evidence["original_text"] == actual["original"]


def test_reasoning_only_source_uses_recorded_reasoning_not_empty_content(actual, tmp_path):
    record = {"messages": [{"role": "user", "content": "question"},
                           {"role": "assistant", "reasoning_content": "真实推理😀" * 6000}]}
    bid, _ = upload(actual, tmp_path, [record])
    with actual["sessions"]() as db:
        _, scope = q.resolve(db, bid, {})
        sample = db.scalar(select(TraceSample).where(TraceSample.snapshot_id == scope["snapshot_id"]))
        events = q.process_records(db, scope, sample.trace_id, TraceEvent)
        document = q.execution_detail(db, scope, sample.trace_id,
            {"section": "evidence", "event_id": events[-1]["event_id"], "redaction_mode": "raw"}, USER)
        assert document["original_text"] == record["messages"][1]["reasoning_content"]
        assert document["complete"] is True


@pytest.mark.parametrize("record,event_index,expected,path", [
    ({"runs": [{"id": "root", "run_type": "chain", "inputs": {}, "outputs": {}},
               {"id": "tool", "parent_run_id": "root", "run_type": "tool", "name": "search",
                "inputs": {"q": "one"}, "outputs": {"answer": "complete result"}}]},
     2, '{"answer":"complete result"}', "runs[1].outputs"),
    (EXAMPLE, 3, "公开报告正文", "events[3]"),
])
def test_evidence_supports_other_ingestion_formats(actual, tmp_path, record, event_index, expected, path):
    bid, _ = upload(actual, tmp_path, [record])
    with actual["sessions"]() as db:
        _, scope = q.resolve(db, bid, {})
        sample = db.scalar(select(TraceSample).where(TraceSample.snapshot_id == scope["snapshot_id"]))
        events = q.process_records(db, scope, sample.trace_id, TraceEvent)
        trace = sample.trace_id
    response = actual["client"].get(f"{ROOT}/batches/{bid}/samples/{trace}", params={**scope,
        "view": "execution", "section": "evidence", "event_id": events[event_index]["event_id"], "redaction_mode": "raw"})
    assert response.status_code == 200, response.text
    document = response.json()["data"]
    assert document["original_text"] == expected
    assert document["source_location"]["source_path"] == path
    assert path in document["source_ref"]
    assert document["complete"] is True


def test_history_evidence_keeps_requested_snapshot(actual):
    with actual["sessions"]() as db:
        batch = db.get(TraceBatch, actual["batch"])
        batch.active_snapshot_id = "not-the-requested-snapshot"
        db.commit()
    document = fetch(actual, redaction_mode="raw").json()["data"]
    assert document["scope"]["snapshot_id"] == actual["scope"]["snapshot_id"]
    assert document["original_text"] == actual["original"]
