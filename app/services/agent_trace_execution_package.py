"""Three-file, raw batch export over a fixed, previously audited snapshot."""
import io
import json
import zipfile

from sqlalchemy import select

from app.core.auth import permission_tier
from app.models.tables import SystemUser
from app.models.agent_traces import TraceEvidence, TraceEvent
from app.services import agent_trace_query as q
from app.services.agent_trace_competition import NAMES, report_rows
from app.services.agent_trace_submission import submission_rows
from app.services.agent_trace_steps import plan

VERSION = "execution-risk-package-v1"
FIRST_METRIC = NAMES["B3"]
ALL_METRIC = "第一/二/三次风险行为步骤、片段和原文证据"


def requested(data):
    return (data.get("view"), data.get("export_kind"), data.get("format")) == ("execution", "package", "zip")


def enabled(task):
    return task.config.get("execution_package_version") == VERSION and requested(task.input_data)


def authorize(db, user):
    """Re-read current role/state even when the worker carries an older role."""
    try:
        uid = int(user["uid"])
    except (KeyError, TypeError, ValueError):
        q.fail(403, "原文结果包需要有效操作者")
    stored = db.get(SystemUser, uid)
    if not stored or stored.status not in {"active", "normal"}:
        q.fail(403, "原文结果包操作者不存在或已停用")
    actor = {"uid": stored.id, "tier": permission_tier(stored.role), "role": stored.role}
    if not all(q.permissions(actor)[k] for k in ("can_export", "can_view_raw")):
        q.fail(403, "原文结果包需要原文查看及导出权限")
    return actor


def positive(value):
    return value if type(value) is int and value > 0 else None


def text(value):
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, allow_nan=False)


def risk_groups(db, scope, sample):
    payload = sample.payload
    if (payload.get("data_origin") != "actual"
            or (payload.get("rule_audit") or {}).get("status") != "completed"
            or not isinstance(payload.get("rule_findings"), list)):
        q.fail(409, "当前快照缺少完整安全审计结果，请重新解析后导出")
    events = q.process_records(db, scope, sample.trace_id, TraceEvent)
    steps = plan(sample)
    # A shared reasoning message can map to multiple actions. Never guess one.
    path_steps = {}
    for step in steps or []:
        for path in step["paths"]:
            path_steps.setdefault(path, set()).add(step["step_id"])
    event_steps, message_steps = {}, {}
    for event in events:
        path = (event.get("source_ref") or {}).get("source_path")
        candidates = path_steps.get(path, set())
        event_steps[event["event_id"]] = next(iter(candidates)) if len(candidates) == 1 else None
        if isinstance(path, str) and path.startswith("messages["):
            index = path.split("]", 1)[0] + "]"
            message_steps.setdefault(index, set()).update(candidates)

    evidence_rows = list(db.scalars(select(TraceEvidence).where(
        TraceEvidence.snapshot_id == scope["snapshot_id"], TraceEvidence.trace_id == sample.trace_id,
        TraceEvidence.projection_version == scope["projection_version"]).order_by(TraceEvidence.id)))
    evidence_by_id = {row.payload.get("evidence_id", row.id): {**row.payload, "event_id": row.event_id}
                      for row in evidence_rows}
    findings = [f for f in payload["rule_findings"] if f.get("category") == "security"]
    if payload.get("audit_decision") == "risk_found" and not findings and not evidence_rows:
        q.fail(409, "风险结论缺少已保存的风险发现或证据，无法导出全部风险")
    used, entries = set(), []
    for finding in findings:
        identity = finding.get("finding_id")
        evidence = evidence_by_id.get(identity, {})
        used.add(identity)
        if finding.get("review_status") == "dismissed" or evidence.get("review_status") == "dismissed":
            continue
        entries.append((finding, evidence))
    # Also preserve source evidence that has no corresponding generated finding.
    for identity, evidence in evidence_by_id.items():
        if identity not in used and evidence.get("review_status") != "dismissed":
            entries.append(({}, evidence))

    groups = {}
    trace_risks = []
    for order, (finding, evidence) in enumerate(entries):
        if finding.get("scope") == "trace":
            trace_risks.append({"step_id": None, "rule_id": finding["rule_id"], "scope": "trace",
                                "risk_quote": "", "evidence": []})
            continue
        event_id = evidence.get("event_id") or finding.get("event_id")
        step = positive(finding.get("competition_step")) or positive(evidence.get("competition_step"))
        step = step or event_steps.get(event_id)
        # Keep separate source events when no reliable action step is available.
        key = ("step", step) if step is not None else ("event", event_id or f"unlocated-{order}")
        group = groups.setdefault(key, {"step_id": step, "quotes": [], "evidence": [], "seen": set()})
        quote = text(evidence.get("match_pattern", finding.get("evidence_text")))
        if quote == "源日志未提供匹配片段":
            quote = ""  # A historical missing-value label is not original evidence.
        if quote and quote not in group["quotes"]:
            group["quotes"].append(quote)
        source = evidence.get("source_evidence") or finding.get("source_evidence")
        if not isinstance(source, list) or not source:
            source = [{"step_id": step, "quote": quote}] if quote else []
        for item in source:
            if not isinstance(item, dict):
                continue
            evidence_step = positive(item.get("step_id"))
            if evidence_step is None:
                evidence_step = event_steps.get(item.get("event_id"))
            if evidence_step is None and type(item.get("message_index")) is int:
                candidates = message_steps.get(f"messages[{item['message_index']}]", set())
                evidence_step = next(iter(candidates)) if len(candidates) == 1 else None
            value = text(item.get("quote"))
            identity = (evidence_step, value)
            if identity not in group["seen"]:
                group["seen"].add(identity)
                group["evidence"].append({"step_id": evidence_step, "quote": value})
    ordered = sorted(groups.values(), key=lambda g: (g["step_id"] is None, g["step_id"] or 0))
    return [{"step_id": group["step_id"], "risk_quote": "\n\n".join(group["quotes"]), "evidence": group["evidence"]}
            for group in ordered] + trace_risks


def build(db, task):
    user = authorize(db, {"uid": task.config["owner_id"]})
    _, scope = q.resolve(db, task.input_data["batch_id"], task.input_data)
    samples, submission = report_rows(db, scope, user)
    first, all_risks = [], []
    for sample, report in zip(samples, submission[:-1], strict=True):
        risks = risk_groups(db, scope, sample)
        identity = report["trajectory_id"]
        located = [risk for risk in risks if risk.get("scope") != "trace"]
        first.append({"trajectory_id": identity, "B3": {"指标": FIRST_METRIC, "risks": located[:1]}})
        all_risks.append({"trajectory_id": identity, "B3": {"指标": ALL_METRIC, "risks": risks}})
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, document in (("submission.json", submission_rows(submission)), ("first-risks.json", first), ("all-risks.json", all_risks)):
            archive.writestr(name, (json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8"))
    return output.getvalue(), "agent-trace-execution.zip", "application/zip"
