import hmac
import json
import re
from hashlib import sha256
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import load_secret
from app.core.time import now_shanghai
from app.models.anomaly_reviews import AnomalyReview
from app.models.tables import DatasetRecord, DatasetVersion, DatasetVersionItem, Task
from app.services.dataset_version_service import register_version
from app.services.governance_versions import GovernanceVersionScope, member, next_label, version_records
from app.services.lineage_service import record_process_edges


class ReviewScope(GovernanceVersionScope):
    def reviews(self, db: Session):
        return list(db.scalars(select(AnomalyReview).where(
            AnomalyReview.dataset_id == self.dataset.id,
            AnomalyReview.version_id == self.version.version_id,
            AnomalyReview.scheme_id == self.scheme,
            AnomalyReview.language == self.language,
            AnomalyReview.owner_digest == self.owner,
        ).order_by(AnomalyReview.candidate_id)).all())


def field_change(review):
    return {"field": "topic_label", "before": review.before_label, "after": review.after_label}


def review_view(review):
    return {
        "candidate_id": review.candidate_id, "status": review.status,
        "sample_id": str(review.record_id),
        "timeline": [{"at": event["at"], "message": event["message"]} for event in review.events],
    }


def transition(review, status, actor, message):
    now = now_shanghai()
    review.status = status
    review.updated_at = now
    review.events = [*review.events, {
        "at": now.isoformat(), "actor_id": actor["uid"],
        "message": f"{message}（{actor['username']}）",
    }]


def review_candidate(db, scope, result_id, sample_id, candidate, action, actor):
    if not scope.is_current:
        raise HTTPException(409, "当前数据集已有更新版本，请基于最新版本重新检测")
    if not re.fullmatch(r"[1-9][0-9]{0,9}", sample_id) or int(sample_id) > 2147483647:
        raise HTTPException(422, "修复建议没有有效的业务样本编号")
    original = member(db, scope, int(sample_id))
    changes = candidate.get("field_changes") or []
    checks = candidate.get("validation_results") or []
    if not checks or not all(check.get("passed") is True for check in checks):
        raise HTTPException(422, "修复建议未通过技术检查")
    if len(changes) != 1 or changes[0].get("field") != "topic_label":
        raise HTTPException(422, "目前仅支持复核单条主题标签修改")
    before, after = changes[0].get("before"), changes[0].get("after")
    if not all(isinstance(label, str) and 0 < len(label.strip()) <= len(label) <= 255
               for label in (before, after)) or before.strip() == after.strip():
        raise HTTPException(422, "主题标签修改无效")
    if original is None or (original.payload or {}).get("topic_label") != before:
        raise HTTPException(409, "样本不在该版本内，或原标签与检测结果不一致")
    candidate_id = candidate["candidate_id"]
    revision = candidate["input_sample_revision_id"]
    review = db.get(AnomalyReview, candidate_id)
    if review is not None and (
        review.owner_digest != scope.owner or review.result_id != result_id
        or review.dataset_id != scope.dataset.id or review.version_id != scope.version.version_id
        or review.record_id != original.id or review.input_revision != revision
        or (review.before_label, review.after_label) != (before, after)
    ):
        raise HTTPException(409, "候选来源或内容与已保存的复核记录不一致")
    target, allowed, message = {
        "submit": ("PENDING_REVIEW", {"DRAFT", "WITHDRAWN"}, "提交人工复核"),
        "approve": ("APPROVED", {"PENDING_REVIEW"}, "审核通过，加入待发布修改集"),
        "reject": ("REJECTED", {"PENDING_REVIEW"}, "驳回修复建议"),
        "withdraw": ("WITHDRAWN", {"PENDING_REVIEW"}, "撤回复核"),
    }[action]
    status = review.status if review else "DRAFT"
    if review is not None and status == target:
        return review_view(review)
    if status not in allowed:
        raise HTTPException(409, "该候选的状态已变化，请刷新后再操作")
    if target == "APPROVED" and any(
        row.record_id == original.id and row.status == "APPROVED" for row in scope.reviews(db)
    ):
        raise HTTPException(409, "这个样本已有一条批准的建议，请先处理修改集里的冲突")
    if review is None:
        review = AnomalyReview(
            candidate_id=candidate_id, result_id=result_id, owner_digest=scope.owner,
            dataset_id=scope.dataset.id, version_id=scope.version.version_id,
            scheme_id=scope.scheme, language=scope.language, record_id=original.id,
            input_revision=revision, before_label=before, after_label=after, events=[],
        )
        db.add(review)
    transition(review, target, actor, message)
    return review_view(review)


def change_set_view(scope, reviews, editable):
    approved = [row for row in reviews if row.status == "APPROVED"]
    return {
        "id": scope.version.version_id, "dataset_id": scope.dataset.id,
        "version_id": scope.version.version_id, "version_label": scope.version.label,
        "updated_at": max((row.updated_at for row in reviews), default=scope.version.created_at).isoformat(),
        "entries": [{"sample_id": str(row.record_id), "candidate_id": row.candidate_id,
                     "changes": [field_change(row)]} for row in approved],
        "excluded": [], "conflicts": len(approved) if not scope.is_current else 0,
        "actions": ["publish", "remove"] if editable and scope.is_current and approved else [],
    }


def remove_approval(db, scope, candidate_id, actor):
    reviews = scope.reviews(db)
    review = next((row for row in reviews if row.candidate_id == candidate_id), None)
    if not scope.is_current or review is None or review.status != "APPROVED":
        raise HTTPException(409, "该候选不在当前可编辑的待发布修改集中")
    transition(review, "WITHDRAWN", actor, "从待发布修改集移除")
    return change_set_view(scope, reviews, True)


def check_version(db, scope, actor, approved):
    errors = []
    if not scope.is_current:
        errors.append("数据集已有更新的版本，请重新检测后再发布")
    if not approved:
        errors.append("没有已批准的修改")
    if len({row.record_id for row in approved}) != len(approved):
        errors.append("同一样本有多条已批准的修改")
    for review in approved:
        original = member(db, scope, review.record_id)
        if original is None or (original.payload or {}).get("topic_label") != review.before_label:
            errors.append(f"样本 {review.record_id} 的原标签或版本已变化")
    fingerprint = json.dumps([
        actor["uid"], scope.owner, scope.dataset.id, scope.version.version_id,
        scope.version.manifest_hash, scope.scheme, scope.language,
        [(row.candidate_id, row.record_id, row.before_label, row.after_label, len(row.events)) for row in approved],
    ], ensure_ascii=False).encode("utf-8")
    return {
        "token": "" if errors else hmac.new(load_secret(), fingerprint, sha256).hexdigest(),
        "count": len(approved), "excluded": [], "valid": not errors, "errors": errors,
        "target_description": f"在「{scope.dataset.name}」下生成新版本，保留原版本 {scope.version.label}",
    }


def publish_version(db, scope, actor, token):
    reviews = scope.reviews(db)
    replay = next((row for row in reviews if row.status == "PUBLISHED"
                   and row.publication_token == token), None)
    if replay is not None:
        version = db.get(DatasetVersion, replay.published_version_id)
        if version is None:
            raise HTTPException(409, "已发布的版本不再存在")
        return {"new_dataset_version_id": version.version_id, "version_label": version.label}
    approved = [row for row in reviews if row.status == "APPROVED"]
    check = check_version(db, scope, actor, approved)
    if not check["valid"] or not hmac.compare_digest(token, check["token"]):
        raise HTTPException(409, "修改集已变化，请重新校验后发布")
    originals = list(db.scalars(version_records(scope).order_by(DatasetVersionItem.id)).all())
    if len(originals) != scope.version.sample_count:
        raise HTTPException(409, "基础版本清单不完整，未生成新版本")
    label, now = next_label(db, scope), now_shanghai()
    task_id = f"tsk_anomaly_repair_{uuid4().hex}"
    repair_by_record = {review.record_id: review for review in approved}
    updated_rows = {}
    for original in originals:
        review = repair_by_record.get(original.id)
        if review is None:
            continue
        payload = {**original.payload, "topic_label": review.after_label}
        digest = sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":")).encode("utf-8")).hexdigest()
        updated_rows[original.id] = DatasetRecord(
            dataset_id=scope.dataset.id, task_id=task_id, source_record_id=original.id,
            payload=payload, content_hash=digest,
        )
    if len(updated_rows) != len(approved):
        raise HTTPException(409, "待修复样本与基础版本清单不一致")
    summary = f"人工复核并修复 {len(approved)} 条主题标签"
    task = Task(
        task_id=task_id, name=summary, capability_code="anomaly_repair", status="succeeded",
        dataset_name=scope.dataset.name, dataset_version=label, progress=100,
        input_data={"dataset_id": scope.dataset.id, "version_id": scope.version.version_id},
        config={}, created_at=now, finished_at=now,
    )
    db.add(task)
    db.add_all(updated_rows.values())
    db.flush()
    record_ids = [updated_rows.get(row.id, row).id for row in originals]
    version = register_version(
        db, scope.dataset, label, record_ids, source_version_id=scope.version.version_id,
        producing_task_id=task_id, change_summary=summary,
    )
    if version.producing_task_id != task_id:
        raise HTTPException(409, "版本号已被其他任务使用，请重新发布")
    scope.dataset.version = version.label
    task.result = {"output_version_id": version.version_id, "output_version": version.label,
                   "output_sample_count": version.sample_count, "repaired_count": len(approved)}
    record_process_edges(
        db, task_id=task_id, source_version_id=scope.version.version_id,
        output_version_id=version.version_id, sample_count=version.sample_count,
        manifest_hash=version.manifest_hash, change_summary=summary,
    )
    for review in approved:
        transition(review, "PUBLISHED", actor, f"已发布到 {version.label}")
        review.published_version_id = version.version_id
        review.publication_token = token
    return {"new_dataset_version_id": version.version_id, "version_label": version.label}
