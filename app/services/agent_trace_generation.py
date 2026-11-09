"""Deterministic, explicitly labelled presentation data; never a risk detector."""
from copy import deepcopy
from hashlib import sha256
import json

VERSION = "value_tools_v1"
NOTICE = "价值分析 · 含模拟过程"


def stable_id(prefix, *parts):
    return prefix + sha256("\0".join(map(str, parts)).encode()).hexdigest()[:32]


def dumps(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def generated_sample(task_id, sample, index):
    """Build a coherent recorded projection, or preserve a supplied actual process."""
    trace = stable_id("trc_", task_id, sample["id"])
    scenario = 0 if index == 0 else int(sha256((task_id + str(sample["id"])).encode()).hexdigest()[:8], 16) % 3
    events = []
    labels = ["样本输入", "分析输入准备", "检索调用", "检索返回", "网页调用", "网页返回",
              "模拟审计摘要", "重复抓取", "抓取返回", "接口查询", "查询结果", "导出请求",
              "策略判定", "补充检索", "检索返回", "网页抓取", "抓取返回", "价值评分结果"]
    for n, label in enumerate(labels, 1):
        event_id = stable_id("ev_", trace, n)
        events.append({
            "event_id": event_id, "display_id": f"STEP-{n:02}", "ordinal": n,
            "event_type": "value_result" if n == 18 else "recorded_step",
            "label": label, "description": f"模拟过程：{label}", "relative_time_ms": (n - 1) * 500,
            "risk_level": "unknown", "call_id": None,
            "source_ref": {"source_id": stable_id("src_", task_id), "record_index": index + 1,
                           "source_path": f"samples[{index}].events[{n - 1}]"},
            "source_excerpt": str(sample.get("text", ""))[:2000] if n == 1 else f"模拟事件：{label}",
            "reasoning_summary": "模拟审计摘要，仅用于展示执行过程，不是模型原始推理。",
            "data_origin": "generated", "generation_policy_version": VERSION,
        })
    events[0].update(source_excerpt=str(sample.get("text", "")), data_origin="mixed",
                     field_origins={"source_excerpt": "actual", "relative_time_ms": "generated"})
    events[-1]["source_excerpt"] = dumps(sample)
    events[-1]["field_origins"] = {"source_excerpt": "actual", "relative_time_ms": "generated"}
    events[-1]["data_origin"] = "mixed"
    tools = ["search", "fetch", "fetch", "query", "export", "search", "fetch"]
    steps = [3, 5, 8, 10, 12, 14, 16]
    starts = [0, 210, 690, 1152, 2010, 2010, 5460]
    durations = [210, 480, 462, 403, None, 320, 3125]
    calls = []
    call_ids = [stable_id("call_", trace, i) for i in range(7)]
    for i, (tool, step, start, duration) in enumerate(zip(tools, steps, starts, durations)):
        denied = i == 4 and scenario != 1
        failed = i == 3 and scenario == 0
        if not denied and duration is None:
            duration = 150
        duplicate = i in (2, 5) and scenario != 2
        status = "not_executed" if denied else "failed" if failed else "succeeded"
        args = ({"q": "public report"} if tool == "search" else
                {"url": "https://example.com/public/report"} if tool == "fetch" else
                {"dataset": "demo_dataset", "export_format": "csv"} if tool == "export" else
                {"query": "public report metadata"})
        result = {"code": "POLICY_DENIED" if denied else "UPSTREAM_ERROR" if failed else "OK",
                  "message": "模拟策略拒绝" if denied else "模拟接口错误" if failed else "模拟公开结果"}
        tags = ([{"code": "overreach", "label": "模拟越权"}] if denied else []) + (
            [{"code": "inefficient", "label": "模拟重复调用"}] if duplicate else [])
        requested = ["restricted.export"] if denied else ["public.read"]
        event = events[step - 1]
        return_event = events[step]
        event.update(event_type="tool_call", call_id=call_ids[i])
        return_event.update(event_type="policy_decision" if denied else "tool_result", call_id=call_ids[i])
        event["relative_time_ms"] = start
        return_event["relative_time_ms"] = start if denied else start + duration
        if denied:
            event["risk_level"] = "high"
        calls.append({
            "call_id": call_ids[i], "display_id": f"CALL-{i + 1:02}", "order": i + 1,
            "source_call_id": f"CALL-{i + 1:02}", "attempt_id": stable_id("attempt_", trace, i),
            "parent_call_id": None, "attempt_index": 0, "tool_id": tool, "tool_name": tool,
            "event_id": event["event_id"], "step_display_id": event["display_id"],
            "request_event_id": event["event_id"], "return_event_id": return_event["event_id"],
            "from_participant_id": "agent", "to_participant_id": tool,
            "relative_start_ms": start, "relative_end_ms": None if denied else start + duration,
            "duration_ms": None if denied else duration, "execution_status": status,
            "permission_decision": "denied" if denied else "allowed",
            "granted_scopes": ["public.read"], "requested_scopes": requested,
            "result_code": result["code"], "input_json": dumps(args), "output_json": dumps(result),
            "input_summary": dumps(args), "output_summary": result["message"], "behavior_tags": tags,
            "evidence_chain": [{"event_id": e["event_id"], "display_id": e["display_id"]}
                               for e in (event, return_event)],
            "analysis": {
                "permission_comparison": {"rows": [{"label": "数据访问权限", "granted": ["public.read"],
                    "requested": requested, "comparison": "mismatch" if denied else "match"}],
                    "actual_decision": {"message": "模拟权限拒绝，未执行导出" if denied else "模拟权限检查通过"}},
                "error_attribution": {"message": "模拟策略在执行前拒绝请求" if denied else "模拟接口执行失败"}
                                     if denied or failed else None,
            }, "data_origin": "generated", "generation_policy_version": VERSION,
        })
    groups = [] if scenario == 2 else [
        {"canonical_call_id": call_ids[1], "duplicate_call_ids": [call_ids[2]], "reason": "模拟：相同上下文中重复抓取同一公开页面。"},
        {"canonical_call_id": call_ids[0], "duplicate_call_ids": [call_ids[5]], "reason": "模拟：相同上下文中重复执行检索。"},
    ]
    duration_is_actual = sample.get("duration_ms") is not None
    total_duration = sample["duration_ms"] if duration_is_actual else 8600
    if duration_is_actual:
        if not isinstance(total_duration, (int, float)) or isinstance(total_duration, bool) or total_duration < 0:
            raise ValueError("实际样本耗时无效")
        scale = total_duration / 8600
        for event in events:
            event["relative_time_ms"] = round(event["relative_time_ms"] * scale)
        for call in calls:
            for field in ("relative_start_ms", "relative_end_ms"):
                if call[field] is not None:
                    call[field] = round(call[field] * scale)
            if call["duration_ms"] is not None:
                call["duration_ms"] = call["relative_end_ms"] - call["relative_start_ms"]
    events[1]["relative_time_ms"] = 0
    events[6]["relative_time_ms"] = calls[1]["relative_end_ms"]
    events[-1]["relative_time_ms"] = total_duration
    payload = {
        "duration_ms": total_duration, "audit_decision": "unknown", "risk_level": "unknown",
        "risk_types": [], "event_count": len(events), "call_count": len(calls),
        "source": {"source_id": stable_id("src_", task_id), "format_code": "value_analysis",
                   "format_label": NOTICE, "filename": "系统生成演示记录"},
        "test_category": None, "scenario_code": f"tools_{scenario}", "data_origin": "mixed",
        "field_origins": {"value_result": "actual", "events": "generated", "calls": "generated",
                          "duration_ms": "actual" if duration_is_actual else "generated"},
        "generation_policy_version": VERSION, "inefficiency_groups": groups,
        "token_usage": deepcopy(sample.get("token_usage") if isinstance(sample.get("token_usage"), dict) else
                                {"input_tokens": 1200, "output_tokens": 790, "reasoning_tokens": 1250, "data_origin": "generated"}),
    }
    # When a producer supplies a normalized actual execution, preserve even a true empty list.
    actual = sample.get("execution")
    if isinstance(actual, dict) and isinstance(actual.get("events"), list) and isinstance(actual.get("calls"), list):
        events, calls = deepcopy(actual["events"]), deepcopy(actual["calls"])
        for event in events:
            event.setdefault("data_origin", "actual")
        for call in calls:
            call.setdefault("data_origin", "actual")
        payload.update({k: deepcopy(v) for k, v in actual.items() if k not in {"events", "calls"}})
        payload.update(event_count=len(events), call_count=len(calls), data_origin=actual.get("data_origin", "mixed"),
                       source=deepcopy(actual.get("source") or {"source_id": stable_id("src_", task_id), "format_code": "value_analysis",
                               "format_label": "价值分析 · 实际过程"}),
                       inefficiency_groups=actual.get("inefficiency_groups", []))
        payload["field_origins"].update(events="actual", calls="actual")
        if "duration_ms" in actual:
            payload["field_origins"]["duration_ms"] = "actual"
    relations = [{"edge_id": stable_id("edge_", trace, a["event_id"], b["event_id"]),
                  "from_event_id": a["event_id"], "to_event_id": b["event_id"], "relation": "sequence",
                  "data_origin": "generated" if not actual else "actual"}
                 for a, b in zip(events, events[1:])]
    return trace, payload, events, calls, relations


def validate_process(payload, events, calls):
    event_ids = {e["event_id"] for e in events}
    call_ids = {c["call_id"] for c in calls}
    if len(event_ids) != len(events) or len(call_ids) != len(calls):
        raise ValueError("过程事件或调用标识重复")
    for c in calls:
        if c["event_id"] not in event_ids:
            raise ValueError("工具调用未关联有效事件")
        for key in ("request_event_id", "return_event_id"):
            if c.get(key) and c[key] not in event_ids:
                raise ValueError("调用返回关联的事件不存在")
        if c["execution_status"] not in {"succeeded", "failed", "not_executed", "unknown"}:
            raise ValueError("工具执行状态无效")
        if c["execution_status"] == "not_executed" and c.get("duration_ms") is not None:
            raise ValueError("未执行调用不能包含执行时长")
        if c.get("duration_ms") is not None and c["duration_ms"] < 0:
            raise ValueError("调用时长不能为负数")
    for group in payload.get("inefficiency_groups", []):
        if not {group["canonical_call_id"], *group["duplicate_call_ids"]} <= call_ids:
            raise ValueError("重复调用证据引用不存在")
    if payload.get("duration_ms") is not None and payload["duration_ms"] < 0:
        raise ValueError("样本时长无效")
