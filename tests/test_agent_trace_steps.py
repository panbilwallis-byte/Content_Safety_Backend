"""Competition action display over immutable sources, isolated from live data."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from test_agent_traces import env
from test_agent_trace_workflow import upload, USER
from app.models.agent_traces import TraceSample, TraceEvent, TraceEvidence
from app.services import agent_trace_steps as steps, agent_trace_query as q, agent_trace_statistics as stats
from app.services.agent_trace_ingest import normalize
from app.services.agent_trace_trajectory import EXAMPLE


def action(identity="a", reasoning=None):
    return {"role": "assistant", "reasoning_content": reasoning, "tool_calls": [
        {"id": identity, "function": {"name": "read", "arguments": '{"file":"public.txt"}'}}]}


def returned(identity="a", content="ok"):
    return {"role": "tool", "tool_call_id": identity, "content": content}


def sample(record):
    execution = normalize(record, "file", 0, json.dumps(record), 1, "auto")["execution"]
    return SimpleNamespace(trace_id="trace", result={"source_record": deepcopy(record)},
                           payload={**execution, "event_count": len(execution["events"])}), execution["events"]


def test_multi_calls_shared_reasoning_reused_ids_and_excluded_sources():
    first = action(reasoning="思考😀")
    first["tool_calls"].append({"id": "b", "function": {"name": "search", "arguments": "{}"}})
    record = {"messages": [{"role": "system", "content": "constraint"}, first,
        returned("b", "second"), returned("a", "first"),
        action("a"), returned("a", "reused"), {"role": "assistant", "content": "done"},
        {"role": "assistant", "content": ""}, returned("orphan", "unmatched")]}
    row, raw = sample(record)
    saved = deepcopy((record, raw, row.payload))
    events, mapping, uncounted, count = steps.project(row, raw)
    assert count["step_count"] == 4
    assert [e["ordinal"] for e in events] == [1, 2, 3, 4]
    assert "first" in events[0]["source_excerpt"] and "second" not in events[0]["source_excerpt"]
    assert "second" in events[1]["source_excerpt"]
    assert "reused" in events[2]["source_excerpt"]
    assert events[0]["reasoning_original"] == "思考😀"
    assert events[1]["reasoning_original"] == "" and events[1]["shared_reasoning_step"] == 1
    assert sum(e["reasoning_original"].count("思考😀") for e in events) == 1
    assert len(uncounted) == 3  # system, empty assistant, orphan return
    assert "unmatched" in events[0]["source_excerpt"]
    for event in raw:
        path = event["source_ref"]["source_path"]
        if path == "messages[2]":
            assert mapping[event["event_id"]] == events[1]["event_id"]
    assert (record, raw, row.payload) == saved


def test_duplicate_ids_do_not_attach_ambiguous_return():
    first = action()
    first["tool_calls"].append(deepcopy(first["tool_calls"][0]))
    row, raw = sample({"messages": [first, returned(content="ambiguous")]})
    events, _, uncounted, count = steps.project(row, raw)
    assert count["step_count"] == 2
    assert len({e["event_id"] for e in events}) == 2
    assert all(not any(p["label"] == "工具返回" for p in e["source_parts"]) for e in events)
    assert any(e["source_excerpt"] == "ambiguous" for e in uncounted)


def test_langchain_container_and_leaf_reply_not_double_counted():
    record = {"runs": [
        {"id": "root", "run_type": "chain", "outputs": {"answer": "done"}},
        {"id": "tool", "parent_run_id": "root", "run_type": "tool", "name": "search", "inputs": {}, "outputs": "ok"},
        {"id": "llm", "parent_run_id": "root", "run_type": "llm", "outputs": "done"}]}
    row, raw = sample(record)
    events, _, uncounted, count = steps.project(row, raw)
    assert count["step_count"] == 2
    assert [e["event_type"] for e in events] == ["tool_call", "message"]
    assert len(events[0]["source_event_ids"]) == 2
    assert len(uncounted) == 1
    del record["runs"][-1]
    row, raw = sample(record)
    events, _, _, count = steps.project(row, raw)
    assert count["step_count"] == 2 and events[-1]["source_ref"]["source_path"] == "runs[0]"


def test_trajectory_thought_call_result_and_final():
    row, raw = sample(deepcopy(EXAMPLE))
    events, mapping, uncounted, count = steps.project(row, raw)
    assert count["step_count"] == 2 and count["raw_event_count"] == 5
    assert events[0]["reasoning_original"] == EXAMPLE["events"][1]["reasoning"]
    assert mapping[raw[1]["event_id"]] == mapping[raw[2]["event_id"]] == mapping[raw[3]["event_id"]]
    assert [e["source_ref"]["source_path"] for e in uncounted] == ["events[0]"]
    assert events[-1]["label"] == "最终回复"


def test_multiple_recorded_thoughts_are_preserved_in_their_action_step():
    record = deepcopy(EXAMPLE)
    record["events"].insert(2, {**deepcopy(record["events"][1]), "content": "进一步分析", "reasoning": "第二段思考"})
    for i, event in enumerate(record["events"]):
        event["idx"] = i
    row, raw = sample(record)
    events, _, _, count = steps.project(row, raw, reasoning_getter=q.original_reasoning)
    assert count["step_count"] == 2
    assert events[0]["reasoning_original"] == record["events"][1]["reasoning"] + "\n\n第二段思考"


def test_missing_call_events_get_distinct_step_ids_instead_of_shared_message_id():
    first = action(reasoning="shared")
    first["content"] = "prepare"
    first["tool_calls"].append({"id": "b", "function": {"name": "search", "arguments": "{}"}})
    row, raw = sample({"messages": [first]})
    raw = [e for e in raw if e["event_type"] != "tool_call"]
    events, _, _, count = steps.project(row, raw)
    assert count["step_count"] == 2
    assert len({e["event_id"] for e in events}) == 2


def test_missing_originals_fallback_and_zero_step_are_distinct():
    row = SimpleNamespace(result={}, payload={"event_count": 9}, trace_id="legacy")
    assert steps.counts(row)["step_count"] is None
    row, raw = sample({"messages": [{"role": "user", "content": "hello"}]})
    events, _, uncounted, count = steps.project(row, raw)
    assert events == [] and len(uncounted) == 1
    assert count["step_count"] == 0 and count["event_count"] == 0


def test_graph_statistics_return_risk_aliases_redaction_and_immutable_reads(env, tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_TRACE_ENABLE_LEGACY_VALUE_SYNC", raising=False)
    record = {"messages": [{"role": "user", "content": '{"password":"context-secret"}'},
        action(reasoning='{"api_key":"reason-secret","text":"think"}'),
        returned(content='{"password":"return-secret","text":"ok"}'),
        {"role": "assistant", "content": "done"}]}
    bid, _ = upload(env, tmp_path, [record])
    with env["sessions"]() as db:
        while stats.materialize(db):
            db.flush()
        db.commit()
        _, scope = q.resolve(db, bid, {})
        row = db.scalar(select(TraceSample).where(TraceSample.snapshot_id == scope["snapshot_id"]))
        raw = q.process_records(db, scope, row.trace_id, TraceEvent)
        saved = deepcopy((row.payload, row.result, raw))
        eid, rid = raw[1]["event_id"], raw[2]["event_id"]
        db.add(TraceEvidence(id="return-evidence", snapshot_id=scope["snapshot_id"],
            trace_id=row.trace_id, event_id=rid, projection_version=scope["projection_version"], payload={
                "event_id": rid, "evidence_id": "return-evidence", "risk_type": {"code": "test", "label": "test"},
                "severity": "high", "match_pattern": "ok"}))
        db.commit()
        detail = q.execution_detail(db, scope, row.trace_id, {}, USER)
        assert detail["graph"]["total_nodes"] == detail["events"]["total"] == 2
        assert detail["summary"]["event_count"] == 2 and detail["summary"]["raw_event_count"] == 4
        assert detail["selection"]["event_id"] == detail["first_risk_event_id"] == eid
        assert detail["danger_patterns"]["items"][0]["event_id"] == eid
        selected = q.execution_detail(db, scope, row.trace_id, {"section": "selection", "event_id": rid}, USER)
        assert selected["selection"]["event_id"] == eid
        assert selected["selection"]["next_event_id"] == raw[3]["event_id"]
        document = json.dumps(detail)
        for secret in ("reason-secret", "return-secret", "context-secret"):
            assert secret not in document
        evidence = q.execution_detail(db, scope, row.trace_id, {"section": "evidence", "event_id": rid, "redaction_mode": "raw"}, USER)
        assert "return-secret" in evidence["original_text"]
        assert q.samples(db, scope, {}, "execution")["items"][0]["event_count"] == 2
        assert q.samples(db, scope, {}, "tools")["items"][0]["event_count"] == 4
        assert stats.aggregate(db, scope, {}, USER)["kpis"]["average_steps"]["value"] == 2
        assert stats.samples(db, scope, {}, USER)["items"][0]["event_count"] == 2
        assert (row.payload, row.result, q.process_records(db, scope, row.trace_id, TraceEvent)) == saved
        assert not db.dirty


def test_step_graph_paging_content_cursor_and_permissions(env, tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_TRACE_ENABLE_LEGACY_VALUE_SYNC", raising=False)
    record = {"messages": [{"role": "user", "content": "input"}]}
    for i in range(502):
        record["messages"].extend([action(str(i)), returned(str(i), "😀" * 18000 if i == 501 else "ok")])
    bid, _ = upload(env, tmp_path, [record])
    with env["sessions"]() as db:
        _, scope = q.resolve(db, bid, {})
        row = db.scalar(select(TraceSample).where(TraceSample.snapshot_id == scope["snapshot_id"]))
        first = q.execution_detail(db, scope, row.trace_id, {}, USER)
        assert first["graph"]["total_nodes"] == 502
        assert len(first["graph"]["nodes"]) == 500
        rest = q.execution_detail(db, scope, row.trace_id, {"section": "graph", "graph_cursor": first["graph"]["next_cursor"]}, USER)
        assert len(rest["graph"]["nodes"]) == 2 and rest["graph"]["complete"]
        assert len(rest["graph"]["edges"]) == 2  # includes chunk boundary
        eid = rest["graph"]["nodes"][-1]["event_id"]
        part = q.execution_detail(db, scope, row.trace_id, {"section": "selection", "event_id": eid}, USER)
        assert part["events"]["page"] == 11
        assert part["selection"]["graph_locator"]["cursor"]
        preview = part["selection"]["content"]["source_excerpt"]
        content = preview["text"]
        cursor = preview["next_cursor"]
        while cursor:
            segment = q.execution_detail(db, scope, row.trace_id, {"section": "selection", "event_id": eid, "selection_cursor": cursor}, USER)["selection_segment"]
            content += segment["text"]
            cursor = segment["next_cursor"]
        assert content.count("😀") == 18000
        with pytest.raises(HTTPException) as exc:
            q.execution_detail(db, scope, row.trace_id, {"section": "graph", "graph_cursor": q.cursor(scope, row.trace_id, "graph", 500, USER)}, USER)
        assert exc.value.status_code == 409
        with pytest.raises(HTTPException) as exc:
            q.execution_detail(db, scope, row.trace_id, {"redaction_mode": "raw"}, {"uid": 3, "tier": "reader"})
        assert exc.value.status_code == 403


def test_competition_samples_have_expected_step_counts():
    path = Path("C:/Users/杨/Desktop/3.1赛马数据/samples.jsonl")
    if not path.exists():
        pytest.skip("external competition fixture is unavailable")
    records = [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    expected = {"s01": 7, "p14": 43, "s04": 22, "science_agent_bench_66": 6, "science_agent_bench_48": 24}
    counts = {}
    for record in records:
        row, raw = sample(record)
        events, _, _, count = steps.project(row, raw)
        counts[record["trajectory_id"]] = count["step_count"]
        assert len(events) == count["step_count"]
    assert counts == expected
    assert sum(counts.values()) / len(counts) == 20.4


@pytest.mark.parametrize("record,expected", [
    ({"messages": [action(), returned(), {"role": "assistant", "content": "done"}]}, 2),
    ({"runs": [{"id": "root", "run_type": "chain", "outputs": {"answer": "done"}},
               {"id": "tool", "parent_run_id": "root", "run_type": "tool", "name": "read",
                "inputs": {}, "outputs": {"text": "result"}}]}, 2),
    (EXAMPLE, 2),
])
def test_supported_formats_export_same_steps_as_graph(env, tmp_path, monkeypatch, record, expected):
    from app.services import agent_trace_reports as reports
    monkeypatch.delenv("AGENT_TRACE_ENABLE_LEGACY_VALUE_SYNC", raising=False)
    bid, _ = upload(env, tmp_path, [record])
    with env["sessions"]() as db:
        while stats.materialize(db):
            db.flush()
        db.commit()
        _, scope = q.resolve(db, bid, {})
        row = db.scalar(select(TraceSample).where(TraceSample.snapshot_id == scope["snapshot_id"]))
        detail = q.execution_detail(db, scope, row.trace_id, {}, USER)
        assert detail["graph"]["total_nodes"] == expected
        normalized = stats.normalized(db, scope, row)
        assert len(normalized["steps"]) == expected
        assert len(normalized["events"]) == row.payload["event_count"]
        assert normalized["event_step_mapping"]
        data = {**scope, "view": "execution", "export_kind": "view", "format": "json",
                "scope": {"type": "sample", "trace_ids": [row.trace_id]}, "contents": ["normalized_json"],
                "selected_trace_id": row.trace_id, "selected_event_id": detail["selection"]["event_id"]}
        reports.validate(db, data, USER)
        body, _, _ = reports.build(db, SimpleNamespace(input_data=data, task_id="step-export", config={"owner_id": 1}))
        exported = json.loads(body)["samples"][0]
        assert exported["summary"]["event_count"] == exported["step_count"] == len(exported["steps"]) == expected
        graph_ids = {e["event_id"] for e in exported["steps"]}
        assert all(item["event_id"] in graph_ids for item in exported["event_step_mapping"])
        assert not db.dirty


def test_existing_snapshot_missing_event_coordinates_still_uses_saved_source(env, tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_TRACE_ENABLE_LEGACY_VALUE_SYNC", raising=False)
    bid, _ = upload(env, tmp_path, [{"messages": [action(), returned()]}])
    with env["sessions"]() as db:
        _, scope = q.resolve(db, bid, {})
        row = db.scalar(select(TraceSample).where(TraceSample.snapshot_id == scope["snapshot_id"]))
        for event in db.scalars(select(TraceEvent).where(TraceEvent.snapshot_id == scope["snapshot_id"])):
            event.payload = {k: v for k, v in event.payload.items() if k != "source_step_ids"}
        db.commit()
        saved = deepcopy(q.process_records(db, scope, row.trace_id, TraceEvent))
        detail = q.execution_detail(db, scope, row.trace_id, {}, USER)
        assert detail["step_count"] == 1 and detail["raw_event_count"] == 2
        assert detail["events"]["items"][0]["source_step_ids"] == [1]
        assert q.process_records(db, scope, row.trace_id, TraceEvent) == saved
        assert not db.dirty


def test_source_reconstructed_step_evidence_is_redacted_before_concatenation(env, tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_TRACE_ENABLE_LEGACY_VALUE_SYNC", raising=False)
    message = action()
    message["tool_calls"][0]["function"]["arguments"] = '{"api_key":"recovered-secret"}'
    bid, _ = upload(env, tmp_path, [{"messages": [message, returned()]}])
    with env["sessions"]() as db:
        _, scope = q.resolve(db, bid, {})
        row = db.scalar(select(TraceSample).where(TraceSample.snapshot_id == scope["snapshot_id"]))
        event = db.scalar(select(TraceEvent).where(TraceEvent.snapshot_id == scope["snapshot_id"]).order_by(TraceEvent.ordinal))
        db.delete(event)
        db.commit()
        detail = q.execution_detail(db, scope, row.trace_id, {}, USER)
        eid = detail["selection"]["event_id"]
        assert eid.startswith("step_")
        evidence = q.execution_detail(db, scope, row.trace_id, {"section": "evidence", "event_id": eid}, USER)
        assert "recovered-secret" not in json.dumps(evidence)
        raw = q.execution_detail(db, scope, row.trace_id, {"section": "evidence", "event_id": eid, "redaction_mode": "raw"}, USER)
        assert "recovered-secret" in raw["original_text"]
