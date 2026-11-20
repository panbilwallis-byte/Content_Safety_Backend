"""Recorded reasoning, event timing and evidence summaries over the public route."""
from copy import deepcopy
import json

import pytest
from sqlalchemy import select

from test_agent_traces import env, ROOT
from test_agent_trace_workflow import upload, USER
from app.core.auth import create_token
from app.models.agent_traces import TraceEvent, TraceSample, TraceCall
from app.services import agent_trace_query as q
from app.services.agent_trace_ingest import normalize
from app.services.agent_trace_rules import audit_process
from app.services.agent_trace_competition import analyze
from app.services.agent_trace_trajectory import EXAMPLE


@pytest.fixture
def recorded(env, tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_TRACE_ENABLE_LEGACY_VALUE_SYNC", raising=False)
    raw = json.dumps({"api_key": "reasoning-secret", "text": "源推理😀" * 6000}, ensure_ascii=False)
    bid, _ = upload(env, tmp_path, [{"messages": [{"role": "user", "content": "公开报告"},
        {"role": "assistant", "reasoning": raw}]}])
    with env["sessions"]() as db:
        _, scope = q.resolve(db, bid, {})
        row = db.scalar(select(TraceSample).where(TraceSample.snapshot_id == scope["snapshot_id"]))
        trace = row.trace_id
        events = q.process_records(db, scope, trace, TraceEvent)
    return {**env, "bid": bid, "scope": scope, "trace": trace, "eid": events[-1]["event_id"], "raw": raw}


def fetch(recorded, **params):
    return recorded["client"].get(f"{ROOT}/batches/{recorded['bid']}/samples/{recorded['trace']}", params={
        "view": "execution", **recorded["scope"], "event_id": recorded["eid"], **params})


@pytest.mark.parametrize("section", ["overview", "events", "selection"])
@pytest.mark.parametrize("mode", ["raw", "redacted"])
def test_reasoning_original_is_complete_in_selection_and_preview(recorded, section, mode):
    response = fetch(recorded, section=section, redaction_mode=mode)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    event = data["events"]["items"][-1]
    expected = recorded["raw"] if mode == "raw" else q.redact(recorded["raw"])
    for selection in (data["selection"], event["selection_preview"]):
        assert selection["content"]["reasoning_original"] == {
            "text": expected, "highlights": [], "truncated": False, "provenance": "source_log"}
        assert selection["content"]["reasoning_summary"]["truncated"] is True
    assert "reasoning_original" not in event  # No duplicate unstructured raw text.
    assert len(expected) > 16384
    if mode == "redacted":
        assert "reasoning-secret" not in response.text


def test_reasoning_permissions_and_empty_original(recorded):
    recorded["client"].headers["Authorization"] = "Bearer " + create_token(3, "user-3", "审计员")
    assert fetch(recorded, redaction_mode="raw").status_code == 403
    data = fetch(recorded).json()["data"]
    assert "reasoning-secret" not in data["selection"]["content"]["reasoning_original"]["text"]
    assert data["events"]["items"][0]["selection_preview"]["content"]["reasoning_original"] == {
        "text": "", "highlights": [], "truncated": False, "provenance": "not_provided"}


@pytest.mark.parametrize("patch,expected", [
    ({"reasoning_summary": "有来源的旧原文", "reasoning_provenance": "source_log"}, "有来源的旧原文"),
    ({"reasoning_summary": "无来源的摘要", "reasoning_provenance": "derived_summary"}, ""),
    ({"reasoning_summary": "无来源的摘要", "reasoning_provenance": None}, ""),
    ({"reasoning_summary": "源日志未记录推理摘要", "reasoning_provenance": "source_log"}, ""),
    ({"event_type": "thought", "source_excerpt": "真实思考记录", "reasoning_provenance": None}, "真实思考记录"),
    ({"data_origin": "generated", "event_type": "thought", "reasoning_summary": "模拟摘要",
      "reasoning_provenance": "source_log", "source_excerpt": "模拟思考"}, ""),
])
def test_historical_reasoning_fallback_without_writes(recorded, patch, expected):
    with recorded["sessions"]() as db:
        row = db.scalar(select(TraceEvent).where(TraceEvent.snapshot_id == recorded["scope"]["snapshot_id"],
                                                TraceEvent.event_id == recorded["eid"]))
        row.payload = {k: v for k, v in {**row.payload, **patch}.items() if k != "reasoning_original"}
        saved = deepcopy(row.payload)
        db.commit()
    original = fetch(recorded).json()["data"]["selection"]["content"]["reasoning_original"]
    assert original["text"] == expected
    assert original["provenance"] == ("source_log" if expected else "not_provided")
    with recorded["sessions"]() as db:
        assert db.scalar(select(TraceEvent).where(TraceEvent.snapshot_id == recorded["scope"]["snapshot_id"],
                                                  TraceEvent.event_id == recorded["eid"])).payload == saved


@pytest.mark.parametrize("field", ["reasoning", "reasoning_content"])
def test_openai_reasoning_only_messages_are_preserved(field):
    record = {"messages": [{"role": "assistant", field: "真实推理"}]}
    events = normalize(record, "file_r", 0, json.dumps(record), 1, "auto")["execution"]["events"]
    assert len(events) == 1
    assert events[0]["reasoning_original"] == "真实推理"
    assert events[0]["source_ref"]["source_path"] == "messages[0]." + field


@pytest.mark.parametrize("with_reasoning", [False, True])
def test_trajectory_thought_original(with_reasoning):
    record = deepcopy(EXAMPLE)
    thought = record["events"][1]
    if not with_reasoning:
        thought.pop("reasoning")
    event = normalize(record, "file_r", 0, json.dumps(record), 1, "auto")["execution"]["events"][1]
    assert event["reasoning_original"] == thought.get("reasoning", thought["content"])


@pytest.mark.parametrize("timestamp,expected", [(None, False), (0, True), (12.5, True), (-1, False),
    (True, False), ("0", False), (float("nan"), False), (float("inf"), False)])
def test_time_flag_uses_all_events_in_all_sections(recorded, timestamp, expected):
    with recorded["sessions"]() as db:
        rows = list(db.scalars(select(TraceEvent).where(TraceEvent.snapshot_id == recorded["scope"]["snapshot_id"]).order_by(TraceEvent.ordinal)))
        rows[-1].payload = {**rows[-1].payload, "relative_time_ms": timestamp}
        db.commit()
    for section in ("overview", "events", "selection", "graph"):
        response = fetch(recorded, section=section, event_id=rows[0].event_id, event_page_size=1)
        assert response.status_code == 200, response.text
        assert response.json()["data"]["has_event_time"] is expected


@pytest.mark.parametrize("timestamp", [float("nan"), float("inf"), float("-inf")])
def test_import_does_not_persist_nonfinite_times(timestamp):
    record = {"messages": [{"role": "assistant", "content": "text", "ts_ms": timestamp}]}
    assert normalize(record, "file_r", 0, "{}", 1, "auto")["execution"]["events"][0]["relative_time_ms"] is None
    record = deepcopy(EXAMPLE)
    record["events"][0]["ts_ms"] = timestamp
    assert normalize(record, "file_r", 0, "{}", 1, "auto")["execution"]["events"][0]["relative_time_ms"] is None


def test_long_rule_snippet_not_capped_at_2000():
    arguments = json.dumps({"command": "cat /forbidden/data", "padding": "x" * 5000})
    record = {"trajectory_id": "long-risk", "messages": [
        {"role": "system", "content": "Working directory: /workspace\nNever access files outside of the working directory."},
        {"role": "assistant", "tool_calls": [{"id": "c", "function": {"name": "bash", "arguments": arguments}}]}]}
    value = normalize(record, "file_r", 0, json.dumps(record), 1, "auto")
    p = value["execution"]
    evidence = audit_process(p, p["events"], p["calls"], record=record)
    assert any(len(e["match_pattern"]) > 2000 for e in evidence)
    quote = analyze(record)["results"]["B3"]["risks"][0]["risk_quote"]
    assert any(e["match_pattern"] == quote for e in evidence)


def test_blank_tool_names_are_grouped_as_unknown(env):
    with env["sessions"]() as db:
        rows = list(db.scalars(select(TraceCall).where(TraceCall.snapshot_id == env["scope"]["snapshot_id"])))
        for row, name in zip(rows[:3], [None, "", "   "]):
            row.payload = {**row.payload, "tool_name": name}
        db.commit()
    data = env["client"].get(ROOT + "/workspace", params={"view": "execution", "include": "dashboard", "batch_id": env["batch"]}).json()["data"]
    summary = data["tool_call_summary"]
    assert summary["total_call_count"] == 56
    assert {x["tool_name"]: x["call_count"] for x in summary["items"]}["unknown"] == 3
