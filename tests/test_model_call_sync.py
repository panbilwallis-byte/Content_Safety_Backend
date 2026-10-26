"""POST /model-calls 写留痕必须当场补挂 tasks 流水。

回归背景：SessionLocal 是 autoflush=False，record_model_call 里新 add 的
ModelCall 在 flush 前对 SELECT 不可见，导致补挂查询空转——只有服务重启时
main.py 的启动回填才把流水补上，表现为"新调用在全链路追踪页不显示，
重启一次才出来一批"。本测试直接调用端点函数，当场断言 tasks 行存在。
"""
import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.domain.schemas import RecordModelCallRequest
from app.models.tables import Base, ComplianceEvent, ModelService, Task
from app.routers import model_workbench


class ModelCallTaskSyncTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine, expire_on_commit=False)
        with self.Session() as db:
            db.add(ModelService(
                id="SVC-T-1", name="测试服务", model_id="7", version="1.0.0",
                service_type="local", status="running", endpoint="http://127.0.0.1:9",
            ))
            db.commit()
        self.original = model_workbench.SessionLocal
        model_workbench.SessionLocal = self.Session

    def tearDown(self):
        model_workbench.SessionLocal = self.original

    def _record(self, risk="high"):
        result = model_workbench.record_model_call(RecordModelCallRequest(
            model_id=7, service_id="SVC-T-1", version="1.0.0",
            prompt="测试调用内容", original_output="原始输出", governed_output="治理输出",
            reason="命中个人信息", risk_level=risk, elapsed_ms=1234,
        ))
        return result["data"]["id"]

    def test_record_creates_task_row_immediately(self):
        call_id = self._record()
        with self.Session() as db:
            task = db.get(Task, call_id)
            self.assertIsNotNone(task, "调用留痕必须当场补挂 tasks 流水，不能等重启")
            self.assertEqual(task.capability_code, "model_risk_governance")
            self.assertEqual(task.status, "succeeded")

    def test_high_risk_record_also_writes_event(self):
        call_id = self._record(risk="high")
        with self.Session() as db:
            event = db.scalar(select(ComplianceEvent).where(
                ComplianceEvent.subject_type == "model_call",
                ComplianceEvent.subject_id == call_id))
            self.assertIsNotNone(event)
            self.assertEqual(event.level, "high")

    def test_low_risk_record_creates_task_without_event(self):
        call_id = self._record(risk="low")
        with self.Session() as db:
            self.assertIsNotNone(db.get(Task, call_id))
            event = db.scalar(select(ComplianceEvent).where(
                ComplianceEvent.subject_id == call_id))
            self.assertIsNone(event)


if __name__ == "__main__":
    unittest.main()
