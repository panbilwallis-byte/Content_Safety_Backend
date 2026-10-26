"""模型服务真实探活（#2.4）：探不通不得记为已核验，占位地址如实拒绝且不改状态。"""
import unittest
from types import SimpleNamespace

import httpx
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.tables import Base, ModelService
from app.routers import model_workbench as module


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class FakeClient:
    behaviour = None

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def get(self, url):
        return FakeClient.behaviour(url)


def stub_httpx(behaviour):
    FakeClient.behaviour = behaviour
    return SimpleNamespace(Client=FakeClient, TimeoutException=httpx.TimeoutException,
                           HTTPError=httpx.HTTPError)


class ServiceProbeTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        self.original_session = module.SessionLocal
        self.original_httpx = module.httpx
        module.SessionLocal = self.Session

    def tearDown(self):
        module.SessionLocal = self.original_session
        module.httpx = self.original_httpx

    def _service(self, endpoint, status="deployed", healthy=None):
        with self.Session() as db:
            service = ModelService(
                id="SVC-PROBE", name="探活测试服务", model_id="1", version="v1.0.0",
                service_type="external", status=status, endpoint=endpoint,
                healthy=healthy, latency_ms=None, checked_at=None)
            db.add(service)
            db.commit()

    def _row(self):
        with self.Session() as db:
            return db.get(ModelService, "SVC-PROBE")

    def test_unreachable_when_check_never_ran(self):
        self._service("https://model.invalid/healthz")
        row = self._row()
        self.assertIsNone(row.healthy)
        self.assertIsNone(row.checked_at)

    def test_local_placeholder_is_refused_without_claiming_verification(self):
        self._service("local://content-safety-model", status="running", healthy=True)
        with self.assertRaises(HTTPException) as caught:
            module.check_model_service("SVC-PROBE")
        self.assertEqual(caught.exception.status_code, 422)
        self.assertIn("不是可探活的 HTTP 地址", caught.exception.detail)
        row = self._row()
        self.assertTrue(row.healthy)      # 拒绝探活不得顺手改写已有状态
        self.assertIsNone(row.checked_at)

    def test_reachable_endpoint_records_running_and_real_latency(self):
        self._service("http://127.0.0.1:8010/healthz")
        module.httpx = stub_httpx(lambda url: FakeResponse(200))
        body = module.check_model_service("SVC-PROBE")
        self.assertIn("连接检查通过", body["message"])
        row = self._row()
        self.assertEqual((row.status, row.healthy), ("running", True))
        self.assertGreaterEqual(row.latency_ms, 1)
        self.assertIsNotNone(row.checked_at)

    def test_http_error_marks_offline_and_keeps_latency(self):
        self._service("http://127.0.0.1:8010/healthz", status="running", healthy=True)
        module.httpx = stub_httpx(lambda url: FakeResponse(503))
        body = module.check_model_service("SVC-PROBE")
        self.assertIn("HTTP 503", body["message"])
        row = self._row()
        self.assertEqual((row.status, row.healthy), ("offline", False))

    def test_timeout_marks_offline_without_fake_latency(self):
        self._service("http://127.0.0.1:8010/healthz")

        def hang(url):
            raise httpx.TimeoutException("timeout")

        module.httpx = stub_httpx(hang)
        body = module.check_model_service("SVC-PROBE")
        self.assertIn("3 秒内未响应", body["message"])
        row = self._row()
        self.assertEqual((row.status, row.healthy, row.latency_ms), ("offline", False, None))

    def test_connection_error_reports_reason(self):
        self._service("http://127.0.0.1:1/healthz")

        def refuse(url):
            raise httpx.ConnectError("refused")

        module.httpx = stub_httpx(refuse)
        body = module.check_model_service("SVC-PROBE")
        self.assertIn("ConnectError", body["message"])
        self.assertEqual(self._row().status, "offline")


if __name__ == "__main__":
    unittest.main()
