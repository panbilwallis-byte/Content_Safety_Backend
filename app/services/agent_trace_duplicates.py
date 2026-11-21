"""C2: evidence-backed unnecessary repetition, shared by cards and reports.

Identical parameters are only a candidate match. A marker needs source context
showing redundancy or a re-read of an unchanged, already observed file. Polling,
verification, changed state and repaired retries take precedence. Unknown tool
effects and missing source context are not assertions of redundancy.
"""
from copy import deepcopy
import json
import re

VERSION = "c2-context-v1"


def canonical(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return value
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def body(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


# These recognize source purposes, not a list of tools allowed to be repeated.
EXCLUDED_PURPOSE = re.compile(
    r"轮询|等待.{0,20}(?:完成|状态|结果)|必要.{0,8}(?:复核|确认)|(?:再次|重新|重复).{0,15}(?:复核|复查|核验|验证)|"
    r"(?:为了|用于|需要|进行).{0,10}(?:复核|复查|核验|验证)|"
    r"(?:复核|复查|核验|验证|确认).{0,8}(?:结果|输出|配置|正确性)|"
    r"修复.{0,20}(?:重试|再次)|(?:状态|数据|文件).{0,12}(?:已变化|已更新|改变后)|"
    r"\bpoll(?:ing)?\b|\bwait(?:ing)? for .{0,30}(?:complet|status|result|finish)|"
    r"\b(?:verify|validate|confirm) (?:the )?(?:result|output|configuration|setting|correctness)|"
    r"\b(?:read|fetch|query|call|check) again.{0,40}(?:verif|confirm|validat)|"
    r"\bdouble[- ]check\b|\brecheck\b|\bafter (?:fix|repair|updat|chang)", re.I)
REDUNDANT_PURPOSE = re.compile(
    r"不必要.{0,12}(?:重复|再次|调用)|无需.{0,12}(?:再次|重读|重复)|重复读取已获取|"
    r"\bunnecessar(?:y|ily)\b.{0,40}(?:repeat|call|read)|"
    r"\bredundant\b.{0,30}(?:call|read|request)|\bno need to (?:read|fetch|query|call).*again", re.I)
POLLING = re.compile(r"(?:^|[_.-])(?:poll|wait|watch|status|progress)(?:$|[_.-])|轮询|进度", re.I)
READ_ACTION = re.compile(r"(?:^|[_.-])(?:read|fetch|get|open|load|inspect|view|search|grep|list|ls)(?:$|[_.-])", re.I)
MUTATION = re.compile(r"(?:^|[_.-])(?:write|edit|patch|update|delete|remove|create|save|put|set|insert|repair|fix)(?:$|[_.-])", re.I)
FAILURE = re.compile(r"(?im)^(?:.*command not found|.*No such file or directory|.*Permission denied|"
                     r"curl: \([0-9]+\)|Error:|Traceback \(most recent call last\):|.*timed out)")


def arguments(call):
    value = call.get("input_json")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def file_read(call):
    """Return a recorded static file target, never inspect the audit host."""
    args = arguments(call)
    command = args.get("command", args.get("cmd"))
    if isinstance(command, str):
        from app.services.agent_trace_security import shell_commands, file_operands
        groups = shell_commands(command)
        if len(groups) != 1 or re.search(r"[$`]|(?:^|[^<])>{1,2}|/proc/|/sys/|/dev/", command):
            return None
        tokens = groups[0]
        program = tokens[0].rsplit("/", 1)[-1]
        # Recognize read effects for shell source, not the wrapper tool's name.
        if program not in {"cat", "sed", "head", "tail", "grep", "rg", "ls", "stat", "wc"}:
            return None
        if (program == "sed" and any(x == "-i" or x.startswith("--in-place") or
                                    (x.startswith("-") and "i" in x[1:]) for x in tokens[1:])):
            return None
        if program == "tail" and any(x in {"-f", "-F", "--follow"} for x in tokens[1:]):
            return None
        paths = file_operands(program, tokens[1:])
        if not paths or any(re.search(r"[$`*?{}]|://|^/(?:proc|sys|dev)/", p) for p in paths):
            return None
        return tuple(paths)
    if not READ_ACTION.search(call.get("tool_name") or "") or MUTATION.search(call.get("tool_name") or ""):
        return None
    path = next((args[k] for k in ("filePath", "file_path", "path", "filename") if isinstance(args.get(k), str)), None)
    if not path or re.search(r"[$`*?{}]|://|^/(?:proc|sys|dev)/", path):
        return None
    return (path,)


def failed(call):
    return (call.get("execution_status") in {"failed", "not_executed"}
            or bool(call.get("execution_failures"))
            or bool(FAILURE.search(body(call.get("output_json")))))


def contexts(calls, record):
    """Attach source coordinates by original call order, never guessed IDs."""
    if not isinstance(record, dict):
        return [{} for _ in calls]
    source = []
    messages = record.get("messages")
    if isinstance(messages, list):
        from app.services.agent_trace_competition import parse
        try:
            parsed = parse(record)[0]
        except (ValueError, TypeError, KeyError):
            return [{} for _ in calls]
        for call in parsed:
            i = call["message_index"]
            # Only natural-language assistant/user context; returned documents
            # and command strings cannot grant themselves an exclusion.
            start = min(source[-1]["message_index"] + 1, i) if source else 0
            purpose = [{"message_index": j, "quote": m[field]}
                       for j, m in enumerate(messages[start:i + 1], start)
                       if m.get("role") in {"assistant", "user"}
                       for field in ("reasoning_content", "reasoning", "content")
                       if isinstance(m.get(field), str) and m[field]]
            user_index = next((j for j in range(i, -1, -1) if messages[j].get("role") == "user"), None)
            if user_index is not None and user_index < start and isinstance(messages[user_index].get("content"), str):
                purpose.append({"message_index": user_index, "quote": messages[user_index]["content"]})
            source.append({**call, "purpose": purpose})
    elif isinstance(record.get("runs"), list):
        for i, run in enumerate(record["runs"]):
            if run.get("run_type") == "tool":
                source.append({"tool_name": run.get("name"), "arguments": run.get("inputs"),
                               "source_path": f"runs[{i}]", "purpose": []})
    elif isinstance(record.get("events"), list):
        previous = -1
        for i, event in enumerate(record["events"]):
            if event.get("type") != "tool_call":
                continue
            purpose = [{"source_path": f"events[{j}]", "quote": e.get("reasoning") or e.get("content")}
                       for j, e in enumerate(record["events"][previous + 1:i + 1], previous + 1)
                       if e.get("type") in {"thought", "user", "tool_call"}
                       and isinstance(e.get("reasoning") or (e.get("content") if e.get("type") != "tool_call" else None), str)]
            source.append({"tool_name": event.get("tool"), "arguments": event.get("tool_args", {}),
                           "source_path": f"events[{i}]", "purpose": purpose})
            previous = i
    if len(source) != len(calls) or any(s.get("tool_name") != c.get("tool_name")
            or canonical(s.get("arguments")) != canonical(c.get("input_json")) for s, c in zip(source, calls)):
        return [{} for _ in calls]
    return source


def duplicate_pairs(calls, record=None):
    """Return prior/current indices with evidence; exclusions always win."""
    source = contexts(calls, record)
    history, pairs = {}, []
    for index, call in enumerate(calls):
        if not call.get("return_event_id") or call.get("input_json") is None or call.get("output_json") is None:
            continue
        key = (call.get("tool_name"), canonical(call["input_json"]))
        previous_index = history.get(key)
        history[key] = index
        if previous_index is None:
            continue
        previous = calls[previous_index]
        purpose = source[index].get("purpose", [])
        context = "\n".join(p["quote"] for p in purpose)
        # A source retry label is supporting evidence of necessity, never
        # supporting evidence of unnecessary repetition.
        if (POLLING.search(call.get("tool_name") or "") or EXCLUDED_PURPOSE.search(context)
                or any(arguments(call).get(k) is True or arguments(call).get(k) in ("true", "enabled")
                       for k in ("poll", "polling", "follow", "watch", "wait_for_completion"))
                or any(t.get("code") == "retry" for t in call.get("behavior_tags", []))):
            continue
        between = calls[previous_index + 1:index]
        # Unknown effects may change state. Do not infer unchanged state through
        # an opaque operation; observed read-only calls need not erase history.
        explicit = next((p for p in purpose if REDUNDANT_PURPOSE.search(p["quote"])), None)
        same_result = canonical(previous["output_json"]) == canonical(call["output_json"])
        static_read = (file_read(call) is not None and same_result and not failed(previous) and not failed(call)
                       and all(file_read(c) is not None for c in between))
        # Positive intent evidence also works for arbitrary tool types. Changes
        # explicitly made between calls still invalidate that justification.
        if explicit and any(MUTATION.search(c.get("tool_name") or "") or
                            re.search(r"\b(?:rm|mv|cp|touch|mkdir|git\s+(?:checkout|reset|pull)|sed\s+-i)\b|>{1,2}",
                                      str(arguments(c).get("command", arguments(c).get("cmd", "")))) for c in between):
            continue
        # For general tools equality alone is insufficient. Static reads provide
        # additional evidence: same recorded file, contents already available,
        # and no intervening operation with unknown or changing effects.
        if not explicit and not static_read:
            continue
        reason = "原文明确说明再次调用不必要" if explicit else "已获取同一文件内容，期间无已知状态变化或必要复核依据"
        pairs.append({"previous_index": previous_index, "index": index, "reason": reason,
                      "context_evidence": [explicit] if explicit else []})
    return pairs


def duplicate_groups(calls, record=None):
    groups, roots = {}, {}
    for pair in duplicate_pairs(calls, record):
        previous, call = calls[pair["previous_index"]], calls[pair["index"]]
        root = roots.get(previous["call_id"], previous["call_id"])
        roots[call["call_id"]] = root
        group = groups.setdefault(root, {"canonical_call_id": root,
            "duplicate_call_ids": [], "reason": pair["reason"], "pairs": []})
        group["duplicate_call_ids"].append(call["call_id"])
        group["pairs"].append({"call_id": call["call_id"], "previous_call_id": previous["call_id"],
                               "context_evidence": pair["context_evidence"]})
    return list(groups.values())


def competition_markers(calls, record):
    """C2 and C5 use exactly the same decisions as normalized tool cards."""
    from app.services.agent_trace_call_status import project_calls as project_statuses
    normalized = []
    for c in calls:
        returns = c["returns"]
        value = "\n".join(body(r["content"]) for r in returns)
        normalized.append({"call_id": str(c["step_id"]), "tool_name": c["tool_name"],
                           "input_json": c["arguments"], "output_json": value if returns else None,
                           "return_event_id": str(returns[0]["message_index"]) if returns else None,
                           "execution_status": "unknown"})
    normalized = project_statuses(normalized, record)
    def evidence(c):
        return [{"step_id": c["step_id"], "message_index": c["message_index"], "quote": body(c["arguments"])}] + [
            {"step_id": c["step_id"], "message_index": r["message_index"], "quote": body(r["content"])} for r in c["returns"]]
    markers = []
    for pair in duplicate_pairs(normalized, record):
        previous, current = calls[pair["previous_index"]], calls[pair["index"]]
        markers.append({"step_id": current["step_id"], "previous_step_id": previous["step_id"],
                        "evidence": evidence(previous) + evidence(current) + pair["context_evidence"]})
    return markers


def project_calls(calls, record=None):
    groups = duplicate_groups(calls, record)
    duplicates = {cid for group in groups for cid in group["duplicate_call_ids"]}
    output = deepcopy(calls)
    for call in output:
        call["behavior_tags"] = [t for t in call.get("behavior_tags", []) if t.get("code") not in {"duplicate", "inefficient"}]
        if call["call_id"] in duplicates:
            call["behavior_tags"].append({"code": "inefficient", "label": "C2 不必要的重复调用"})
    return output, groups


def pair_text(previous, current, reason, evidence):
    """Keep source fragments structured so display redaction can inspect JSON."""
    return json.dumps({"reason": reason,
        "previous": {"arguments": previous.get("input_json"), "result": previous.get("output_json")},
        "current": {"arguments": current.get("input_json"), "result": current.get("output_json")},
        "context": evidence}, ensure_ascii=False)


def project_audit(payload, calls, record=None):
    """Refresh C2 presentation on frozen snapshots without database writes."""
    value = deepcopy(payload)
    findings = value.get("rule_findings")
    if not isinstance(findings, list):
        return value
    from app.services.agent_trace_generation import stable_id
    findings = [f for f in findings if f.get("rule_id") not in {"recorded_duplicate", "repeated_request"}]
    for pair in duplicate_pairs(calls, record):
        previous, current = calls[pair["previous_index"]], calls[pair["index"]]
        findings.append({"finding_id": stable_id("finding_", VERSION, current["call_id"]),
            "rule_id": "repeated_request", "rule_version": VERSION, "category": "efficiency",
            "label": "C2 不必要的重复调用", "call_id": current["call_id"], "event_id": current["event_id"],
            "event_display_id": current.get("step_display_id"), "severity": "low", "outcome": "recorded",
            "evidence_text": pair_text(previous, current, pair["reason"], pair["context_evidence"]),
            "source_ref": {}, "detection_source": "backend_rule"})
    if findings == value["rule_findings"]:
        return value
    value["rule_findings"] = findings
    if isinstance(value.get("rule_audit"), dict):
        value["rule_audit"]["efficiency_count"] = sum(f.get("category") == "efficiency" for f in findings)
        value["rule_audit"]["duplicate_policy_version"] = VERSION
    return value
