"""Adapter for the external agent's events/tool_events JSONL contract."""
from copy import deepcopy
import json

CODE = "agent_trajectory"
LABEL = "智能体轨迹 JSONL"
EXAMPLE = {
    "sample_id": "AGT-001", "language": "zh", "topic": "正常任务",
    "duration_ms": 1200, "outcome": "success",
    "tokens": {"input": 100, "reasoning": 20, "output": 30, "total": 150, "estimated": False},
    "events": [
        {"idx": 0, "step_id": "STEP-00", "type": "user", "content": "查询公开报告", "ts_ms": 0},
        {"idx": 1, "step_id": "STEP-01", "type": "thought", "content": "准备查询", "reasoning": "需要读取报告", "ts_ms": 100},
        {"idx": 2, "step_id": "STEP-01", "type": "tool_call", "tool": "fetch_page", "tool_args": {"url": "https://example.com/report"}, "ts_ms": 200},
        {"idx": 3, "step_id": "STEP-01", "type": "tool_result", "tool": "fetch_page", "content": "公开报告正文", "tool_status": "success", "latency_ms": 100, "ts_ms": 200},
        {"idx": 4, "step_id": "STEP-02", "type": "final", "content": "报告摘要", "ts_ms": 1200},
    ],
    "tool_events": [{"step_id": "STEP-01", "tool": "fetch_page", "status": "success", "latency_ms": 100}],
}


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None


def normalize_trajectory(record, file_id, index, raw, line, normalize):
    events = record.get("events")
    if len(str(record.get("sample_id", ""))) > 255:
        raise ValueError("sample_id 最长255字符")
    if not record.get("sample_id") or not isinstance(events, list) or not events:
        raise ValueError("智能体轨迹缺少 sample_id 或 events")
    if not all(isinstance(e, dict) and type(e.get("idx")) is int for e in events):
        raise ValueError("事件缺少整数 idx")
    if len({e["idx"] for e in events}) != len(events):
        raise ValueError("事件 idx 重复")
    if [e["idx"] for e in events] != sorted(e["idx"] for e in events):
        raise ValueError("事件 idx 未按执行顺序排列")
    details = record.get("tool_events", [])
    if not isinstance(details, list) or not all(isinstance(t, dict) for t in details):
        raise ValueError("tool_events 必须为对象列表")
    def key(e):
        return (e.get("step_id"), e.get("tool"))
    by_key = {key(t): t for t in details}
    if len(by_key) != len(details):
        raise ValueError("tool_events 中的步骤和工具关联重复")
    messages = []
    request_keys = set()
    for e in events:
        kind = e.get("type")
        if kind not in {"user", "thought", "tool_call", "tool_result", "final", "error", "system"}:
            raise ValueError("不支持的智能体事件类型")
        if kind in {"tool_call", "tool_result"}:
            if not e.get("step_id") or not e.get("tool"):
                raise ValueError("工具事件缺少 step_id 或 tool")
            source_id = json.dumps(key(e), ensure_ascii=False)
            if kind == "tool_call":
                if key(e) in request_keys:
                    raise ValueError("工具请求关联重复")
                request_keys.add(key(e))
                messages.append({"role": "assistant", "tool_calls": [{"id": source_id,
                    "function": {"name": e["tool"], "arguments": json.dumps(e.get("tool_args", {}), ensure_ascii=False)}}]})
            else:
                messages.append({"role": "tool", "tool_call_id": source_id, "content": e.get("content", "")})
        else:
            messages.append({"role": kind if kind in {"user", "system"} else "assistant", "content": e.get("content") or ""})
    if set(by_key) - request_keys:
        raise ValueError("tool_events 包含未关联的工具请求")
    tokens = record.get("tokens") or {}
    if not isinstance(tokens, dict):
        raise ValueError("tokens 必须为对象")
    converted = {"messages": messages, "language": record.get("language", "unknown"),
        "duration_ms": record.get("duration_ms"), "usage": {
            "input_tokens": tokens.get("input"), "reasoning_tokens": tokens.get("reasoning"),
            "output_tokens": tokens.get("output")}, "task_status": {
                "success": "completed", "terminated": "terminated", "error": "failed", "failed": "failed"
            }.get(record.get("outcome"), "unknown")}
    result = normalize(converted, file_id, index, raw, line, "openai_messages")
    execution = result["execution"]
    execution["source"].update(format_code=CODE, format_label=LABEL)
    execution["token_usage"].update(estimated=tokens.get("estimated") is True,
        output_includes_reasoning=False, total_tokens=tokens.get("total"))
    categories = {"提示注入": "prompt_injection", "隐私泄露": "privacy_leak", "工具滥用": "tool_misuse",
        "越权访问": "unauthorized_access", "有害内容": "harmful_content", "正常任务": "normal", "其他": "other"}
    execution.update(test_category=categories.get(record.get("topic"), "unknown"),
        original_sample_id=str(record["sample_id"]), model=record.get("model"), provider=record.get("provider"),
        started_at=record.get("started_at"), declared_source=record.get("source"),
        error=record.get("error"), origin_type="external_import", decision_rounds=record.get("steps"), retry_count=record.get("retry_count", 0),
        duplicate_count=record.get("duplicate_count", 0), task_outcome_source="source_log")
    calls = iter(execution["calls"])
    call_by_key = {}
    labels = {"user": "用户输入", "thought": "已记录推理", "tool_call": "工具调用", "tool_result": "工具返回", "final": "最终回答", "error": "运行错误", "system": "系统消息"}
    for j, (source, target) in enumerate(zip(events, execution["events"])):
        target.update(event_type=source["type"], label=labels[source["type"]],
            relative_time_ms=number(source.get("ts_ms")), source_step_id=source.get("step_id"),
            reasoning_summary=source.get("reasoning") or "源日志未记录推理摘要",
            reasoning_provenance="source_log" if source.get("reasoning") else "not_provided",
            token_usage=deepcopy(source.get("tokens")))
        target["source_ref"]["source_path"] = f"events[{j}]"
        if source["type"] == "tool_call":
            c = next(calls)
            call_by_key[key(source)] = c
            c["relative_start_ms"] = number(source.get("ts_ms"))
        elif source["type"] == "tool_result" and key(source) in call_by_key:
            c = call_by_key[key(source)]
            d = by_key.get(key(source), {})
            try:
                body = json.loads(source.get("content", ""))
            except (ValueError, TypeError):
                body = {}
            if not isinstance(body, dict):
                body = {}
            status = source.get("tool_status") or d.get("status") or body.get("status")
            c.update(execution_status={"success": "succeeded", "error": "failed", "blocked": "not_executed"}.get(status, "unknown"),
                result_code=d.get("error_code") or body.get("error_code") or ("OK" if status == "success" else "NOT_RECORDED"),
                permission_decision="denied" if status == "blocked" else "unknown",
                recorded_policy=source.get("policy") or d.get("policy"))
            for field in ("granted_scopes", "requested_scopes"):
                scopes = d.get(field, source.get(field, []))
                if not isinstance(scopes, list) or not all(isinstance(v, str) for v in scopes):
                    raise ValueError("工具权限范围必须为字符串列表")
                c[field] = scopes
            latency = number(source.get("latency_ms", d.get("latency_ms")))
            c["duration_ms"] = None if status == "blocked" else latency
            c["policy_duration_ms"] = latency if status == "blocked" else None
            start = c["relative_start_ms"]
            c["relative_end_ms"] = start + latency if start is not None and latency is not None else None
            c["behavior_tags"] = [{"code": code, "label": label} for flag, code, label in
                [("duplicate", "duplicate", "重复请求"), ("retry", "retry", "重试")]
                if source.get(flag) or d.get(flag)]
            if status == "blocked":
                c["analysis"]["permission_comparison"]["actual_decision"]["message"] = "源日志记录策略拒绝；完整授权依据未提供"
            if status == "error":
                c["analysis"]["error_attribution"] = {"message": f"源日志记录工具错误：{c['result_code']}"}
    from app.services.agent_trace_audit import recorded_audit
    recorded_audit(record, execution, execution["events"], execution["calls"])
    result["source_record"] = record
    return result
