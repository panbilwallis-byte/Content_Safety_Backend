"""登录与当前身份接口（#9）。"""
from datetime import datetime, timedelta, timezone
import os
from secrets import token_urlsafe

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import create_token, permission_tier, verify_password
from app.core.database import get_db
from app.core.response import success
from app.models.tables import SystemUser

router = APIRouter()


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


@router.post("/auth/login")
def login(request: LoginRequest, db: Session = Depends(get_db)):
    user = db.scalar(select(SystemUser).where(SystemUser.username == request.username.strip()))
    # 账号不存在与密码错误返回同一提示，避免账号枚举。
    if user is None or user.status != "active" or not verify_password(
            request.password, user.password_hash or ""):
        raise HTTPException(status_code=401, detail="账号或密码错误")
    token = create_token(user.id, user.username, user.role)
    return success({
        "token": token,
        "user": {
            "username": user.username,
            "display_name": user.display_name,
            "role": user.role,
            "tier": permission_tier(user.role),
        },
    }, message="登录成功")


@router.get("/auth/me")
def me(request: Request):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    return success(user)


@router.post("/auth/model-service-token")
def model_service_token(request: Request, response: Response, db: Session = Depends(get_db)):
    """Create a separate, short lived model token for an active platform operator."""
    if os.getenv("DOCKER_LLM_AUTO_TOKEN") != "true":
        raise HTTPException(status_code=404, detail="模型令牌自动签发未启用")
    identity = getattr(request.state, "user", {})
    user = db.get(SystemUser, int(identity["uid"])) if identity else None
    if user is None or user.status != "active" or permission_tier(user.role) != "operator":
        raise HTTPException(status_code=403, detail="当前账号无法自动获取模型访问令牌")

    origin = os.getenv("LLM_GOVERNANCE_ORIGIN")
    setup_secret = os.getenv("APP_SETUP_SECRET")
    if not origin or not setup_secret:
        raise HTTPException(status_code=503, detail="模型令牌服务尚未配置")
    token = token_urlsafe(48)
    expires_at = datetime.now(timezone.utc) + timedelta(days=7)
    try:
        with httpx.Client(timeout=5, trust_env=False) as client:
            result = client.post(
                origin.rstrip("/") + "/api/auth/token",
                headers={"X-Setup-Secret": setup_secret},
                json={"token": token, "expires_at": expires_at.isoformat(), "label": f"docker-web-user-{user.id}"},
            )
        if result.status_code != 201:
            raise HTTPException(status_code=503, detail="模型令牌签发未完成")
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail="暂时无法连接模型令牌服务") from exc
    response.headers["Cache-Control"] = "no-store"
    return success({"token": token, "expires_at": expires_at.isoformat()})
