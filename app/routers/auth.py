"""登录与当前身份接口（#9）。"""
from fastapi import APIRouter, Depends, HTTPException, Request
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
