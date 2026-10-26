"""训练台账只读代理：信封解包、跨令牌错误映射与未配置令牌的显式报错（不触网）。"""
import unittest
from unittest.mock import patch

from fastapi import HTTPException

import app.routers.training_tasks as module


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


if __name__ == "__main__":
    unittest.main()
