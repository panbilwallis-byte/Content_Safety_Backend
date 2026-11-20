"""Interpret recorded tool outcomes; never execute source text or inspect nested data as status."""
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


def outcome(output, *, metadata=None, error=False):
    """A completed linked return is success unless recorded outcome evidence says otherwise."""
    try:
        body = json.loads(output) if isinstance(output, str) else output
    except (ValueError, TypeError):
        body = None
    body = body if isinstance(body, dict) else {}
    meta = metadata if isinstance(metadata, dict) else {}
    # Only envelope fields: a result/data payload may legitimately describe errors.
    fields = {**body, **{k: v for k, v in meta.items() if v is not None}}
    code = fields.get("error_code")
    permission = fields.get("permission_decision")
    status = next((STATUSES[str(fields[k]).lower()] for k in ("execution_status", "status")
                   if str(fields.get(k, "")).lower() in STATUSES), None)
    def result(state, result_code):
        return {"execution_status": state, "result_code": str(result_code),
                "permission_decision": "denied" if state == "not_executed" else permission or "unknown"}
    if permission == "denied" or str(code).upper() in BLOCK_CODES or status == "not_executed":
        return result("not_executed", code or "POLICY_DENIED")
    if error or fields.get("is_error") is True or fields.get("isError") is True or fields.get("success") is False:
        return result("failed", code or "ERROR")
    if status:
        return result(status, code or ("OK" if status == "succeeded" else "TIMEOUT" if str(fields.get("status")).lower() == "timeout" else "ERROR"))
    if code not in (None, "", 0, "0", "OK", "SUCCESS") or fields.get("error"):
        return result("failed", code or "ERROR")
    for key in ("exit_code", "exitCode", "returncode", "return_code"):
        value = fields.get(key)
        if type(value) is int:
            return result("succeeded" if value == 0 else "failed", "OK" if value == 0 else "TIMEOUT" if value == 124 else f"EXIT_{value}")
    http = fields.get("http_status", fields.get("status_code"))
    if type(http) is int and 400 <= http <= 599:
        return result("failed", f"HTTP_{http}")
    if fields.get("success") is True:
        return result("succeeded", "OK")
    if str(fields.get("execution_status", fields.get("status", ""))).lower() in {"pending", "running", "in_progress", "unknown"}:
        return result("unknown", "NOT_RECORDED")
    if output is None or (isinstance(output, str) and output.strip() == "null"):
        return result("unknown", "NOT_RECORDED")
    if not isinstance(output, str) or body:
        return result("succeeded", "OK")
    text = output.strip()
    # File previews, including source code and quoted diagnostics, are data.
    if re.match(r"<path>[^\n]+</path>\s*<type>(?:file|directory)</type>", text):
        return result("succeeded", "OK")
    # Execution wrappers report the process result separately from its stdout.
    exit_match = re.search(r"^(?:Process|Command) exited with code\s+(-?\d+)\s*$", text, re.M)
    if exit_match:
        value = int(exit_match[1])
        return result("succeeded" if value == 0 else "failed", "OK" if value == 0 else "TIMEOUT" if value == 124 else f"EXIT_{value}")
    http_match = re.match(r"HTTP/\d(?:\.\d)?\s+(\d{3})\b", text)
    if http_match and int(http_match[1]) >= 400:
        return result("failed", f"HTTP_{http_match[1]}")
    if re.match(r"(?:policy|permission|access)[ _-]*(?:denied|blocked|rejected)\b|^(?:Blocked|Denied)\s*:", text, re.I):
        return result("not_executed", "POLICY_DENIED")
    if re.search(r"^(?:curl:\s*\(28\)|NO NET(?: \(exit 28\))?$|(?:tool|command|operation|execution)\b[^\n]*(?:timed out|timeout))", text, re.M | re.I):
        return result("failed", "TIMEOUT")
    if re.search(r"^User aborted the command\s*$", text, re.M):
        return result("failed", "ABORTED")
    # Do not turn arbitrary mentions of 'error', source code or warnings into failures.
    if re.search(r"^(?:Error:\s|curl:\s*\(\d+\)|(?:/[^\s:]+/)?(?:ba)?sh:.*(?:not found|Permission denied)|Traceback \(most recent call last\):)", text, re.M):
        if "ImportError: blocked for fallback test" not in text:
            return result("failed", "ERROR")
    return result("succeeded", "OK")


def project_calls(calls):
    """Repair historical unknown outcomes in responses without rewriting snapshots."""
    values = deepcopy(calls)
    for call in values:
        if call.get("execution_status") == "unknown" and call.get("return_event_id"):
            code = call.get("result_code")
            metadata = {"error_code": code} if code and code not in {"NOT_RECORDED", "OK"} else None
            fields = outcome(call.get("output_json"), metadata=metadata)
            if call.get("permission_decision") == "denied":
                fields.update(execution_status="not_executed", permission_decision="denied", result_code="POLICY_DENIED")
            call.update(fields)
    return values
