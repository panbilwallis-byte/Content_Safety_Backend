"""C3 outcome checks shared by imports, historical projections and reports.

Source commands are parsed as data. No commands are executed by these checks.
"""
from copy import deepcopy
import json
import re

STATUSES = {
    "success": "succeeded", "succeeded": "succeeded", "ok": "succeeded", "completed": "succeeded",
    "error": "failed", "failed": "failed", "failure": "failed", "timeout": "failed",
    "cancelled": "failed", "canceled": "failed", "aborted": "failed",
    "blocked": "not_executed", "denied": "not_executed", "rejected": "not_executed",
    "not_executed": "not_executed",
}
BLOCK_CODES = {"POLICY_DENIED", "PERMISSION_DENIED", "ACCESS_DENIED", "FORBIDDEN", "UNAUTHORIZED", "BLOCKED"}
METADATA_FIELDS = ("execution_status", "status", "is_error", "isError", "success", "error_code", "error",
                   "exit_code", "exitCode", "returncode", "return_code", "http_status", "status_code", "permission_decision")


def decoded(value):
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return value


def command_from(arguments):
    args = decoded(arguments)
    command = (args.get("command") or args.get("cmd")) if isinstance(args, dict) else None
    return command if isinstance(command, str) else ""


def task_instructions(record):
    return "\n".join(m["content"] for m in record.get("messages", []) if isinstance(m, dict)
                     and m.get("role") in {"system", "developer", "user"} and isinstance(m.get("content"), str))


def outcome(output, *, metadata=None, error=False, command="", instructions="", tool_name=""):
    """Prefer actual failure evidence to outer success, excluding expected test results."""
    body = decoded(output)
    body = body if isinstance(body, dict) else {}
    fields = {**body, **{k: v for k, v in (metadata or {}).items() if v is not None}}
    text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
    stripped = text.strip()
    code = fields.get("error_code")
    permission = fields.get("permission_decision")
    statuses = [STATUSES[str(fields[k]).lower()] for k in ("execution_status", "status")
                if str(fields.get(k, "")).lower() in STATUSES]
    status = next((s for s in ("not_executed", "failed", "succeeded") if s in statuses), None)
    failures = []

    def finish(state, result_code):
        return {"execution_status": state, "result_code": str(result_code),
                "permission_decision": "denied" if state == "not_executed" else permission or "unknown",
                "execution_failures": failures}

    def add(label, result_code, quote):
        if not any(f["error_type"] == label for f in failures):
            # Quoted JSON string values may be escaped in the source return.
            failures.append({"error_type": label, "result_code": result_code,
                             "quote": quote if quote in text else text})

    if permission == "denied" or str(code).upper() in BLOCK_CODES or status == "not_executed":
        return finish("not_executed", code or "POLICY_DENIED")
    if re.match(r"(?:policy|permission|access)[ _-]*(?:denied|blocked|rejected)\b|(?:Blocked|Denied)\s*:", stripped, re.I):
        return finish("not_executed", "POLICY_DENIED")

    # Previewed contents are data. A wrapper's explicit failure still counts.
    preview = bool(re.match(r"<path>[^\n]+</path>\s*<type>(?:file|directory)</type>", stripped))
    file_read = tool_name.lower().rsplit(".", 1)[-1] in {"read", "read_file"} or bool(
        re.fullmatch(r"\s*(?:cat|head|tail|sed)\s+[^;&|<>`$\n]+", command))

    # A task-requested target crash is a test result, not a failure of the tool.
    target_test = bool(re.search(
        r"(?:trigger|reproduce|demonstrate|verify|induce|cause|proof.of.concept|poc).{0,120}"
        r"(?:crash|vulnerabilit|use.after.free)|(?:触发|复现|验证).{0,40}(?:崩溃|漏洞)", instructions, re.I | re.S))
    target_crash = target_test and bool(re.search(
        r"AddressSanitizer:|MemorySanitizer:|UndefinedBehaviorSanitizer:|Segmentation fault|target crashed|heap-use-after-free", text, re.I))

    # grep/diff/test exit 1 means a normal negative answer when used as a probe.
    from app.services.agent_trace_security import shell_commands
    commands = shell_commands(command)
    negative_exit = len(commands) == 1 and commands[0][0].rsplit("/", 1)[-1] in {"grep", "rg", "diff", "cmp", "test", "["}
    download = any(t[0].rsplit("/", 1)[-1] == "curl" and (
        any(v in {"-O", "--remote-name"} for v in t) or
        any(v in {"-o", "--output"} and i + 1 < len(t) and t[i + 1] not in {"/dev/null", "-"} for i, v in enumerate(t)) or
        any(v.startswith("--output=") and v.split("=", 1)[1] not in {"/dev/null", "-"} for v in t)) for t in commands)

    def exit_failure(value, quote):
        if value == 0 or (value == 1 and negative_exit) or (target_crash and value in {1, -6, -11, 134, 139}):
            return
        label, result_code = ("执行超时", "TIMEOUT") if value == 124 else (
            ("命令不存在", "EXIT_127") if value == 127 else ("进程退出异常", f"EXIT_{value}"))
        add(label, result_code, quote)

    # Inspect execution-bearing output/stdout/stderr, not arbitrary result/data.
    streams = [body[k] for k in ("output", "stdout", "stderr") if isinstance(body.get(k), str)] if body else [text]
    read_error = re.match(r"^(?:Error:\s|(?:cat|head|tail|sed):.*(?:No such file|Permission denied))", stripped)
    if not preview and (not file_read or read_error):
        for stream in streams:
            fenced = False
            lines = stream.splitlines()
            for line_index, line in enumerate(lines):
                value = line.strip()
                if value.startswith("```"):
                    fenced = not fenced
                    continue
                if fenced:
                    continue
                exit_match = re.fullmatch(r"(?:Process|Command) exited with code\s+(-?\d+)", value, re.I)
                if exit_match:
                    exit_failure(int(exit_match[1]), line)
                    continue
                if re.match(r"^curl:\s*\(28\)|^NO NET \(exit 28\)$", value, re.I):
                    add("下载超时" if download else "网络请求超时", "TIMEOUT", line)
                elif re.match(r"^curl:\s*\(23\).*Failure writing output", value, re.I):
                    add("输出写入失败", "ERROR", line)
                elif re.fullmatch(r"User aborted the command", value, re.I):
                    add("执行被用户中止", "ABORTED", line)
                elif re.match(r"^(?:/\S+/)?(?:ba)?sh:.*command not found", value, re.I):
                    add("命令不存在", "EXIT_127", line)
                elif re.match(r"^(?:/\S+/)?(?:ba)?sh:.*(?:No such file|Permission denied)", value, re.I):
                    add("命令或请求失败", "ERROR", line)
                elif re.match(r"^(?:cat|head|tail|sed):.*(?:No such file|Permission denied)", value, re.I):
                    add("文件读取失败", "ERROR", line)
                elif re.match(r"^(?:tool|command|operation|execution)\b.*(?:timed out|timeout)", value, re.I):
                    add("工具超时", "TIMEOUT", line)
                elif re.match(r"^(?:[^\s:]+:\d+(?::\d+)?:\s*(?:fatal )?error:|(?:gcc|g\+\+|clang|javac):\s*(?:fatal )?error:|Compilation failed\b)", value, re.I):
                    add("编译失败", "ERROR", line)
                elif re.fullmatch(r"Traceback \(most recent call last\):", value):
                    # Explicit raise + test intent + matching exception, with no
                    # hard-coded exception message or sample identity.
                    following = "\n".join(lines[line_index + 1:]).split("Traceback (most recent call last):", 1)[0].split("---", 1)[0]
                    expected = any(re.search(rf"^{re.escape(m[1])}:\s*{re.escape(m[3])}\s*$", following, re.M)
                        for m in re.finditer(r"raise\s+(\w+)\s*\(\s*(['\"])([^'\"\n]+)\2\s*\)", command))
                    test_intent = re.search(r"fallback|expected|should raise|assertRaises|pytest\.raises|预期|回退", command, re.I)
                    if not (expected and test_intent) and not target_crash:
                        add("运行异常", "ERROR", line)
                elif re.match(r"^Error:\s|^curl:\s*\(\d+\)", value):
                    add("工具执行错误", "ERROR", line)

    # NO NET alone is a negative connectivity probe, not a timeout diagnostic.
    negative_probe = stripped == "NO NET"
    for key in ("exit_code", "exitCode", "returncode", "return_code"):
        if type(fields.get(key)) is int:
            exit_failure(fields[key], text)
            break
    http = fields.get("http_status", fields.get("status_code"))
    probe_http = any(t[0].rsplit("/", 1)[-1] == "curl" and any(v in {"-I", "--head", "-w", "--write-out"} for v in t) for t in commands)
    if type(http) is int and 400 <= http <= 599 and not probe_http:
        add("请求失败", f"HTTP_{http}", text)
    http_match = re.match(r"HTTP/\d(?:\.\d)?\s+(\d{3})\b", stripped)
    if http_match and int(http_match[1]) >= 400 and not probe_http:
        add("请求失败", f"HTTP_{http_match[1]}", text)
    expected_negative = negative_exit and any(type(fields.get(k)) is int and fields[k] == 1
                                             for k in ("exit_code", "exitCode", "returncode", "return_code"))
    explicit_timeout = "TIMEOUT" in str(code).upper() or str(fields.get("status")).lower() == "timeout"
    if not negative_probe and (not target_crash or explicit_timeout):
        if error or fields.get("is_error") is True or fields.get("isError") is True or fields.get("success") is False or (status == "failed" and not expected_negative) or fields.get("error"):
            if not failures:
                add("工具超时" if explicit_timeout else "工具执行错误", code or ("TIMEOUT" if explicit_timeout else "ERROR"), text)
        if code not in (None, "", 0, "0", "OK", "SUCCESS", "NOT_RECORDED") and not failures:
            add("工具超时" if "TIMEOUT" in str(code).upper() else "工具执行错误", str(code), text)
    if failures:
        return finish("failed", failures[0]["result_code"])
    if negative_probe or target_crash:
        return finish("succeeded", "OK")
    if str(fields.get("execution_status", fields.get("status", ""))).lower() in {"pending", "running", "in_progress", "unknown"}:
        return finish("unknown", "NOT_RECORDED")
    if output is None or stripped == "null":
        return finish("unknown", "NOT_RECORDED")
    return finish("succeeded", "OK")


def project_calls(calls, record=None):
    """Reconcile actual recorded calls, including old success/failure classifications."""
    record = record or {}
    instructions = task_instructions(record)
    source_calls = []
    if isinstance(record.get("messages"), list):
        from app.services.agent_trace_competition import parse
        source_calls = parse(record)[0]
    values = deepcopy(calls)
    for i, call in enumerate(values):
        if call.get("data_origin") == "generated" or not call.get("return_event_id"):
            continue
        metadata = call.get("outcome_metadata")
        source = source_calls[i] if len(source_calls) == len(calls) else None
        if (source and source["tool_name"] == call.get("tool_name") and source["returns"]
                and decoded(source.get("arguments")) == decoded(call.get("input_json"))):
            message = record["messages"][source["returns"][0]["message_index"]]
            metadata = {k:message[k] for k in METADATA_FIELDS if k in message}
        if metadata is None:
            # Legacy records without their original envelope retain declared facts.
            metadata = {"execution_status":call.get("execution_status")} if call.get("execution_status") != "unknown" else {}
            if call.get("result_code") not in {None, "NOT_RECORDED", "OK"}:
                metadata["error_code"] = call["result_code"]
            if call.get("permission_decision") == "denied":
                metadata["permission_decision"] = "denied"
        call.update(outcome(call.get("output_json"), metadata=metadata, command=command_from(call.get("input_json")),
                            instructions=instructions, tool_name=call.get("tool_name", "")))
    return values


def project_audit(payload, calls):
    """Refresh tool runtime findings only; preserve risk and audit availability."""
    value = deepcopy(payload)
    audit = value.get("rule_audit")
    eligible = {c["call_id"] for c in calls if "execution_failures" in c}
    if not eligible or not isinstance(audit, dict) or audit.get("status") != "completed" or not isinstance(value.get("rule_findings"), list):
        return value
    from app.services.agent_trace_generation import stable_id
    value["rule_findings"] = [f for f in value.get("rule_findings", []) if not (
        f.get("call_id") in eligible and f.get("rule_id") in {"tool_error", "tool_timeout"})]
    for call in calls:
        facts = call.get("execution_failures", [])
        if not facts:
            continue
        timeout = any("TIMEOUT" in f["result_code"] for f in facts)
        rule = "tool_timeout" if timeout else "tool_error"
        value["rule_findings"].append({
            "finding_id": stable_id("finding_", "c3", rule, call["call_id"]), "rule_id": rule,
            "rule_version": value["rule_audit"].get("version"), "category": "operation",
            "label": "、".join(f["error_type"] for f in facts), "call_id": call["call_id"],
            "event_id": call["event_id"], "event_display_id": call.get("step_display_id"),
            "severity": "low", "outcome": "recorded", "evidence_text": "\n".join(f["quote"] for f in facts),
            "detection_source": "backend_rule", "source_ref": {},
        })
    value["rule_audit"]["operation_count"] = sum(f.get("category") == "operation" for f in value["rule_findings"])
    return value
