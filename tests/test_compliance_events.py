"""训练中合规事件（GN-018）：写入、汇总口径、任务上下文与分页契约。"""
import unittest
from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.time import now_shanghai
from app.models.tables import Base, Dataset, Model, TrainingTask
from app.routers.compliance_events import compliance_events_overview, list_compliance_events
from app.routers.training_tasks import advance_training_task
from app.services.compliance_event_service import add_event


class ComplianceEventsTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()

    def _make_task(self):
        now = now_shanghai()
        task = TrainingTask(
            id="TR-EVT-1", name="事件测试", status="running", progress=0,
            model_id="1", base_version="v1.0.0", dataset_id="1",
            dataset_version="v1.0.0", epochs=2, epoch=0, learning_rate=0.001,
            batch_size=8, target_version="v1.0.1", method="低秩适配微调",
            loss_history=[], validation_loss_history=[], checkpoints=[],
            created_at=now - timedelta(seconds=200), updated_at=now,
        )
        self.db.add(task)
        self.db.commit()
        return task

    def test_overview_summary_follows_frontend_contract(self):
        add_event(self.db, subject_type="model_call", subject_id="c1",
                  event_type="模型输出异常告警", level="high")
        add_event(self.db, subject_type="model_call", subject_id="c2",
                  event_type="模型输出异常告警", level="low")
        handled = add_event(self.db, subject_type="model_call", subject_id="c3",
                            event_type="模型输出异常告警", level="high")
        handled.status = "handled"
        self.db.commit()
        data = compliance_events_overview(training_task_id=None, db=self.db)["data"]
        self.assertEqual(data["summary"], {"total": 3, "high_risk": 1, "handled": 2})
        self.assertEqual(len(data["recent_events"]), 3)

    def test_training_advance_writes_lifecycle_events(self):
        task = self._make_task()
        self.assertTrue(advance_training_task(self.db, task, now_shanghai()))
        self.db.commit()
        data = compliance_events_overview(training_task_id="TR-EVT-1", db=self.db)["data"]
        types = {row["event_type"] for row in data["recent_events"]}
        self.assertIn("训练轮次完成", types)
        self.assertIn("训练任务完成", types)

    def test_events_scoped_by_training_task(self):
        for index in range(3):
            add_event(self.db, subject_type="training_task", subject_id="TR-A",
                      training_task_id="TR-A", event_type=f"事件{index}")
        add_event(self.db, subject_type="training_task", subject_id="TR-B",
                  training_task_id="TR-B", event_type="别的任务")
        self.db.commit()
        page = list_compliance_events(training_task_id="TR-A", page=2, page_size=2,
                                      db=self.db)["data"]
        self.assertEqual(page["total"], 3)
        self.assertEqual(page["total_pages"], 2)
        self.assertEqual(len(page["items"]), 1)
        overview = compliance_events_overview(training_task_id="TR-A", db=self.db)["data"]
        self.assertEqual(overview["summary"]["total"], 3)

    def test_dataset_marked_test_only_raises_high_risk_event_on_training(self):
        """反例库等 usage=test 数据集被用于训练时必须留下高风险事件。"""
        self.db.add(Model(id=1, name="测试模型", version="v1.0.0"))
        self.db.add(Dataset(id=1, name="反例库", version="v1.0.0",
                            metadata_json={"usage": "test"}))
        self.db.commit()
        import app.routers.training_tasks as training_module
        from app.domain.schemas import CreateTrainingTaskRequest
        from app.models.tables import ComplianceEvent

        # create_training_task 内部自开 SessionLocal：临时替换为测试会话，避免写真实库。
        original = training_module.SessionLocal
        training_module.SessionLocal = sessionmaker(
            bind=engine_of(self.db), autoflush=False, autocommit=False)
        try:
            training_module.create_training_task(CreateTrainingTaskRequest(
                name="越权训练", model_id="1", base_version="v1.0.0", dataset_id="1",
                dataset_version="v1.0.0", epochs=1, learning_rate=0.001, batch_size=8,
                target_version="v9.9.9", method="低秩适配微调",
            ))
        finally:
            training_module.SessionLocal = original
        events = self.db.query(ComplianceEvent).filter_by(event_type="训练数据越权访问尝试").all()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].level, "high")


def engine_of(session):
    return session.get_bind()


if __name__ == "__main__":
    unittest.main()
