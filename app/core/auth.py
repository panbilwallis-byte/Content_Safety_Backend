"""平台登录鉴权核心（#9）：PBKDF2 口令散列、HMAC 签名令牌、只读/操作两级权限。

令牌为无状态签名串（payload.signature），一年有效；密钥取环境变量
AUTH_SECRET，缺省时持久化在 storage/auth_secret.key（不进 git）。
口令散列用标准库 pbkdf2_sha256，不引入额外依赖。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

ITERATIONS = 260_000
# 演示期长登录：一年有效。无状态令牌无法单独吊销，公开部署前应调短并轮换密钥。
TOKEN_TTL_SECONDS = 365 * 24 * 3600
# 只读角色：能看不能写，写方法在中间件层统一拦截。
READER_ROLES = {"审计员", "auditor", "只读"}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        ITERATIONS,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt_b64, digest_b64 = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), base64.b64decode(salt_b64), int(iterations))
        return hmac.compare_digest(expected, actual)
    except (ValueError, TypeError):
        return False


def permission_tier(role: str) -> str:
    return "reader" if (role or "").strip() in READER_ROLES else "operator"


def load_secret() -> bytes:
    env = os.getenv("AUTH_SECRET")
    if env:
        return env.encode("utf-8")
    path = Path(__file__).resolve().parents[2] / "storage" / "auth_secret.key"
    if path.exists():
        return path.read_bytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_bytes(32)
    path.write_bytes(secret)
    return secret


def create_token(user_id: int, username: str, role: str,
                 secret: bytes | None = None, now: float | None = None) -> str:
    secret = secret or load_secret()
    issued = time.time() if now is None else now
    payload = {
        "uid": user_id,
        "username": username,
        "role": role,
        "tier": permission_tier(role),
        "exp": int(issued + TOKEN_TTL_SECONDS),
    }
    body = base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("ascii").rstrip("=")
    signature = hmac.new(secret, body.encode("ascii"), hashlib.sha256).hexdigest()
    return body + "." + signature


def parse_token(token: str, secret: bytes | None = None,
                now: float | None = None) -> dict | None:
    """校验签名与有效期并返回身份；任何一环不合规返回 None（不猜测身份）。"""
    secret = secret or load_secret()
    body, separator, signature = (token or "").partition(".")
    if not separator or not body:
        return None
    expected = hmac.new(secret, body.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return None
    try:
        padded = body + "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or int(payload.get("exp", 0)) < (time.time() if now is None else now):
        return None
    return payload
