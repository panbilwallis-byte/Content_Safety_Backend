import os
import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Annotated, Literal
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.exc import IntegrityError

from app.core.database import SessionLocal
from app.core.response import success
from app.services import anomaly_reviews as reviews

router = APIRouter(prefix="/data-governance/anomaly-reviews")
MODEL_ORIGIN = os.getenv("LLM_GOVERNANCE_ORIGIN", "http://127.0.0.1:8001").rstrip("/")
ANOMALY_RESULTS = "/api/v1/data-governance/anomaly-results"
REPAIR_SCHEMES = {"anomaly-basic-v2", "anomaly-basic-v2.1"}
ModelToken = Annotated[str, Header(alias="X-Model-Access-Token")]
CandidateAction = Literal["submit", "approve", "reject", "withdraw"]


class ResultScope(BaseModel):
    dataset_id: int = Field(gt=0)
    version_id: str = Field(min_length=1, max_length=100)
    scheme_id: str = Field(min_length=1, max_length=64)
    language: str = Field(min_length=1, max_length=24)


class CandidateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: str = Field(min_length=1, max_length=64)
    input_sample_revision_id: str = Field(min_length=1, max_length=128)


class RemoveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: str = Field(min_length=1, max_length=64)


class PublishInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    validation_token: str = Field(pattern=r"^[a-f0-9]{64}$")


@dataclass(frozen=True)
class ReviewContext:
    result_id: str
    model_token: str
    owner: str
    actor: dict
    scope: dict

    @property
    def writable(self):
        return self.actor.get("tier") == "operator" and self.scope["scheme_id"] in REPAIR_SCHEMES

    def require_write(self):
        if not self.writable:
            raise HTTPException(403, "当前账号或检测方案不支持修复发布")

    def result_path(self):
        return f"{ANOMALY_RESULTS}/{quote(self.result_id, safe='')}"


def read_model(path: str, token: str, subject: str = "异常检测") -> dict:
    try:
        response = httpx.get(
            f"{MODEL_ORIGIN}{path}", headers={"Authorization": f"Bearer {token}"},
            timeout=10, follow_redirects=False, trust_env=False,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"无法连接模型服务核验{subject}结果") from exc
    if response.status_code in (401, 403):
        raise HTTPException(403, f"模型访问令牌无效，或无权查看该{subject}结果")
    if response.status_code == 404:
        raise HTTPException(404, f"{subject}结果或样本不存在，或不属于当前模型令牌")
    try:
        body = response.json()
        if response.status_code == 200 and body.get("code") == 0 and isinstance(body.get("data"), dict):
            return body["data"]
    except (ValueError, AttributeError):
        pass
    raise HTTPException(502, f"模型服务未返回可核验的{subject}结果")


def review_context(result_id: str, request: Request, model_token: ModelToken) -> ReviewContext:
    if (not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", result_id)
        or not 32 <= len(model_token) <= 512 or not model_token.isascii()):
        raise HTTPException(422, "异常检测结果编号或令牌格式无效")
    actor = getattr(request.state, "user", None)
    if not actor:
        raise HTTPException(401, "请先登录业务平台")
    result = read_model(f"{ANOMALY_RESULTS}/{quote(result_id, safe='')}", model_token)
    if result.get("id") != result_id:
        raise HTTPException(502, "模型服务返回了不匹配的异常检测结果")
    try:
        scope = ResultScope.model_validate(result.get("scope")).model_dump()
    except ValidationError as exc:
        raise HTTPException(502, "模型服务缺少有效的数据集和版本信息") from exc
    return ReviewContext(result_id, model_token, sha256(model_token.encode()).hexdigest(), actor, scope)


Context = Annotated[ReviewContext, Depends(review_context)]


def commit(db):
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "发生并发修改，请刷新后重试") from exc


@router.get("/{result_id}/state")
def get_state(context: Context):
    with SessionLocal() as db:
        scope = reviews.ReviewScope(db, context.scope, context.owner)
        rows = scope.reviews(db)
        editable = context.writable and scope.is_current
        return success({
            "editable": editable,
            "reviews": [reviews.review_view(row) for row in rows if row.result_id == context.result_id],
            "change_set": reviews.change_set_view(scope, rows, editable),
        })


@router.post("/{result_id}/samples/{sample_id}/candidates/{action}")
def update_candidate(sample_id: str, action: CandidateAction, payload: CandidateInput, context: Context):
    context.require_write()
    if not re.fullmatch(r"[1-9][0-9]{0,9}", sample_id):
        raise HTTPException(422, "样本编号无效")
    sample = read_model(f"{context.result_path()}/samples/{quote(sample_id, safe='')}", context.model_token)
    candidate = next((item for item in sample.get("candidates", [])
                      if item.get("candidate_id") == payload.candidate_id), None)
    if (sample.get("id") != sample_id or candidate is None
        or sample.get("revision_id") != payload.input_sample_revision_id
        or candidate.get("input_sample_revision_id") != payload.input_sample_revision_id):
        raise HTTPException(409, "候选或输入修订不匹配，请重新加载检测结果")
    with SessionLocal() as db:
        scope = reviews.ReviewScope(db, context.scope, context.owner, lock=True)
        result = reviews.review_candidate(
            db, scope, context.result_id, sample_id, candidate, action, context.actor,
        )
        commit(db)
        return success(result)


@router.post("/{result_id}/change-set/remove")
def remove_approval(payload: RemoveInput, context: Context):
    context.require_write()
    with SessionLocal() as db:
        scope = reviews.ReviewScope(db, context.scope, context.owner, lock=True)
        result = reviews.remove_approval(db, scope, payload.candidate_id, context.actor)
        commit(db)
        return success(result)


@router.post("/{result_id}/change-set/validate")
def validate_version(context: Context):
    context.require_write()
    with SessionLocal() as db:
        scope = reviews.ReviewScope(db, context.scope, context.owner)
        approved = [row for row in scope.reviews(db) if row.status == "APPROVED"]
        return success(reviews.check_version(db, scope, context.actor, approved))


@router.post("/{result_id}/change-set/generate-version")
def generate_version(payload: PublishInput, context: Context):
    context.require_write()
    try:
        with SessionLocal() as db:
            scope = reviews.ReviewScope(db, context.scope, context.owner, lock=True)
            result = reviews.publish_version(db, scope, context.actor, payload.validation_token)
            commit(db)
            return success(result)
    except IntegrityError as exc:
        raise HTTPException(409, "版本清单发生并发冲突，请刷新后重新校验") from exc
