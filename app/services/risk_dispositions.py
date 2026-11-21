import hmac
import json
from hashlib import sha256
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import load_secret
from app.core.time import now_shanghai
from app.models.risk_disposition import RiskDisposition
from app.models.tables import DatasetRecord, DatasetVersion, DatasetVersionItem, Task
from app.services.database_ingest import record_hash
from app.services.dataset_version_service import register_version
from app.services.governance_versions import GovernanceVersionScope, member, next_label, version_records
from app.services.lineage_service import record_process_edges


LEVELS = {"NOTICE", "LOW", "MEDIUM", "HIGH"}
REFERENCE_LABELS = {"reference_risk_level", "reference_value_tier", "reference_is_anomaly"}


def list_reviews(db: Session, scope: GovernanceVersionScope, result_id: str):
    return list(db.scalars(select(RiskDisposition).where(
        RiskDisposition.result_id == result_id,
        RiskDisposition.owner_digest == scope.owner,
        RiskDisposition.dataset_id == scope.dataset.id,
        RiskDisposition.version_id == scope.version.version_id,
    ).order_by(RiskDisposition.record_id)).all())


def review_view(row: RiskDisposition):
    return {
        "sample_id": str(row.record_id),
        "input_sample_revision_id": row.input_revision,
        "original_level": row.original_level,
        "original_category": row.original_category,
        "status": row.status,
        "decision": row.decision,
        "reviewed_level": row.reviewed_level,
        "treatment": row.treatment,
        "replacement_text": row.replacement_text,
        "events": row.events,
        "published_version_id": row.published_version_id,
    }


def change_status(row, status, action, note, actor):
    now = now_shanghai()
    row.status = status
    row.updated_at = now
    row.events = [*row.events, {
        "action": action, "note": note, "at": now.isoformat(),
        "actor_id": actor["uid"], "actor": actor["username"],
    }]


def change_review(db: Session, scope, result_id, record_id, command, actor, sample=None):
    if not scope.is_current:
        raise HTTPException(409, "固定版本已有更新版本，请在新版本上重新检测后再处置")
    original = member(db, scope, record_id)
    if original is None:
        raise HTTPException(409, "样本不在检测对应的固定版本内")
    row = db.get(RiskDisposition, (result_id, record_id))
    digest = record_hash(original.payload)
    if row is not None and (
        row.owner_digest != scope.owner or row.dataset_id != scope.dataset.id
        or row.version_id != scope.version.version_id
        or row.input_revision != command.input_sample_revision_id or row.input_digest != digest
    ):
        raise HTTPException(409, "检测结果、样本修订或原始内容已变化，请刷新后重试")

    action = command.action
    if action == "submit":
        if row is not None:
            return review_view(row)
        level = sample.get("maximum_suggested_level")
        category = sample.get("primary_category")
        if not sample.get("findings") or level not in LEVELS or not category:
            raise HTTPException(422, "该样本没有可复核的风险依据")
        row = RiskDisposition(
            result_id=result_id, record_id=record_id, owner_digest=scope.owner,
            dataset_id=scope.dataset.id, version_id=scope.version.version_id,
            input_revision=command.input_sample_revision_id, input_digest=digest,
            original_level=level, original_category=category, events=[],
        )
        db.add(row)
        change_status(row, "PENDING_REVIEW", action, "", actor)
        return review_view(row)

    allowed = {
        "confirm": {"PENDING_REVIEW"}, "adjust": {"PENDING_REVIEW"},
        "dismiss": {"PENDING_REVIEW"}, "propose": {"PENDING_ACTION"},
        "approve": {"PENDING_APPROVAL"}, "return": {"PENDING_APPROVAL", "APPROVED"},
    }
    if row is None or row.status not in allowed[action]:
        raise HTTPException(409, "当前状态无法执行该操作，请刷新复核记录")
    note = command.note.strip()
    if not note:
        raise HTTPException(422, "请填写操作理由")
    if action in ("confirm", "adjust", "dismiss"):
        if action == "adjust" and (command.level is None or command.level == row.original_level):
            raise HTTPException(422, "请选择与模型建议不同的等级")
        row.decision = action
        row.reviewed_level = (command.level if action == "adjust" else
                              row.original_level if action == "confirm" else None)
        status = "DISMISSED" if action == "dismiss" else "PENDING_ACTION"
    elif action == "propose":
        if not command.treatment:
            raise HTTPException(422, "请选择处置方式")
        replacement = (command.replacement_text or "").strip()
        if command.treatment != "exclude":
            text = original.payload.get("text")
            if not isinstance(text, str) or not replacement or replacement == text.strip():
                raise HTTPException(422, "请填写与原正文不同的处置后正文")
        row.treatment = command.treatment
        row.replacement_text = replacement if command.treatment != "exclude" else None
        status = "PENDING_APPROVAL"
    else:
        status = "APPROVED" if action == "approve" else "PENDING_ACTION"
    change_status(row, status, action, note, actor)
    return review_view(row)


def approved_preview(db: Session, scope, result_id, actor, approved):
    if not scope.is_current:
        raise HTTPException(409, "数据集已有更新版本，请在新版本上重新检测")
    if not approved:
        raise HTTPException(409, "没有已审核通过的处置")
    for row in approved:
        original = member(db, scope, row.record_id)
        if original is None or record_hash(original.payload) != row.input_digest:
            raise HTTPException(409, f"样本 {row.record_id} 的原始内容或版本已变化")
    excluded = sum(row.treatment == "exclude" for row in approved)
    remaining = scope.version.sample_count - excluded
    if remaining <= 0:
        raise HTTPException(422, "排除后没有可复检的样本，请调整处置清单")
    payload = [
        actor["uid"], scope.owner, result_id, scope.version.version_id, scope.version.manifest_hash,
        [(row.record_id, row.input_digest, row.treatment, row.replacement_text, len(row.events))
         for row in approved],
    ]
    fingerprint = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return {
        "token": hmac.new(load_secret(), fingerprint, sha256).hexdigest(),
        "source_version_label": scope.version.label,
        "replaced_count": len(approved) - excluded, "excluded_count": excluded,
        "sample_count": remaining,
    }


def published_view(version: DatasetVersion):
    return {"new_dataset_version_id": version.version_id, "version_label": version.label,
            "sample_count": version.sample_count}


def publish(db: Session, scope, result_id, actor, token):
    rows = list_reviews(db, scope, result_id)
    prior = next((r for r in rows if r.status == "PUBLISHED" and r.publication_token == token), None)
    if prior:
        version = db.get(DatasetVersion, prior.published_version_id)
        if version is None:
            raise HTTPException(409, "已发布的版本不存在")
        return published_view(version)
    approved = [row for row in rows if row.status == "APPROVED"]
    check = approved_preview(db, scope, result_id, actor, approved)
    if not hmac.compare_digest(token, check["token"]):
        raise HTTPException(409, "已批准的处置清单发生变化，请重新预览")
    originals = list(db.scalars(version_records(scope).order_by(DatasetVersionItem.id)).all())
    if len(originals) != scope.version.sample_count:
        raise HTTPException(409, "原始版本清单不完整")
    approved_by_record = {row.record_id: row for row in approved}
    label, now = next_label(db, scope), now_shanghai()
    task_id = f"tsk_risk_disposition_{uuid4().hex}"
    summary = f"风险处置：替换正文 {check['replaced_count']} 条，排除 {check['excluded_count']} 条"
    task = Task(
        task_id=task_id, name=summary, capability_code="risk_disposition", status="succeeded",
        dataset_name=scope.dataset.name, dataset_version=label, progress=100,
        input_data={"dataset_id": scope.dataset.id, "version_id": scope.version.version_id,
                    "risk_result_id": result_id},
        config={}, created_at=now, finished_at=now,
    )
    db.add(task)
    version_rows = []
    for original in originals:
        review = approved_by_record.get(original.id)
        if review is None:
            version_rows.append(original)
        elif review.treatment != "exclude":
            # 参考答案与输入正文绑定，衍生正文需要重新标注。
            payload = {key: value for key, value in original.payload.items() if key not in REFERENCE_LABELS}
            payload["text"] = review.replacement_text
            derived = DatasetRecord(
                dataset_id=scope.dataset.id, task_id=task_id, source_record_id=original.id,
                payload=payload, content_hash=record_hash(payload),
            )
            db.add(derived)
            version_rows.append(derived)
    db.flush()
    version = register_version(
        db, scope.dataset, label, [row.id for row in version_rows],
        source_version_id=scope.version.version_id, producing_task_id=task_id, change_summary=summary,
    )
    if version.producing_task_id != task_id:
        raise HTTPException(409, "版本号已被其他任务使用，请重试")
    scope.dataset.version = version.label
    task.result = {"output_version_id": version.version_id, "output_version": version.label,
                   "output_sample_count": version.sample_count, "replaced_count": check["replaced_count"],
                   "excluded_count": check["excluded_count"]}
    record_process_edges(
        db, task_id=task_id, source_version_id=scope.version.version_id,
        output_version_id=version.version_id, sample_count=version.sample_count,
        manifest_hash=version.manifest_hash, change_summary=summary,
    )
    for row in approved:
        change_status(row, "PUBLISHED", "publish", version.label, actor)
        row.published_version_id = version.version_id
        row.publication_token = token
    return published_view(version)
