"""Frozen tools report tasks, genuine files and short-lived capability tickets."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import io
import json
import os
from pathlib import Path
import re
import time
from uuid import uuid4
import zipfile

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.core.database import SessionLocal
from app.core.time import now_shanghai
from app.models.tables import SystemUser, Task
from app.models.agent_traces import TraceArtifact, TraceIdempotency
from app.repositories.task_repository import task_to_dict
from app.services.agent_trace_generation import dumps
from app.services import agent_trace_query as query
from app.services.agent_trace_query import fail, resolve, get_sample, process_records, permissions, redact
from app.models.agent_traces import TraceEvent, TraceCall, TraceEvidence


def directory():
    path = Path(os.getenv("AGENT_TRACE_ARTIFACT_DIR", "storage/agent-trace-artifacts")).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def idem_key(actor, path, key):
    if not key or len(key) > 200:
        fail(422, "X-Request-Id 必填且最长 200 字符")
    return hashlib.sha256(dumps([str(actor), "POST", path, key]).encode()).hexdigest()


def owner_task(db, task_id, user):
    task = db.get(Task, task_id)
    if not task or task.capability_code not in {"agent_trace_export", "agent_trace_ingest"}:
        fail(404, "导出任务不存在")
    if str(task.config.get("owner_id")) != str(user["uid"]):
        fail(403, "没有访问此导出任务的权限")
    if task.capability_code == "agent_trace_ingest":
        from app.models.agent_traces import TraceBatch
        batch = db.get(TraceBatch, task.input_data["batch_id"])
        if not batch: fail(404, "批次不存在")
        query.llm_access(db, batch)
    else:
        resolve(db, task.input_data["batch_id"], task.input_data)
    return task


def validate_input(db, data, user):
    if data.get("view") in {"ingest", "statistics", "execution"}:
        from app.services.agent_trace_reports import validate
        return validate(db, data, user)
    if not permissions(user)["can_export"]:
        fail(403, "当前账号不能导出")
    if data.get("view") != "tools":
        fail(422, "首版仅支持工具交互导出")
    kind = data.get("export_kind")
    if (kind, data.get("format")) not in {("view", "png"), ("package", "zip")}:
        fail(422, "导出类型或格式不支持")
    for key in ("batch_id", "snapshot_id", "projection_version", "selected_trace_id"):
        if not isinstance(data.get(key), str) or not data[key] or len(data[key]) > 64:
            fail(422, f"{key} 必须为有效标识")
    scope = data.get("scope", {})
    if not isinstance(scope, dict):
        fail(422, "scope 必须为对象")
    ids = scope.get("trace_ids", [])
    if scope.get("type") != "sample" or not isinstance(ids, list) or len(ids) != 1 or not isinstance(ids[0], str):
        fail(422, "首版仅支持一个当前样本的导出")
    if data.get("selected_trace_id") != ids[0]:
        fail(409, "所选样本与导出范围不一致")
    batch, resolved = resolve(db, data.get("batch_id"), data)
    sample = get_sample(db, resolved, ids[0])
    calls = process_records(db, resolved, ids[0], TraceCall)
    selected = data.get("selected_call_id")
    if selected is not None and not isinstance(selected, str):
        fail(422, "selected_call_id 必须为字符串")
    if selected and not any(c["call_id"] == selected for c in calls):
        fail(404, "导出调用不属于所选样本")
    event = data.get("selected_event_id")
    if event is not None and not isinstance(event, str):
        fail(422, "selected_event_id 必须为字符串")
    if event and not any(e["event_id"] == event for e in process_records(db, resolved, ids[0], TraceEvent)):
        fail(404, "导出事件不属于所选样本")
    contents = data.get("contents")
    allowed = {"tools_png"} if kind == "view" else {"normalized_json", "evidence_index", "manifest"}
    if not isinstance(contents, list) or not contents or any(not isinstance(c, str) or c not in allowed for c in contents):
        fail(422, "包含未支持的导出内容")
    if kind == "package" and "manifest" not in contents:
        fail(422, "ZIP 必须包含 manifest")
    if data.get("redaction_profile", "standard") != "standard" or data.get("anonymize_ids", True) is not True:
        fail(422, "首版导出要求 standard 脱敏和匿名化")
    return {**deepcopy(data), **resolved, "redaction_profile": "standard", "anonymize_ids": True}


def create_task(db, data, user, key, name=None):
    frozen = validate_input(db, data, user)  # Authorization is checked on every replay.
    identity = idem_key(user["uid"], "/tasks/execute", key)
    digest = hashlib.sha256(json.dumps({"input": data, "name": name}, sort_keys=True,
                                      ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    existing = db.get(TraceIdempotency, identity)
    if existing and existing.expires_at > now_shanghai():
        if existing.payload_hash != digest:
            fail(409, "同一幂等键不能用于不同导出内容")
        return owner_task(db, existing.result["task_id"], user), False
    if existing:
        db.delete(existing)
        db.flush()
    task_id = "tr_export_" + uuid4().hex
    record = TraceIdempotency(key=identity, payload_hash=digest, result={"task_id": task_id},
                              expires_at=now_shanghai() + timedelta(hours=24))
    try:
        db.add(record)
        db.flush()
        task = Task(task_id=task_id, name=name or "工具交互导出", capability_code="agent_trace_export",
            status="pending", progress=0, input_data=frozen,
            config={"owner_id": str(user["uid"]), "role": user.get("role", ""), "tier": user.get("tier", "operator")},
            source_name="工具交互", result={"kind": "agent_trace_export", "batch_id": frozen["batch_id"],
                                         "snapshot_id": frozen["snapshot_id"], "artifacts": []})
        db.add(task)
        db.commit()
        return task, True
    except IntegrityError:
        db.rollback()
        existing = db.get(TraceIdempotency, identity)
        if not existing or existing.payload_hash != digest:
            fail(409, "导出请求并发冲突，请重新查询")
        return owner_task(db, existing.result["task_id"], user), False


def anonymize(document, seed, sample):
    """One package-wide mapping; it is never included in the resulting files."""
    identifiers = {sample.trace_id, sample.sample_id, sample.display_id}
    def collect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(item, str) and (key.endswith("_id") or key in {"display_id", "step_display_id"}):
                    identifiers.add(item)
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
    collect(document)
    mapping = {v: "anon_" + hashlib.sha256((seed + "\0" + v).encode()).hexdigest()[:12] for v in identifiers if v}
    embedded_ids = [re.escape(v) for v in sorted(mapping, key=len, reverse=True) if len(v) >= 4]
    embedded_pattern = re.compile("|".join(embedded_ids)) if embedded_ids else None
    def replace(value):
        if isinstance(value, dict):
            return {k: replace(v) for k, v in value.items()}
        if isinstance(value, list):
            return [replace(v) for v in value]
        if isinstance(value, str):
            if value in mapping:
                return mapping[value]
            try:
                parsed = json.loads(value)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, (dict, list)):
                return dumps(replace(parsed))
            return embedded_pattern.sub(lambda m: mapping[m.group(0)], value) if embedded_pattern else value
        return value
    return replace(redact(document, preserve_ids=True))


def render_png(document):
    from PIL import Image, ImageDraw, ImageFont
    fonts = [os.getenv("AGENT_TRACE_FONT", ""), "C:/Windows/Fonts/msyh.ttc",
             "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
             "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"]
    font_path = next((p for p in fonts if p and Path(p).is_file()), None)
    if not font_path:
        raise ValueError("PNG 中文字体不可用，请配置 AGENT_TRACE_FONT")
    font, title_font = ImageFont.truetype(font_path, 18), ImageFont.truetype(font_path, 28)
    calls = document["calls"]
    width, height = 1500, 260 + max(1, len(calls)) * 180
    if height > 60000:
        raise ValueError("调用过多无法生成 PNG，请导出 ZIP")
    img = Image.new("RGB", (width, height), "#f3f7ff")
    draw = ImageDraw.Draw(img)
    draw.text((30, 20), "智能体轨迹审计 · 工具交互", font=title_font, fill="#17365e")
    draw.text((30, 70), "价值分析 · 含模拟过程（按固定快照生成，非浏览器截图）", font=font, fill="#ad5e00")
    draw.text((30, 110), f"样本 {document['sample']['trace_id']} | 调用 {len(calls)} | 来源 {document['sample']['source']['format_label']}", font=font, fill="#17365e")
    y = 180
    for c in calls:
        draw.rounded_rectangle((25, y, width - 25, y + 160), radius=8, outline="#aac5ee", fill="white")
        lines = [f"{c['display_id']}  {c['tool_name']}  {c['execution_status']}  {c.get('duration_ms')} ms",
                 f"权限：{c['permission_decision']} | 结果：{c['result_code']}",
                 "请求：" + c["input_json"], "返回：" + c["output_json"]]
        for j, line in enumerate(lines):
            draw.text((40, y + 12 + j * 34), line[:100], font=font, fill="#17365e")
        y += 180
    if not calls:
        draw.text((40, y), "该样本没有工具调用", font=font, fill="#17365e")
    output = io.BytesIO()
    img.save(output, format="PNG")
    return output.getvalue()


def build_artifact(db, task):
    if task.input_data.get("view") != "tools":
        from app.services.agent_trace_reports import build
        return build(db, task)
    data = task.input_data
    batch, scope = resolve(db, data["batch_id"], data)
    trace = data["selected_trace_id"]
    sample = get_sample(db, scope, trace)
    document = {"scope": scope, "sample": {**query.sample_summary(sample), "source": sample.payload["source"]},
        "value_result": sample.result, "events": process_records(db, scope, trace, TraceEvent),
        "calls": process_records(db, scope, trace, TraceCall),
        "evidence": [r.payload for r in db.scalars(select(TraceEvidence).where(
            TraceEvidence.snapshot_id == scope["snapshot_id"], TraceEvidence.trace_id == trace,
            TraceEvidence.projection_version == scope["projection_version"]))]}
    document = anonymize(document, task.task_id, sample)
    if data["export_kind"] == "view":
        return render_png(document), "agent-trace-tools.png", "image/png"
    contents = set(data["contents"])
    files = {}
    if "normalized_json" in contents:
        files["normalized.json"] = dumps(document).encode()
    if "evidence_index" in contents:
        files["evidence-index.json"] = dumps({"calls": [{"call_id": c["call_id"], "event_id": c["event_id"],
            "evidence_chain": c["evidence_chain"]} for c in document["calls"]], "evidence": document["evidence"]}).encode()
    manifest = {"schema_version": "1", **document["scope"], "created_at": now_shanghai().isoformat() + "+08:00",
        "sample_count": 1, "call_count": len(document["calls"]), "event_count": len(document["events"]),
        "redaction_profile": "standard", "anonymize_ids": True, "anonymization_policy": "package-salted-v1",
        "data_origin": sample.payload["data_origin"], "notice": ("源日志实际记录；风险结论以明确记录的审计结果为准" if sample.payload["data_origin"] == "actual" else "含模拟过程，不作为真实风险审计证据"),
        "generation_policy_version": sample.payload.get("generation_policy_version"), "ui_version": data.get("ui_version", "agent-trace-tools-v1"),
        "contents": sorted(contents), "warnings": ["原始日志默认不导出"],
        "files": [{"path": name, "size_bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(),
                   "mime_type": "application/json"} for name, body in files.items()]}
    files["manifest.json"] = dumps(manifest).encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return output.getvalue(), "agent-trace-tools.zip", "application/zip"


def run_export(task_id, sessions=SessionLocal):
    with sessions() as db:
        claimed = db.execute(update(Task).where(Task.task_id == task_id, Task.status == "pending")
                             .values(status="running", progress=10)).rowcount
        db.commit()
        if claimed != 1:
            return
        task = db.get(Task, task_id)
        try:
            user = {"uid": task.config["owner_id"], "tier": task.config.get("tier", "operator")}
            validate_input(db, task.input_data, user)
            body, filename, mime = build_artifact(db, task)
            aid = "art_" + uuid4().hex
            storage_name = aid + (".png" if mime == "image/png" else ".json" if mime == "application/json" else ".zip")
            target = directory() / storage_name
            temporary = target.with_suffix(".tmp")
            temporary.write_bytes(body)
            temporary.replace(target)
            artifact = TraceArtifact(artifact_id=aid, task_id=task_id, owner_id=str(user["uid"]),
                batch_id=task.input_data["batch_id"], snapshot_id=task.input_data["snapshot_id"],
                storage_name=storage_name, filename=filename, mime_type=mime,
                sha256=hashlib.sha256(body).hexdigest(), size_bytes=len(body),
                expires_at=now_shanghai() + timedelta(hours=24))
            db.add(artifact)
            task.status, task.progress, task.finished_at = "succeeded", 100, now_shanghai()
            task.result = {**task.result, "projection_version": task.input_data["projection_version"],
                           "artifacts": [artifact_dict(artifact)]}
            db.commit()
        except Exception as exc:
            db.rollback()
            task = db.get(Task, task_id)
            task.status, task.finished_at = "failed", now_shanghai()
            message = str(exc.detail) if isinstance(exc, HTTPException) else str(exc) if isinstance(exc, ValueError) else "导出文件生成失败，请检查存储和字体配置"
            task.result = {**task.result, "error_message": message, "artifacts": []}
            db.commit()


def artifact_dict(row):
    return {"artifact_id": row.artifact_id, "filename": row.filename, "mime_type": row.mime_type,
            "batch_id": row.batch_id, "snapshot_id": row.snapshot_id, "size_bytes": row.size_bytes,
            "sha256": row.sha256, "expires_at": row.expires_at.isoformat() + "+08:00"}


def artifact_for_user(db, artifact_id, user):
    row = db.get(TraceArtifact, artifact_id)
    if not row:
        fail(410, "导出产物不存在或已清理，请重新导出")
    if str(user["uid"]) != row.owner_id:
        fail(403, "没有产物访问权限")
    if not permissions(user)["can_export"]:
        fail(403, "当前账号不能下载导出产物")
    if row.expires_at <= now_shanghai():
        fail(410, "导出产物已过期，请重新导出")
    owner_task(db, row.task_id, user)
    return row


def ticket(db, artifact_id, user, key):
    row = artifact_for_user(db, artifact_id, user)
    identity = idem_key(user["uid"], "/artifacts/" + artifact_id + "/download-tickets", key)
    existing = db.get(TraceIdempotency, identity)
    if existing and existing.expires_at > now_shanghai():
        return existing.result
    if existing:
        db.delete(existing)
        db.flush()
    expires = now_shanghai() + timedelta(minutes=5)
    token = query.sign({"kind": "download", "artifact": artifact_id, "actor": str(user["uid"]),
                        "expires": time.time() + 300})
    result = {**artifact_dict(row), "download_url": "/api/v1/compliance/agent-traces/downloads?t=" + token,
              "expires_at": expires.isoformat() + "+08:00", "authorization_mode": "ticket"}
    try:
        db.add(TraceIdempotency(key=identity, payload_hash=hashlib.sha256(b"{}").hexdigest(),
            result=result, expires_at=now_shanghai() + timedelta(hours=24)))
        db.commit()
        return result
    except IntegrityError:
        db.rollback()
        return db.get(TraceIdempotency, identity).result


def download(db, token):
    data = query.unsign(token)
    if data.get("kind") != "download":
        fail(403, "不是下载票据")
    try:
        actor_id = int(data["actor"])
    except (KeyError, TypeError, ValueError):
        fail(403, "下载票据操作者无效")
    user = db.get(SystemUser, actor_id)
    if not user or user.status not in {"active", "normal"}:
        fail(403, "下载操作者已停用")
    from app.core.auth import permission_tier
    actor = {"uid": user.id, "tier": permission_tier(user.role)}
    artifact = artifact_for_user(db, data["artifact"], actor)
    path = directory() / artifact.storage_name
    if path.parent != directory() or not path.is_file():
        fail(410, "导出文件已清理")
    if hashlib.sha256(path.read_bytes()).hexdigest() != artifact.sha256:
        fail(409, "导出文件完整性校验失败")
    return artifact, path
