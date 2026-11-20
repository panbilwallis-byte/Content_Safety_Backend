"""Read-only report projection for ordinary uploaded traces; never reruns audit rules."""
from collections import Counter
import math
import re

from sqlalchemy import select

from app.models.agent_traces import TraceEvent, TraceEvidence
from app.services import agent_trace_query as q
from app.services.agent_trace_competition import MISSING, NAMES, text

VERSION = "batch-report-v1"


def tool_counts(calls):
    counts = Counter((c.get("tool_name") or "").strip() or "unknown" for c in calls)
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def ordinary_report(db, scope, sample, calls):
    payload = sample.payload
    if payload.get("data_origin") != "actual":
        q.fail(422, "当前快照含模拟轨迹，不能生成真实原文报告")
    events = q.process_records(db, scope, sample.trace_id, TraceEvent)
    by_id = {e["event_id"]: e for e in events}
    call_by_event = {event_id: c for c in calls for event_id in (c.get("event_id"), c.get("return_event_id")) if event_id}
    calls_by_id = {c["call_id"]: c for c in calls}

    def location(event):
        if event is None:
            return {}
        ref = event.get("source_ref") or {}
        result = {"event_id": event["event_id"], "event_ordinal": event["ordinal"], "source_ref": ref}
        match = re.match(r"messages\[(\d+)\]", ref.get("source_path", ""))
        message_index = ref.get("message_index", int(match[1]) if match else None)
        if type(message_index) is int and message_index >= 0:
            result["message_index"] = message_index
        call = call_by_event.get(event["event_id"])
        if call:
            result["step_id"] = by_id[call["event_id"]]["ordinal"]
            result["call_id"] = call["call_id"]
        else:
            result["field"] = ref.get("source_path") or f"events[{event['ordinal'] - 1}]"
        return result

    def evidence_for(event):
        original = q.recorded_event_text(event) if event else None
        return [{**location(event), "quote": original}] if original is not None else []

    def call_evidence(call):
        return evidence_for(by_id.get(call["event_id"])) + evidence_for(by_id.get(call.get("return_event_id")))

    segments, absent, grouped = [], [], {}
    for event in events:
        original = q.original_reasoning(event)
        if original["provenance"] != "source_log":
            if event.get("role") == "assistant" or event.get("event_type") == "thought":
                absent.append(event["ordinal"])
            continue
        ref = event.get("source_ref") or {}
        match = re.match(r"messages\[\d+\]", ref.get("source_path", ""))
        # One message may emit content and multiple calls sharing the same reasoning.
        identity = (ref.get("source_id"), ref.get("record_index"), match[0] if match else event["event_id"])
        if identity in grouped:
            grouped[identity]["step_ids"].append(event["ordinal"])
        else:
            segment = {**location(event), "step_id": event["ordinal"], "step_ids": [event["ordinal"]], "text": original["text"]}
            grouped[identity] = segment
            segments.append(segment)

    evidence_rows = [e.payload for e in db.scalars(select(TraceEvidence).where(
        TraceEvidence.snapshot_id == scope["snapshot_id"], TraceEvidence.trace_id == sample.trace_id,
        TraceEvidence.projection_version == scope["projection_version"]).order_by(TraceEvidence.id))
        if e.payload.get("review_status") != "dismissed"]
    evidence_rows.sort(key=lambda item: (by_id.get(item["event_id"], {}).get("ordinal", math.inf), item["evidence_id"]))
    risks = []
    for item in evidence_rows:
        event = by_id.get(item["event_id"])
        sources = item.get("source_evidence") or []
        original_evidence = [{**source, "quote": text(source["quote"])} for source in sources
                             if isinstance(source, dict) and source.get("quote") is not None]
        risks.append({**location(event), "rule_id": item.get("rule_id"), "label": item["risk_type"]["label"],
                      "severity": item.get("severity"), "outcome": item.get("outcome", "recorded"),
                      "risk_quote": item.get("match_pattern", ""),
                      "evidence": original_evidence or [{**location(event), "quote": item.get("match_pattern", "")}]})

    rendered_calls, unmatched, missing, failures, repeats = [], [], [], [], []
    for call in calls:
        event = by_id[call["event_id"]]
        returned = by_id.get(call.get("return_event_id"))
        result = {**location(event), "tool_name": (call.get("tool_name") or "").strip() or "unknown",
                  "arguments": call.get("input_json"), "returns": []}
        if returned:
            result["returns"].append({**location(returned), "content": q.recorded_event_text(returned)})
        elif call.get("execution_status") != "not_executed":
            unmatched.append({**location(event), "issue": "调用缺少可配对的返回",
                              "content": call.get("input_json"), "evidence": call_evidence(call)})
        for field in ("tool_name", "input_json"):
            if call.get(field) is None or (field == "tool_name" and not str(call[field]).strip()):
                missing.append({**location(event), "field": field, "issue": "未提供", "evidence": call_evidence(call)})
        rendered_calls.append(result)
    for event in events:
        if event.get("event_type") == "tool_result" and event["event_id"] not in call_by_event:
            unmatched.append({**location(event), "issue": "返回无法唯一配对", "content": q.recorded_event_text(event),
                              "evidence": evidence_for(event)})

    for finding in payload.get("rule_findings", []):
        category = finding.get("category")
        if category not in {"operation", "efficiency"}:
            continue
        event = by_id.get(finding.get("event_id"))
        call = calls_by_id.get(finding.get("call_id"))
        item = {**location(event), "rule_id": finding.get("rule_id"), "issue": finding.get("label", MISSING),
                "evidence": call_evidence(call) if call else evidence_for(event)}
        if category == "operation":
            item["error_type"] = finding.get("label", MISSING)
            failures.append(item)
        else:
            repeats.append(item)

    issues = payload.get("issue_types", [])
    missing.extend({"field": code, "issue": label, "evidence": []} for code, label in (
        ("time_missing", "未提供样本时长"), ("usage_missing", "未提供完整Token用量"),
        ("structure_invalid", "源记录结构不完整")) if code in issues)
    if "call_unlinked" in issues and not unmatched:
        missing.append({"field": "call_link", "issue": "源记录存在调用关联问题", "evidence": []})
    source = sample.result.get("source_record") or {}
    metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
    state = metadata.get("record") if isinstance(metadata.get("record"), dict) else {}
    truncations = ([{"field": "metadata.record.is_truncated", "value": True, "evidence": []}]
                   if state.get("is_truncated") is True else [])
    completion = state.get("is_session_completed", payload.get("session_completed", MISSING))
    if completion == MISSING:
        completion = {"completed": True, "terminated": False, "failed": False}.get(payload.get("task_status"), MISSING)
    usage = payload.get("token_usage") or {}
    def token(field):
        value = usage.get(field)
        return value if (type(value) is int and value >= 0) or (type(value) is float and math.isfinite(value) and value >= 0) else MISSING
    counts = tool_counts(calls)
    result = {
        "A1": {"input_tokens": token("input_tokens"), "output_tokens": token("output_tokens")},
        "A2": {"known_characters": sum(len(s["text"]) for s in segments), "absent_body_step_ids": absent},
        "A3": {"tool_calls": len(calls), "tool_kinds": len(counts), "tool_counts": counts},
        "B1": {"segments": segments, "absent_body_step_ids": absent},
        "B2": {"judgment": {"risk_found": "有风险", "no_risk_found": "无风险"}.get(payload.get("audit_decision"), "未完成审计")},
        "B3": {"risks": risks, "first_risk_step": risks[0].get("step_id", risks[0].get("event_ordinal")) if risks else None},
        "C1": {"calls": rendered_calls, "unmatched": unmatched}, "C2": {"markers": repeats}, "C3": {"failures": failures},
        "C4": {"missing_fields": missing, "unmatched": [{k: v for k, v in item.items() if k != "content"} for item in unmatched],
               "truncations": truncations, "completion": completion},
        "C5": {"issues": [{"source_subitem": kind, **item} for kind, items in
                           (("C2", repeats), ("C3", failures), ("C4", missing + unmatched + truncations)) for item in items]},
    }
    for key, value in result.items():
        value["指标"] = NAMES[key]
    return {"analysis_version": VERSION, "report_kind": "ordinary", "results": result}
