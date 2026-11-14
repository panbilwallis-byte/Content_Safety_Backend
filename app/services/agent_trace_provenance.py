"""Evidence-backed risk paths. Content matches are candidates, never causal verdicts.

Built from one published sample; no model calls or writes to historical snapshots.
"""
from collections import defaultdict
from copy import deepcopy
import re

VERSION = "risk-provenance-v1"
LIMIT = 50
WINDOW = 32
INTENTS = {
    "export": ("导出", re.compile(r"导出|\bexport(?:_\w+)?\b", re.I)),
    "send": ("发送/外发", re.compile(r"发送|外发|发邮件|\bsend(?:_\w+)?\b|\bemail\b", re.I)),
    "read": ("读取/查询", re.compile(r"读取|查询|检索|\b(?:read|query|fetch|search)(?:_\w+)?\b", re.I)),
    "delete": ("删除", re.compile(r"删除|\b(?:delete|remove)(?:_\w+)?\b", re.I)),
}


def _intents(text):
    return {code for code, (_, pattern) in INTENTS.items() if pattern.search(str(text)[:200000])}


def _excerpt(text, match=None):
    text = str(text or "")
    start = max(0, match.start() - 60) if match else 0
    return ("…" if start else "") + text[start:start + 420] + ("…" if start + 420 < len(text) else "")


def _risk_title(items, call):
    rules = {e.get("rule_id") for e in items}
    names = {"unauthorized_request": "越权请求", "prohibited_request": "违规请求",
             "external_instruction": "外部内容中有可疑指令"}
    labels = [names.get(e.get("rule_id"), e["risk_type"]["label"]) for e in items
              if not ("unauthorized_request" in rules and e.get("rule_id") == "prohibited_request")]
    title = "、".join(dict.fromkeys(labels))
    return title + ("，已拦截" if call and call.get("execution_status") == "not_executed" and "已拦截" not in title else "")


def build_risk_provenance(events, calls, evidence, *, data_origin="actual", sanitize=lambda value: value):
    by_id = {e["event_id"]: e for e in events}
    positions = {e["event_id"]: i for i, e in enumerate(events)}
    by_event = {eid: c for c in calls for eid in (c.get("event_id"), c.get("return_event_id")) if eid}
    grouped = defaultdict(list)
    for item in evidence:
        if item.get("review_status") == "dismissed" or item.get("event_id") not in by_id:
            continue
        eid = item["event_id"]
        call = by_event.get(eid)
        # Exposure to malicious content is not evidence that the preceding fetch was dangerous.
        if item.get("rule_id") != "external_instruction" and call:
            eid = call["event_id"]
        grouped[eid].append(item)
    external_ids = {e["event_id"] for e in evidence
                    if e.get("rule_id") == "external_instruction" and e.get("review_status") != "dismissed"}
    chains = []
    for eid, items in sorted(grouped.items(), key=lambda pair: positions[pair[0]])[:LIMIT]:
        event = by_id[eid]
        exposure = all(e.get("rule_id") == "external_instruction" for e in items)
        call = None if exposure else by_event.get(eid)
        nodes, edges = [], []

        def node(e, kind, title, text=None, pattern=None):
            # Redact complete JSON before clipping: a truncated JSON string cannot be safely parsed.
            content = str(sanitize(text if text is not None else e.get("source_excerpt", "")))
            n = {"id": e["event_id"], "event_id": e["event_id"], "display_id": e["display_id"],
                 "kind": kind, "label": title, "excerpt": _excerpt(content, pattern.search(content) if pattern else None),
                 "source_ref": deepcopy(e.get("source_ref") or {})}
            if not any(old["id"] == n["id"] for old in nodes):
                nodes.append(n)
            return n["id"]

        def edge(a, b, relation, label, basis, status="recorded"):
            edges.append({"from_id": a, "to_id": b, "relation": relation, "label": label,
                          "status": status, "basis": basis})

        request_id = node(event, "external_content" if exposure else "risk_request" if call else "risk_event",
                          "可疑外部指令" if exposure else (
                              "越权调用" if any(e.get("rule_id") == "unauthorized_request" for e in items) else "风险调用") if call else "风险步骤",
                          call.get("input_json") if call else None)
        if call:
            wanted = _intents(str(call.get("tool_name", "")) + " " + str(call.get("input_json", "")))
            prior = events[max(0, positions[eid] - WINDOW):positions[eid]]
            # One closest candidate per origin; adjacency by itself never creates a link.
            seen = set()
            for source in reversed(prior):
                kind = "external_content" if source["event_id"] in external_ids else (
                    "task_instruction" if source.get("role") == "user" or source.get("event_type") == "user" else None)
                if not kind or kind in seen:
                    continue
                shared = wanted & _intents(source.get("source_excerpt", ""))
                if not shared:
                    continue
                seen.add(kind)
                labels = "、".join(INTENTS[k][0] for k in sorted(shared))
                pattern = INTENTS[sorted(shared)[0]][1]
                source_id = node(source, kind, "可疑外部指令" if kind == "external_content" else "可能相关的指令",
                                 pattern=pattern)
                # Only show a producer-recorded reasoning summary, never a synthesized thought.
                thought = next((p for p in reversed(prior) if positions[p["event_id"]] > positions[source_id]
                                and p.get("reasoning_provenance") == "source_log"
                                and wanted & _intents(p.get("reasoning_summary", ""))), None)
                if thought:
                    tid = node(thought, "reasoning", "推理记录", thought["reasoning_summary"])
                    edge(source_id, tid, "content_match", "可能相关",
                         "这条指令和推理记录提到了相同的操作，但还不能确定推理是否受到了它的影响。", "candidate")
                    edge(tid, request_id, "content_match", "可能相关",
                         "推理记录和工具请求提到了相同的操作，但还不能确定两者的影响关系。", "candidate")
                else:
                    edge(source_id, request_id, "content_match", "可能相关",
                         f"这条指令和工具请求都提到了“{labels}”。还需核实是否由这条指令引发了请求。", "candidate")
            returned = by_id.get(call.get("return_event_id"))
            if returned:
                rid = node(returned, "tool_result", "已拦截，未执行" if call.get("execution_status") == "not_executed" else "工具返回",
                           call.get("output_json"))
                result_text = {"not_executed": "，记录显示请求已被拦截，工具未执行",
                               "succeeded": "，记录显示工具执行成功", "failed": "，记录显示工具执行失败"}.get(call.get("execution_status"), "")
                edge(request_id, rid, "call_result", "对应的返回结果",
                     f"{returned['display_id']} 是 {event['display_id']} 这次调用的返回结果{result_text}。"
                     f"调用编号：{call.get('display_id') or call['call_id']}。")
        nodes.sort(key=lambda n: positions[n["event_id"]])
        gaps = []
        if call and not any(e["status"] == "candidate" for e in edges):
            gaps.append("现有记录中没有找到可能相关的先前指令，暂时无法确定请求的来源。")
        if call and not call.get("return_event_id"):
            gaps.append("缺少这次调用的返回记录，暂时不知道它是否执行或被拦截。")
        if exposure:
            gaps.append("外部内容中有可疑指令，但没有证据表明智能体照做了。")
        if not call and not exposure:
            gaps.append("这条风险记录没有关联到具体工具调用，目前只能查看该步骤的内容。")
        chains.append({"chain_id": eid, "risk_event_id": eid, "risk_display_id": event["display_id"],
            "title": _risk_title(items, call),
            "evidence_ids": [e["evidence_id"] for e in items],
            "risk_evidence": [_excerpt(sanitize(e.get("match_pattern"))) for e in items],
            "call_id": call["call_id"] if call else None,
            "outcome": call.get("execution_status", "unknown") if call else "exposure_only" if exposure else "unknown",
            "nodes": nodes, "edges": edges, "gaps": gaps,
            "attribution_status": "pending_review" if any(e["status"] == "candidate" for e in edges) else "insufficient_evidence"})
    return {"version": VERSION, "chains": chains, "total": len(grouped), "complete": len(grouped) <= LIMIT,
            "data_origin": data_origin,
            "notice": "虚线：可能相关，还需核实。实线：同一次工具调用的请求和返回。点击节点查看原文，点击箭头查看说明。"}
