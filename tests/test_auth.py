"""登录鉴权核心（#9）：口令散列、签名令牌、两级权限与登录接口。"""
import time
import unittest

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.auth import (
    create_token,
    hash_password,
    parse_token,
    permission_tier,
    verify_password,
)
from app.core.time import now_shanghai
from app.models.tables import Base, SystemUser
from app.routers.auth import LoginRequest, login

SECRET = b"unit-test-secret-0123456789abcdef"


class AuthCoreTest(unittest.TestCase):
    def test_password_roundtrip_rejects_wrong_input(self):
        stored = hash_password("admin123")
        self.assertTrue(stored.startswith("pbkdf2_sha256$"))
        self.assertTrue(verify_password("admin123", stored))
        self.assertFalse(verify_password("wrong", stored))
        self.assertFalse(verify_password("admin123", ""))
        self.assertFalse(verify_password("admin123", "garbage"))

    def test_token_roundtrip_and_tamper_detection(self):
        token = create_token(1, "admin", "超级管理员", secret=SECRET, now=1_700_000_000)
        payload = parse_token(token, secret=SECRET, now=1_700_000_100)
        self.assertEqual(payload["username"], "admin")
        self.assertEqual(payload["tier"], "operator")
        self.assertIsNone(parse_token(token + "x", secret=SECRET))
        self.assertIsNone(parse_token(token, secret=b"other-secret-0123456789abcdef01234567"))
        self.assertIsNone(parse_token("not-a-token", secret=SECRET))

    def test_expired_token_rejected(self):
        token = create_token(1, "admin", "超级管理员", secret=SECRET, now=1_700_000_000)
        self.assertIsNone(parse_token(token, secret=SECRET, now=time.time() + 10 ** 9))

    def test_auditor_role_maps_to_reader(self):
        self.assertEqual(permission_tier("审计员"), "reader")
        self.assertEqual(permission_tier("超级管理员"), "operator")
        self.assertEqual(permission_tier(""), "operator")


class LoginEndpointTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.db.add(SystemUser(
            username="admin", display_name="系统管理员", email="a@x.local",
            role="超级管理员", status="active",
            password_hash=hash_password("admin123"), created_at=now_shanghai()))
        self.db.add(SystemUser(
            username="ghost", display_name="停用号", email="g@x.local",
            role="普通用户", status="disabled",
            password_hash=hash_password("x123456"), created_at=now_shanghai()))
        self.db.commit()

    def test_login_success_returns_token_and_identity(self):
        result = login(LoginRequest(username="admin", password="admin123"), db=self.db)
        self.assertEqual(result["code"], 0)
        self.assertEqual(result["data"]["user"]["tier"], "operator")
        payload = parse_token(result["data"]["token"])
        self.assertEqual(payload["username"], "admin")

    def test_login_failures_share_one_message(self):
        cases = [
            LoginRequest(username="admin", password="bad"),
            LoginRequest(username="nobody", password="whatever"),
            LoginRequest(username="ghost", password="x123456"),
        ]
        for request in cases:
            with self.assertRaises(HTTPException) as caught:
                login(request, db=self.db)
            self.assertEqual(caught.exception.status_code, 401)
            self.assertEqual(caught.exception.detail, "账号或密码错误")


if __name__ == "__main__":
    unittest.main()
