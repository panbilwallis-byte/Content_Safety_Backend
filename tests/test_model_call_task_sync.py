"""真实调用留痕补挂 tasks 行，并与全链路五要素证据联动。"""

import unittest
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.tables import Base, ModelCall, ModelService, Task
from app.routers.model_workbench import sync_model_call_tasks
from app.services.compliance_service import task_evidence


class ModelCallTaskSyncTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(
            engine, tables=[ModelService.__table__, ModelCall.__table__, Task.__table__]
        )
        self.db = sessionmaker(bind=engine)()
        self.db.add(ModelService(
            id="SVC-1", name="DeepSeek智能体服务", model_id="7", version="4.1.3",
            service_type="external", status="running",
            endpoint="https://api.deepseek.com/v1"))
        self.db.add(ModelCall(
            id="CALL-TEST01", model_id="7", service_id="SVC-1", version="4.1.3",
            prompt="请帮我整理这份联系人记录，用于公开发布。",
            original_output="联系人：许狠狠，手机号：13445567897",
            governed_output="联系人：许**，手机号：134****7897",
            reason="公开场景需对姓名和手机号脱敏", risk_level="high",
            reconstruction=None, elapsed_ms=1580, status="succeeded",
            trace_id="trace-test", created_at=datetime(2026, 10, 26, 1, 4, 38)))
        self.db.commit()

    def test_sync_creates_task_row(self):
        self.assertEqual(sync_model_call_tasks(self.db), 1)
        self.db.commit()
        task = self.db.get(Task, "CALL-TEST01")
        self.assertIsNotNone(task)
        self.assertEqual(task.capability_code, "model_risk_governance")
        self.assertEqual(task.status, "succeeded")
        self.assertEqual(task.model_version, "4.1.3")
        self.assertEqual(task.input_data["model_call_id"], "CALL-TEST01")

    def test_sync_is_idempotent(self):
        self.assertEqual(sync_model_call_tasks(self.db), 1)
        self.db.commit()
        self.assertEqual(sync_model_call_tasks(self.db), 0)

    def test_five_elements_all_verified(self):
        sync_model_call_tasks(self.db)
        self.db.commit()
        task = self.db.get(Task, "CALL-TEST01")
        evidence = task_evidence(task, "evidence-task-CALL-TEST01")
        states = {item["key"]: item["state"] for item in evidence["redacted_fields"]}
        self.assertEqual(set(states), {"input", "time", "interface", "version", "output"})
        self.assertTrue(all(state == "verified" for state in states.values()), states)
        self.assertEqual(evidence["integrity_state"], "verified")


if __name__ == "__main__":
    unittest.main()
