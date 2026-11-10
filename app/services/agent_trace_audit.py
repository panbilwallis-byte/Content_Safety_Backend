"""Preserve producer-declared audit results; do not infer risk from test labels."""
from app.services.agent_trace_generation import stable_id


def recorded_audit(record, payload, events, calls):
    audit = record.get("audit")
    payload.update(audit_decision="unknown", risk_level="unknown", risk_types=[])
    if not isinstance(audit, dict):
        return []
    decision = audit.get("decision", "unknown")
    if decision not in {"risk_found", "no_risk_found", "pending", "error", "unknown"}:
        raise ValueError("audit.decision 无效")
    payload["audit_decision"] = decision
    payload["audit_source"] = {"kind": "source_log", "provider": audit.get("provider"), "version": audit.get("version")}
    level = audit.get("risk_level", "unknown")
    if level not in {"high", "medium", "low", "none", "unknown"}:
        raise ValueError("audit.risk_level 无效")
    payload["risk_level"] = level
    evidence = audit.get("evidence", [])
    if not isinstance(evidence, list):
        raise ValueError("audit.evidence 必须为列表")
    raw_events = record.get("events", record.get("messages", []))
    by_idx = {e.get("idx", i): target for i, (e, target) in enumerate(zip(raw_events, events)) if isinstance(e, dict)}
    rows = []
    for i, item in enumerate(evidence):
        if not isinstance(item, dict) or item.get("event_idx") not in by_idx:
            raise ValueError("风险证据 event_idx 未关联实际事件")
        target = by_idx[item["event_idx"]]
        kind = item.get("risk_type")
        if not isinstance(kind, str) or not kind:
            raise ValueError("风险证据缺少 risk_type")
        confidence = item.get("confidence")
        if confidence is not None and (type(confidence) not in {int, float} or not 0 <= confidence <= 1):
            raise ValueError("风险置信度无效")
        severity = item.get("severity", level)
        if severity not in {"high", "medium", "low", "unknown"}:
            raise ValueError("风险证据 severity 无效")
        rows.append({"evidence_id": stable_id("evidence_", target["event_id"], i), "event_id": target["event_id"],
            "risk_type": {"code": kind, "label": item.get("label") or kind},
            "match_pattern": item.get("matched_text") or "源日志未提供匹配片段", "severity": severity,
            "rule_id": item.get("rule_id"), "rule_version": audit.get("version"), "review_status": "pending",
            "confidence": confidence, "confidence_kind": "source_log" if confidence is not None else "unknown",
            "related_call_ids": [c["call_id"] for c in calls if c["event_id"] == target["event_id"]],
            "data_origin": "actual", "detection_source": "external_record"})
        target["risk_level"] = severity
    payload["risk_types"] = list({r["risk_type"]["code"]: r["risk_type"] for r in rows}.values())
    return rows
