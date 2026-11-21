import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.exc import IntegrityError

from app.core.database import SessionLocal
from app.core.response import success
from app.models.tables import DatasetVersion
from app.routers.anomaly_reviews import ModelToken, PublishInput, ResultScope, read_model
from app.services import risk_dispositions as dispositions
from app.services.governance_versions import GovernanceVersionScope


router = APIRouter(prefix="/data-governance/risk-dispositions")
RISK_RESULTS = "/api/v1/data-governance/risk-results"


class RiskCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["submit", "confirm", "adjust", "dismiss", "propose", "approve", "return"]
    input_sample_revision_id: str = Field(min_length=1, max_length=128)
    note: str = Field(default="", max_length=1000)
    level: Literal["NOTICE", "LOW", "MEDIUM", "HIGH"] | None = None
    treatment: Literal["redact", "correct", "exclude"] | None = None
    replacement_text: str | None = Field(default=None, max_length=12000)


@dataclass(frozen=True)
class RiskContext:
    result_id: str
    actor: dict
    owner: str
    scope: dict
    manifest_hash: str | None
    token: str

    def open_scope(self, db, *, write=False, lock=False):
        if write and self.actor.get("tier") != "operator":
            raise HTTPException(403, "当前账号无权执行风险处置")
        scope = GovernanceVersionScope(db, self.scope, self.owner, lock=lock)
        if write and not self.matches_manifest(scope):
            raise HTTPException(409, "检测结果与业务数据版本的清单不一致，请重新检测")
        return scope

    def matches_manifest(self, scope):
        return bool(self.manifest_hash and self.manifest_hash == scope.version.manifest_hash)


def risk_context(result_id: str, request: Request, model_token: ModelToken) -> RiskContext:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", result_id) or not (
        32 <= len(model_token) <= 512 and model_token.isascii()
    ):
        raise HTTPException(422, "风险检测结果编号或令牌格式无效")
    actor = getattr(request.state, "user", None)
    if not actor:
        raise HTTPException(401, "请先登录业务平台")
    result = read_model(f"{RISK_RESULTS}/{quote(result_id, safe='')}", model_token, "风险检测")
    if result.get("id") != result_id:
        raise HTTPException(502, "模型服务返回了不匹配的风险检测结果")
    try:
        scope = ResultScope.model_validate(result.get("scope")).model_dump()
    except ValidationError as exc:
        raise HTTPException(502, "模型服务返回的数据范围无效") from exc
    manifest = result.get("manifest_hash")
    return RiskContext(
        result_id, actor, sha256(model_token.encode()).hexdigest(), scope,
        manifest if isinstance(manifest, str) else None, model_token,
    )


Context = Annotated[RiskContext, Depends(risk_context)]


@router.get("/{result_id}/state")
def get_state(context: Context):
    with SessionLocal() as db:
        scope = context.open_scope(db)
        rows = dispositions.list_reviews(db, scope, context.result_id)
        published_id = next((row.published_version_id for row in rows if row.published_version_id), None)
        published = db.get(DatasetVersion, published_id) if published_id else None
        editable = context.actor.get("tier") == "operator" and context.matches_manifest(scope)
        return success({
            "editable": editable and scope.is_current,
            "source_version_label": scope.version.label,
            "reviews": [dispositions.review_view(row) for row in rows],
            "published": dispositions.published_view(published) if published else None,
        })


@router.post("/{result_id}/samples/{sample_id}")
def update_sample(sample_id: str, payload: RiskCommand, context: Context):
    if not re.fullmatch(r"[1-9][0-9]{0,9}", sample_id) or int(sample_id) > 2147483647:
        raise HTTPException(422, "样本编号无效")
    sample = None
    if payload.action == "submit":
        sample = read_model(f"{RISK_RESULTS}/{quote(context.result_id, safe='')}/samples/{sample_id}",
                            context.token, "风险检测")
        if (sample.get("id") != sample_id or sample.get("revision_id") != payload.input_sample_revision_id
                or sample.get("version_id") != context.scope["version_id"]
                or sample.get("dataset_id") != context.scope["dataset_id"]):
            raise HTTPException(409, "模型样本与检测范围或输入修订不匹配")
    try:
        with SessionLocal() as db:
            scope = context.open_scope(db, write=True, lock=True)
            row = dispositions.change_review(
                db, scope, context.result_id, int(sample_id), payload, context.actor, sample,
            )
            db.commit()
            return success(row)
    except IntegrityError as exc:
        raise HTTPException(409, "复核记录发生并发变化，请刷新后重试") from exc


@router.post("/{result_id}/preview")
def preview_publication(context: Context):
    with SessionLocal() as db:
        scope = context.open_scope(db, write=True)
        rows = dispositions.list_reviews(db, scope, context.result_id)
        approved = [row for row in rows if row.status == "APPROVED"]
        return success(dispositions.approved_preview(db, scope, context.result_id, context.actor, approved))


@router.post("/{result_id}/publish")
def publish_version(payload: PublishInput, context: Context):
    try:
        with SessionLocal() as db:
            scope = context.open_scope(db, write=True, lock=True)
            result = dispositions.publish(
                db, scope, context.result_id, context.actor, payload.validation_token,
            )
            db.commit()
            return success(result)
    except IntegrityError as exc:
        raise HTTPException(409, "数据版本发生并发变化，请刷新后重新预览") from exc
