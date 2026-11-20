"""Read-only OpenAI-message audit. Source text is data, never executable instructions.

Rules deliberately require recorded evidence; these are not a general semantic judge.
"""
from collections import Counter
from copy import deepcopy
import json
import re

VERSION = "competition-v4"
MISSING = "未提供"
NAMES = {
    "A1": "数据统计指标可视化①：输入、输出token数",
    "A2": "数据统计指标可视化②：思考内容字符总量",
    "A3": "数据统计指标可视化③：工具调用次数与种类",
    "A4": "数据统计指标可视化④：多条轨迹展示与工具调用汇总",
    "B1": "思维链可视化质量①：按原始顺序展示完整思考内容",
    "B2": "思维链可视化质量②：判断轨迹是否有风险",
    "B3": "思维链可视化质量③：首次风险行为步骤、片段和原文证据",
    "C1": "工具调用可视化质量①：工具名称、参数及对应返回",
    "C2": "工具调用可视化质量②：识别不必要的重复调用",
    "C3": "工具调用可视化质量③：执行失败及错误类型",
    "C4": "工具调用可视化质量④：完整性检查与会话完成状态",
    "C5": "工具调用可视化质量⑤：问题所在记录与原文",
}


def text(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def first_risk(risks):
    """Project all findings to one earliest scoring step, keeping its evidence."""
    located = [r for r in risks if type(r.get("step_id")) is int and r["step_id"] > 0]
    if not located:
        return []
    step = min(r["step_id"] for r in located)
    earliest = [r for r in located if r["step_id"] == step]
    result = deepcopy(earliest[0])
    result["evidence"] = []
    for risk in earliest:
        for item in risk.get("evidence", []):
            if item not in result["evidence"]:
                result["evidence"].append(deepcopy(item))
    return [result]


def execution_failures(call, command):
    """Classify explicit runtime diagnostics, retaining their exact return location."""
    from app.services.agent_trace_security import shell_commands
    downloads = False
    for tokens in shell_commands(command):
        if tokens[0].rsplit("/", 1)[-1] != "curl":
            continue
        for i, token in enumerate(tokens[1:], 1):
            if token in {"-O", "--remote-name"}:
                downloads = True
            if token in {"-o", "--output"} and i + 1 < len(tokens):
                downloads |= tokens[i + 1] not in {"/dev/null", "-"}
            elif token.startswith("--output="):
                downloads |= token.split("=", 1)[1] not in {"/dev/null", "-"}
    rules = [
        (r"^User aborted the command$", "执行被用户中止"),
        (r"^curl:\s*\(23\).*Failure writing output", "输出写入失败"),
        (r"^curl:\s*\(28\)", "下载超时" if downloads else "网络请求超时"),
        (r"^NO NET \(exit 28\)$", "网络请求超时"),
        (r"^Command exited with code 124$", "执行超时"),
        (r"^(?:/usr)?(?:/bin/)?(?:ba)?sh:.*command not found", "命令不存在"),
        (r"^(?:/usr)?(?:/bin/)?(?:ba)?sh:.*No such file|^Error:.*(?:403|404)", "命令或请求失败"),
        (r"^(?:tool|command|operation)\b.*timed out|^execution\b.*timeout", "工具超时"),
        (r"^Traceback \(most recent call last\):$", "运行异常"),
    ]
    found = {}
    for returned in call["returns"]:
        output = text(returned["content"])
        fenced = False
        for line in output.splitlines():
            if line.lstrip().startswith("```"):
                fenced = not fenced
                continue
            if fenced:
                continue
            for pattern, error in rules:
                if error == "运行异常" and "blocked for fallback test" in output:
                    continue
                if re.search(pattern, line.strip(), re.I):
                    item = {"step_id": call["step_id"], "message_index": returned["message_index"], "quote": line}
                    evidence = found.setdefault(error, [])
                    if item not in evidence:
                        evidence.append(item)
                    break
    # Nonzero exit codes alone (including requested target crashes) are insufficient.
    return [{"step_id": call["step_id"], "error_type": error, "evidence": evidence}
            for error, evidence in found.items()]


def parse(record):
    """Number assistant actions, pairing returns only inside their reply range."""
    messages = record.get("messages")
    if not isinstance(messages, list):
        raise ValueError("messages 必须为数组")
    calls, segments, absent, unmatched, missing = [], [], [], [], []
    locations, pending = {}, {}
    step = 0
    for i, m in enumerate(messages):
        if not isinstance(m, dict) or not isinstance(m.get("role"), str):
            raise ValueError(f"messages[{i}] 缺少消息角色")
        role = m["role"]
        if role == "assistant":
            pending = {}  # A later reply starts a new call-ID namespace.
            tool_calls = m.get("tool_calls") or []
            if not isinstance(tool_calls, list) or any(not isinstance(c, dict) for c in tool_calls):
                raise ValueError(f"messages[{i}].tool_calls 必须为对象数组")
            ids = []
            for k, c in enumerate(tool_calls):
                step += 1
                ids.append(step)
                fn = c.get("function") or {}
                if not isinstance(fn, dict):
                    raise ValueError("工具 function 必须为对象")
                item = {"step_id": step, "message_index": i, "tool_call_id": c.get("id"),
                        "tool_name": fn.get("name"), "arguments": fn.get("arguments"), "returns": [], "call_index": k}
                calls.append(item)
                for field, value in (("id", c.get("id")), ("function.name", fn.get("name")), ("function.arguments", fn.get("arguments"))):
                    if value is None or (field != "function.arguments" and value == ""):
                        missing.append({"step_id": step, "message_index": i, "field": f"tool_calls[{k}].{field}",
                                        "issue": "null" if value is None and (field in c or field.split('.')[-1] in fn) else "缺失或空值"})
                if isinstance(c.get("id"), str) and c["id"]:
                    pending.setdefault(c["id"], []).append(item)
            if not ids and (m.get("content") or m.get("reasoning_content")):
                step += 1
                ids = [step]
            if ids:
                locations[i] = ids
                body = m.get("reasoning_content")
                if isinstance(body, str) and body:
                    segments.append({"step_id": ids[0], "step_ids": ids, "message_index": i, "text": body})
                else:
                    absent.extend(ids)
        elif role == "tool":
            candidates = pending.get(m.get("tool_call_id"), []) if isinstance(m.get("tool_call_id"), str) else []
            if len(candidates) == 1 and not candidates[0]["returns"]:
                item = candidates[0]
                item["returns"].append({"message_index": i, "content": m.get("content")})
                locations[i] = [item["step_id"]]
                if "content" not in m or m["content"] is None:
                    missing.append({"message_index": i, "step_id": item["step_id"], "field": "content",
                                    "issue": "null" if "content" in m else "缺失"})
            else:
                unmatched.append({"message_index": i, "tool_call_id": m.get("tool_call_id"),
                                  "issue": "返回无法唯一配对", "content": m.get("content")})
    for c in calls:
        if not c["returns"]:
            unmatched.append({"step_id": c["step_id"], "message_index": c["message_index"],
                              "tool_call_id": c["tool_call_id"], "issue": "调用缺少可唯一配对的返回", "content": c["arguments"]})
    return calls, segments, absent, unmatched, missing, locations


def analyze(record, *, apply_annotations=True):
    calls, segments, absent, unmatched, missing, locations = parse(record)
    messages = record["messages"]
    counts = Counter(c["tool_name"] for c in calls if isinstance(c["tool_name"], str))
    def evidence(i, quote):
        return {"message_index": i, **({"step_id": locations[i][0]} if i in locations else {}), "quote": quote}
    def call_evidence(c):
        return [{"step_id": c["step_id"], "message_index": c["message_index"], "quote": text(c["arguments"])}] + [
            {"step_id": c["step_id"], **r, "quote": text(r["content"])} for r in c["returns"]]
    from app.services.agent_trace_security import security_findings
    risks = first_risk(security_findings(messages, calls))
    failures, repeats, truncations = [], [], []
    history = {}
    for c in calls:
        args = text(c["arguments"])
        try:
            decoded = json.loads(args)
        except (ValueError, TypeError):
            decoded = {}
        command = decoded.get("command", decoded.get("cmd", "")) if isinstance(decoded, dict) else ""
        outputs = "\n".join(text(r["content"]) for r in c["returns"])
        failures.extend(execution_failures(c, command))
        key = (c["tool_name"], args)
        previous = history.get(key)
        # A successful read repeated without any intervening mutation is redundant.
        readonly = c["tool_name"] in {"read", "grep", "ls"} or bool(re.fullmatch(r"(?:sed|cat|head|ls)\s+[^;&|]+", command))
        if previous and readonly and previous["returns"] and outputs == "\n".join(text(r["content"]) for r in previous["returns"]):
            repeats.append({"step_id": c["step_id"], "previous_step_id": previous["step_id"], "evidence": call_evidence(previous) + call_evidence(c)})
        if c["tool_name"] in {"write", "edit", "apply_patch"} or (command and not readonly):
            history.clear()
        history[key] = c
    # Only explicit recording/transport truncation, not ellipses or head/limit usage.
    pattern = re.compile(r"\[.*?truncated.*?\]|(?:output|content|response) (?:has been |was |is )?truncated|image will be omitted|content omitted|内容省略|内容截断", re.I)
    for i, m in enumerate(messages):
        if m["role"] not in {"tool", "assistant"}:
            continue
        fields = [("content", m.get("content")), ("reasoning_content", m.get("reasoning_content"))]
        fields += [(f"tool_calls[{k}].function.arguments", c.get("function", {}).get("arguments")) for k, c in enumerate(m.get("tool_calls") or [])]
        for field, value in fields:
            for source_line in text(value).splitlines():
                if pattern.search(source_line):
                    truncations.append({"message_index": i, **({"step_id": locations[i][0]} if i in locations else {}), "field": field, "quote": source_line})
    metadata = record.get("metadata") or {}
    state = metadata.get("record") or {} if isinstance(metadata, dict) else {}
    if state.get("is_truncated") is True:
        truncations.append({"field": "metadata.record.is_truncated", "value": True})
    usage = record.get("usage") or {}
    result = {
        "A1": {"input_tokens": usage.get("input_tokens", MISSING), "output_tokens": usage.get("output_tokens", MISSING)},
        "A2": {"known_characters": sum(len(s["text"]) for s in segments), "absent_body_step_ids": absent},
        "A3": {"tool_calls": len(calls), "tool_kinds": len(counts), "tool_counts": dict(counts)},
        "B1": {"segments": segments, "absent_body_step_ids": absent},
        "B2": {"judgment": "有风险" if risks else "无风险"}, "B3": {"risks": risks, "first_risk_step": risks[0]["step_id"] if risks else None},
        "C1": {"calls": calls, "unmatched": unmatched}, "C2": {"markers": repeats}, "C3": {"failures": failures},
        "C4": {"missing_fields": missing, "unmatched": [{k: v for k, v in u.items() if k != "content"} for u in unmatched],
               "truncations": truncations, "completion": state.get("is_session_completed", MISSING)},
        "C5": {"issues": []},
    }
    for source, rows in (("C2", repeats), ("C3", failures), ("C4", missing + unmatched + truncations)):
        for item in rows:
            issue = {"source_subitem": source, **item}
            if "evidence" not in issue:
                i = item.get("message_index")
                if i is not None and "quote" in item:
                    # The truncation quote already came from the identified source field.
                    issue["evidence"] = [evidence(i, item["quote"])]
                elif i is not None and "content" in item:
                    issue["evidence"] = [evidence(i, text(item["content"]))]
                else:
                    # Missing fields / metadata have no message-body quote to invent.
                    issue["evidence"] = []
            result["C5"]["issues"].append(issue)
    for key, value in result.items():
        value["指标"] = NAMES[key]
    if apply_annotations:
        from app.services.agent_trace_reviewed_evidence import apply_reviewed_evidence
        apply_reviewed_evidence(record, result, locations)
    return {"trajectory_id": record.get("trajectory_id"), "analysis_version": VERSION, "results": result}


def report(db, scope, user):
    from sqlalchemy import select
    from app.models.agent_traces import TraceSample, TraceCall
    from app.services.agent_trace_batch_report import ordinary_report, tool_counts, VERSION as ORDINARY_VERSION
    from app.services import agent_trace_query as q
    if not q.permissions(user)["can_view_raw"]:
        q.fail(403, "赛题报告包含完整原文，需要原文查看权限")
    samples = list(db.scalars(select(TraceSample).where(TraceSample.snapshot_id == scope["snapshot_id"])
                              .order_by(TraceSample.ingest_order, TraceSample.trace_id)))
    if not samples:
        q.fail(422, "当前快照没有可导出的轨迹样本")
    rows, counts, ids = [], Counter(), set()
    reserved = {"aggregate"}
    for sample in samples:
        record = sample.result.get("source_record", {})
        if record.get("trajectory_id") and isinstance(record.get("messages"), list):
            if record["trajectory_id"] in reserved:
                q.fail(409, "当前快照包含重复或保留 trajectory_id，无法生成唯一报告")
            reserved.add(record["trajectory_id"])
    candidates = [sample.payload.get("original_sample_id") for sample in samples]
    candidate_counts = Counter(c for c in candidates if isinstance(c, str) and c)
    for sample in samples:
        record = sample.result.get("source_record", {})
        calls = q.process_records(db, scope, sample.trace_id, TraceCall)
        if record.get("trajectory_id") and isinstance(record.get("messages"), list):
            row = deepcopy(sample.payload.get("competition_report"))
            if not row:
                q.fail(409, "当前快照缺少赛题分析结果，请等待规则升级或重新解析")
            identity = record["trajectory_id"]
        else:
            row = ordinary_report(db, scope, sample, calls)
            candidate = sample.payload.get("original_sample_id")
            identity = candidate if isinstance(candidate, str) and candidate and candidate_counts[candidate] == 1 and candidate not in reserved else sample.trace_id
            while identity in reserved or identity in ids:
                identity = "trace:" + identity
        row["trajectory_id"] = identity
        ids.add(identity)
        per_tool = tool_counts(calls)
        row["results"]["A3"].update(tool_calls=len(calls), tool_kinds=len(per_tool), tool_counts=per_tool)
        rows.append(row)
        counts.update(per_tool)
    aggregate = {"trajectory_id": "aggregate", "results": {"A4": {"指标": NAMES["A4"],
                 "trajectory_ids": [r["trajectory_id"] for r in rows], "tool_calls": sum(counts.values()), "tool_counts": dict(counts)}}}
    versions = {r.get("analysis_version", "competition-v1") for r in rows}
    return {"jsonl": "\n".join(json.dumps(r, ensure_ascii=False) for r in [*rows, aggregate]) + "\n",
            "analysis_version": next(iter(versions)) if len(versions) == 1 else ORDINARY_VERSION,
            "notice": "基于固定快照的已保存原文与审计结果；普通轨迹步骤采用事件序号，缺失信息不补造；未命中不代表全面安全"}
