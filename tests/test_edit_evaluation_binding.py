"""编辑产物的评估绑定自动登记：只用服务端令牌、幂等、失败不推翻任务。"""
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.services.model_comparison as comparison
from app.models.tables import Base, ComplianceEvent, Model, ModelVersion, Task


class RecordingLlm:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def __call__(self, path, token, body=None):
        self.calls.append({"path": path, "token": token, "body": body})
        if self.error is not None:
            raise self.error
        return self.result


class EditEvaluationBindingTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        with self.Session() as db:
            db.add(Model(id=10, name="风险知识编辑测试模型", version="v1.0.0"))
            db.add(ModelVersion(model_id=10, version="v1.0.0", description="基线"))
            db.add(ModelVersion(model_id=10, version="v1.0.0-edit", task_id="tsk_edit_1",
                                description="编辑产物"))
            db.add(Task(task_id="tsk_edit_1", name="风险知识编辑", capability_code="knowledge_edit",
                        status="succeeded", progress=100,
                        input_data={"model_id": "10", "target_knowledge": "未经授权的个人信息披露"},
                        config={}, result={"before_version": "v1.0.0", "after_version": "v1.0.0-edit"}))
            db.commit()
        self.originals = (comparison.LLM_ACCESS_TOKEN, comparison.llm)
        comparison.LLM_ACCESS_TOKEN = "svc-token"

    def tearDown(self):
        comparison.LLM_ACCESS_TOKEN, comparison.llm = self.originals

    def _task(self):
        with self.Session() as db:
            return db.scalar(select(Task).where(Task.task_id == "tsk_edit_1"))

    def _events(self):
        with self.Session() as db:
            return list(db.scalars(select(ComplianceEvent)).all())

    def _ensure(self):
        with self.Session() as db:
            task = db.scalar(select(Task).where(Task.task_id == "tsk_edit_1"))
            result = dict(task.result or {})
            return comparison.ensure_edit_binding(
                db, task_id="tsk_edit_1", model_id=10, version="v1.0.0-edit",
                result=result, trace_id="trace-edit-1")

    def test_success_writes_binding_into_task_result(self):
        fake = RecordingLlm(result={"mode": "simulated_policy",
                                    "policy_version": "privacy-demo-template-v1:tsk_edit_1",
                                    "model_id": "c55ba507"})
        with patch.object(comparison, "llm", fake):
            outcome = self._ensure()
        self.assertEqual(outcome["status"], "bound")
        self.assertEqual(fake.calls[0]["path"], "/model-evaluation/prepare-edit")
        self.assertEqual(fake.calls[0]["body"], {"platform_model_id": 10, "version": "v1.0.0-edit"})
        self.assertEqual(self._task().result["evaluation_binding"]["status"], "bound")
        self.assertEqual(self._events(), [])

    def test_only_the_server_token_is_sent_never_the_platform_login_token(self):
        fake = RecordingLlm(result={"mode": "simulated_policy", "policy_version": "v1", "model_id": "m"})
        with patch.object(comparison, "llm", fake):
            self._ensure()
        self.assertEqual(fake.calls[0]["token"], "Bearer svc-token")
        comparison.LLM_ACCESS_TOKEN = "Bearer already-prefixed"
        with patch.object(comparison, "llm", fake):
            self._ensure()
        self.assertEqual(fake.calls[1]["token"], "Bearer already-prefixed")

    def test_missing_server_token_is_reported_not_raised(self):
        comparison.LLM_ACCESS_TOKEN = ""
        fake = RecordingLlm(result={})
        with patch.object(comparison, "llm", fake):
            outcome = self._ensure()
        self.assertEqual(outcome["status"], "pending")
        self.assertIn("LLM_ACCESS_TOKEN", outcome["message"])
        self.assertEqual(fake.calls, [])  # 令牌没配就不该往 8001 发请求
        self.assertEqual(len(self._events()), 1)

    def test_baseline_without_template_keeps_task_succeeded_and_records_event(self):
        fake = RecordingLlm(error=HTTPException(
            409, "基线 v1.0.0 尚未配置模拟编辑模板，请管理员在 8001 配置一次"))
        with patch.object(comparison, "llm", fake):
            outcome = self._ensure()
        self.assertEqual(outcome["status"], "pending")
        self.assertIn("模拟编辑模板", outcome["message"])
        task = self._task()
        self.assertEqual(task.status, "succeeded")  # 产物是真的，不能因绑定失败判任务失败
        self.assertEqual(task.result["evaluation_binding"]["status"], "pending")
        events = self._events()
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0].event_type, events[0].level, events[0].subject_type),
                         ("评估绑定未就绪", "medium", "model_version"))
        self.assertIn("v1.0.0-edit", events[0].detail)

    def test_service_unavailable_is_also_non_fatal(self):
        fake = RecordingLlm(error=HTTPException(503, "8001 模型服务不可用或响应无效"))
        with patch.object(comparison, "llm", fake):
            outcome = self._ensure()
        self.assertEqual(outcome["status"], "pending")
        self.assertIn("8001", outcome["message"])
        self.assertEqual(len(self._events()), 1)

    def test_unexpected_error_still_leaves_the_succeeded_task_alone(self):
        fake = RecordingLlm(error=AttributeError("'NoneType' object has no attribute 'get'"))
        with patch.object(comparison, "llm", fake):
            outcome = self._ensure()
        self.assertEqual(outcome["status"], "pending")
        self.assertIn("NoneType", outcome["message"])
        self.assertEqual(self._task().status, "succeeded")
        self.assertEqual(len(self._events()), 1)

    def test_result_written_survives_the_commit(self):
        with patch.object(comparison, "llm", RecordingLlm(
                result={"mode": "simulated_policy", "policy_version": "v1", "model_id": "m"})):
            self._ensure()
        with self.Session() as db:  # 新会话读得到，说明已提交
            task = db.scalar(select(Task).where(Task.task_id == "tsk_edit_1"))
            self.assertEqual(task.result["evaluation_binding"]["status"], "bound")


if __name__ == "__main__":
    unittest.main()
