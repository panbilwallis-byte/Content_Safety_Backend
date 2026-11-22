"""Preserve producer-declared audit results and add trajectory risk judgments."""
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
    # A message can emit content and several tool-call events. Zip would shift
    # every subsequent source coordinate; use recorded source paths instead.
    by_idx = {}
    for i, source in enumerate(raw_events):
        if not isinstance(source, dict):
            continue
        prefix = f"events[{i}]" if "events" in record else f"messages[{i}]"
        targets = [e for e in events if (e.get("source_ref") or {}).get("source_path", "").split(".")[0] == prefix]
        if len(targets) == 1:
            by_idx[source.get("idx", i)] = targets[0]
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
            "related_call_ids": [c["call_id"] for c in calls if target["event_id"] in {c["event_id"], c.get("return_event_id")}],
            "data_origin": "actual", "detection_source": "external_record"})
        target["risk_level"] = severity
    payload["risk_types"] = list({r["risk_type"]["code"]: r["risk_type"] for r in rows}.values())
    return rows


def has_located_model_findings(payload):
    return any(finding.get("detection_source") == "model_fallback" and finding.get("event_id")
               for finding in (payload.get("rule_findings") or []))


def apply_model_audit(payload, trace, metadata, events, evidence):
    payload["model_audit"] = {k: v for k, v in metadata.items() if k not in {"risk_quote", "source_evidence"}}
    ordinal = metadata.get("risk_event_ordinal")
    event = next((item for item in events if item["ordinal"] == ordinal), None)
    quote, source = metadata.get("risk_quote"), metadata.get("source_evidence")
    if not metadata.get("risk_detected") or event is None or not quote or not source:
        return

    risk_type = {"code": "other", "label": "其他风险"}
    payload["audit_decision"] = "risk_found"
    if payload["risk_level"] == "none":
        payload["risk_level"] = "unknown"
    if all(item["code"] != risk_type["code"] for item in payload["risk_types"]):
        payload["risk_types"].append(risk_type)

    finding = {
        "finding_id": stable_id("finding_", metadata["version"], trace),
        "rule_id": "other", "rule_version": metadata["version"], "risk_type": risk_type,
        "scope": "trace", "category": "security", "label": "模型判定有风险",
        "event_id": None, "event_display_id": None, "call_id": None,
        "severity": "unknown", "outcome": "model_judgment", "evidence_text": "", "source_evidence": [],
        "detection_source": "model_fallback", "model_provider": metadata["provider"], "model_id": metadata["model"],
    }
    payload["rule_findings"].append(finding)
    payload["rule_audit"]["security_count"] += 1
    report = payload.get("competition_report")
    if report:
        report["results"]["B2"]["judgment"] = "有风险"
    from app.services.agent_trace_competition import first_risk

    ref = event.get("source_ref") or {}
    coordinates = {key: ref[key] for key in ("competition_step", "message_index", "call_index") if key in ref}
    step = coordinates.get("competition_step", event["ordinal"])
    finding.update(coordinates, scope="event", event_id=event["event_id"], event_display_id=event["display_id"],
                   call_id=event.get("call_id"), evidence_text=quote, source_evidence=source, source_ref=ref)
    evidence.append({**coordinates, "evidence_id": finding["finding_id"], "event_id": event["event_id"],
                     "risk_type": risk_type, "match_pattern": quote, "severity": "unknown",
                     "rule_id": "other", "rule_version": metadata["version"], "policy_version": metadata["version"],
                     "review_status": "pending", "confidence": None, "confidence_kind": "unknown",
                     "related_call_ids": [event["call_id"]] if event.get("call_id") else [],
                     "data_origin": "actual", "detection_source": "model_fallback", "source_evidence": source})
    payload["risk_steps"] = sorted({*payload["risk_steps"], step})
    payload["first_risk_step"] = payload["risk_steps"][0]
    if report and "competition_step" in ref:
        b3 = report["results"]["B3"]
        model_risk = {"step_id": step, "rule_id": "other", "risk_quote": quote,
                      "event_id": event["event_id"], "call_id": event.get("call_id"), "evidence": source}
        risks = first_risk([*b3["risks"], model_risk])
        b3.update(risks=risks, first_risk_step=risks[0]["step_id"])
