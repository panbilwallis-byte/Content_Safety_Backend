"""训练台账只读代理：信封解包、跨令牌错误映射与未配置令牌的显式报错（不触网）。"""
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.routers.training_tasks as module
from app.domain.schemas import CreateRealTrainingRequest
from app.models.tables import Base, Dataset, Model, TrainingTask


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", headers=None, content=b"x"):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {"content-type": "application/octet-stream"}
        self.content = content

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class TrainingProxyTest(unittest.TestCase):
    def setUp(self):
        self.original_token = module.LLM_ACCESS_TOKEN

    def tearDown(self):
        module.LLM_ACCESS_TOKEN = self.original_token

    def test_missing_token_returns_422(self):
        module.LLM_ACCESS_TOKEN = ""
        with self.assertRaises(HTTPException) as ctx:
            module.proxy_training_artifacts("t-1", authorization="")
        self.assertEqual(ctx.exception.status_code, 422)
    def test_success_unwraps_envelope_and_uses_server_token(self):
        module.LLM_ACCESS_TOKEN = "svc-token"
        fake = FakeResponse(payload={"code": 0, "data": [{"id": "a1", "filename": "outputs/best.pt"}]})
        with patch.object(module.httpx, "get", return_value=fake) as get:
            result = module.proxy_training_artifacts("t-1", authorization="")
        self.assertEqual(result["data"], [{"id": "a1", "filename": "outputs/best.pt"}])
        self.assertIn("/tasks/t-1/artifacts", get.call_args.args[0])
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer svc-token")

    def test_upstream_forbidden_maps_message_and_status(self):
        module.LLM_ACCESS_TOKEN = "svc-token"
        fake = FakeResponse(status_code=403,
                            payload={"message": "该训练任务属于另一个访问令牌，请切换为建单时使用的令牌"})
        with patch.object(module.httpx, "get", return_value=fake):
            with self.assertRaises(HTTPException) as ctx:
                module.proxy_training_artifacts("t-1", authorization="")
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("不属于" in str(ctx.exception.detail) or "另一个访问令牌" in str(ctx.exception.detail),
                      [True], ctx.exception.detail)

    def test_download_streams_bytes_with_disposition(self):
        module.LLM_ACCESS_TOKEN = "svc-token"
        fake = FakeResponse(payload={"code": 0, "data": []},
                            headers={"content-type": "application/octet-stream",
                                     "content-disposition": 'attachment; filename="best.pt"'},
                            content=b"weights")
        result = None
        with patch.object(module.httpx, "get", return_value=fake):
            result = module.proxy_training_artifact_download("t-1", "a1", authorization="")
        self.assertEqual(result.body, b"weights")
        self.assertIn('filename="best.pt"', result.headers["content-disposition"])


class CreateRealTaskTokenTest(unittest.TestCase):
    """建单必须用服务端令牌；上游 401/403 映射 502，避免前端误判平台登录过期。"""

    def setUp(self):
        engine = create_engine("sqlite+pysqlite:///:memory:",
                               connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        self.original_session = module.SessionLocal
        self.original_token = module.LLM_ACCESS_TOKEN
        self.original_secret = module.LLM_SETUP_SECRET
        module.SessionLocal = self.Session
        module.LLM_SETUP_SECRET = "setup-secret"
        with self.Session() as db:
            db.add(Model(id=7, name="m", version="4.1.3"))
            db.add(Dataset(id=7, name="d", version="v1.0.0", metadata_json={"usage": "test"}))
            db.commit()

    def tearDown(self):
        module.SessionLocal = self.original_session
        module.LLM_ACCESS_TOKEN = self.original_token
        module.LLM_SETUP_SECRET = self.original_secret

    def _request(self):
        return CreateRealTrainingRequest(
            name="建单验证", model_id="7", base_version="4.1.3", dataset_id="7",
            dataset_version="v1.0.0", target_version="4.1.4", epochs=2,
            learning_rate=0.05, batch_size=8, method="低秩适配微调")

    def test_create_uses_server_token_not_platform_header(self):
        module.LLM_ACCESS_TOKEN = "svc-token"
        fake = FakeResponse(status_code=202,
                            payload={"code": 0, "data": {"id": "11111111-1111-1111-1111-111111111111"}})
        with patch.object(module.httpx, "post", return_value=fake) as post:
            result = module.create_real_training_task(self._request(), authorization="Bearer platform-token")
        sent = post.call_args.kwargs["headers"]
        self.assertEqual(sent["Authorization"], "Bearer svc-token")
        self.assertEqual(sent["X-Setup-Secret"], "setup-secret")
        self.assertEqual(result["data"]["llm_task_id"], "11111111-1111-1111-1111-111111111111")
        with self.Session() as db:
            self.assertIsNotNone(db.get(TrainingTask, "11111111-1111-1111-1111-111111111111"))

    def test_upstream_401_maps_to_502(self):
        module.LLM_ACCESS_TOKEN = "svc-token"
        fake = FakeResponse(status_code=401, payload={"message": "Missing, invalid, disabled, or expired token"})
        with patch.object(module.httpx, "post", return_value=fake):
            with self.assertRaises(HTTPException) as ctx:
                module.create_real_training_task(self._request(), authorization="")
        self.assertEqual(ctx.exception.status_code, 502)


if __name__ == "__main__":
    unittest.main()
