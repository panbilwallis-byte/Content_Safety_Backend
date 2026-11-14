"""Read-only explanations derived from recorded tool outcomes and audit findings."""
from copy import deepcopy
import json


def outcome_reason(call):
    analysis = call.get("analysis") or {}
    decision = (analysis.get("permission_comparison") or {}).get("actual_decision") or {}
    try:
        body = json.loads(call.get("output_json") or "null")
    except (ValueError, TypeError):
        body = None
    body = body if isinstance(body, dict) else {}
    detail = body.get("data") if isinstance(body.get("data"), dict) else {}
    recorded = (analysis.get("error_attribution") or {}).get("message")
    code = call.get("result_code")
    if call.get("permission_decision") == "denied" or code == "POLICY_DENIED":
        message = decision.get("message")
        if message in {None, "", "源日志没有记录权限判定", "权限判定未完成"}:
            message = detail.get("message") or body.get("message") or recorded
        if not message:
            granted = "、".join(call.get("granted_scopes") or []) or "未提供"
            requested = "、".join(call.get("requested_scopes") or []) or "未提供"
            message = f"策略拒绝请求；授权范围：{granted}；请求权限：{requested}。"
        # A denial alone does not prove whether an external action already ran.
        if call.get("execution_status") == "not_executed":
            message += "（本次工具未执行）"
        return {"kind": "policy_denied", "message": message}
    status = call.get("execution_status")
    if status == "succeeded":
        return {"kind": "succeeded", "message": "调用执行成功"}
    if status == "not_executed":
        return {"kind": "not_executed", "message": call.get("non_execution_reason") or recorded or "工具未执行，源日志未提供原因"}
    if status == "failed":
        message = recorded or detail.get("message") or body.get("message") or "源日志未提供原因"
        if code and code != "NOT_RECORDED" and code not in message:
            message = f"{message}（错误码：{code}）"
        return {"kind": "failed", "message": message}
    return {"kind": "unknown", "message": recorded or "源日志未提供原因"}


def explain_calls(calls):
    result = deepcopy(calls)
    for call in result:
        call.setdefault("analysis", {})["outcome_reason"] = outcome_reason(call)
    return result


def efficiency_groups(payload, calls):
    """Merge legacy evidence and current findings without declaring retries inefficient."""
    by_id = {c["call_id"]: c for c in calls}
    groups, covered = [], set()
    for original in payload.get("inefficiency_groups", []):
        canonical = original.get("canonical_call_id")
        if canonical not in by_id:
            continue
        duplicates = list(dict.fromkeys(c for c in original.get("duplicate_call_ids", [])
                                       if c in by_id and c != canonical and c not in covered))
        if not duplicates:
            continue
        group = deepcopy(original)
        group.update(duplicate_call_ids=duplicates, kind=original.get("kind", "inefficient"))
        groups.append(group)
        covered.update(duplicates)

    # Pair only equal tool names and parsed arguments, retaining the previous call ID.
    previous, preceding = {}, {}
    for call in calls:
        try:
            args = json.loads(call.get("input_json", "null"))
            args = json.dumps(args, sort_keys=True, ensure_ascii=False)
        except (ValueError, TypeError):
            args = str(call.get("input_json", ""))
        key = (call.get("tool_name"), args)
        preceding[call["call_id"]] = previous.get(key)
        previous[key] = call["call_id"]

    findings = {}
    priority = {"recorded_retry": 3, "recorded_duplicate": 2, "repeated_request": 1}
    for finding in payload.get("rule_findings", []):
        cid, rule = finding.get("call_id"), finding.get("rule_id")
        if cid not in by_id or cid in covered or rule not in priority or finding.get("category") != "efficiency":
            continue
        if cid not in findings or priority[rule] > priority[findings[cid]["rule_id"]]:
            findings[cid] = finding
    for cid, finding in findings.items():
        retry = finding["rule_id"] == "recorded_retry"
        canonical = by_id[cid].get("parent_call_id") if retry else preceding[cid]
        if canonical not in by_id or canonical == cid:
            canonical = None
        reason = ("源日志记录重试；是否必要需结合前次结果核查，不直接判定低效。" if retry else
                  "相同工具和参数再次请求，待结合上下文核查，不直接判定低效。" if canonical else
                  "源日志记录重复请求，未提供可关联的前次调用；待核查。")
        if finding.get("evidence_text"):
            reason += "依据：" + str(finding["evidence_text"])
        groups.append({"canonical_call_id": canonical or cid, "duplicate_call_ids": [cid] if canonical else [],
                       "kind": "retry" if retry else "duplicate", "reason": reason})
    return groups
