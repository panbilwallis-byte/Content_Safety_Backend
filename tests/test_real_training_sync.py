"""真实训练账本：建单代理入账与进度同步的事件/版本行为（不触网部分）。"""
import unittest
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.time import now_shanghai
from app.domain.schemas import SyncTrainingTaskRequest
from app.models.tables import Base, ComplianceEvent, Model, ModelVersion, TrainingTask
from app.routers.training_tasks import sync_real_training_task


class RealTrainingSyncTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        import app.routers.training_tasks as module
        self.original = module.SessionLocal
        module.SessionLocal = self.Session

    def tearDown(self):
        import app.routers.training_tasks as module
        module.SessionLocal = self.original

    def _task(self, task_id=None):
        now = now_shanghai()
        task = TrainingTask(
            id=task_id or str(uuid4()), name="真实训练", status="pending", progress=0,
            model_id="1", base_version="v1.0.0", dataset_id="1", dataset_version="v1.0.0",
            epochs=2, epoch=0, learning_rate=0.05, batch_size=64, target_version="v1.0.1",
            method="低秩适配微调", loss_history=[], validation_loss_history=[],
            checkpoints=[], created_at=now, updated_at=now,
        )
        with self.Session() as db:
            db.add(Model(id=1, name="测试模型", version="v1.0.0"))
            db.add(task)
            db.commit()
            return task.id

    def _sync(self, task_id, **kwargs):
        return sync_real_training_task(task_id, SyncTrainingTaskRequest(**kwargs))

    def test_sync_records_epoch_and_checkpoint_events(self):
        task_id = self._task()
        self._sync(task_id, status="running", epoch=1, progress=50,
                   loss_history=[0.62], validation_loss_history=[0.58],
                   checkpoints=[{"name": "checkpoint-01", "epoch": 1, "loss": 0.58}])
        with self.Session() as db:
            types = set(db.scalars(
                select(ComplianceEvent.event_type).where(
                    ComplianceEvent.training_task_id == task_id)).all())
        self.assertEqual(types, {"训练轮次完成", "检查点保存"})

    def test_success_sync_registers_version_and_completion_event(self):
        task_id = self._task()
        self._sync(task_id, status="succeeded", epoch=2, progress=100,
                   loss_history=[0.62, 0.21], validation_loss_history=[0.58, 0.19],
                   checkpoints=[{"name": "checkpoint-02", "epoch": 2, "loss": 0.19}])
        with self.Session() as db:
            version = db.scalar(select(ModelVersion).where(ModelVersion.task_id == task_id))
            event = db.scalar(select(ComplianceEvent).where(
                ComplianceEvent.event_type == "训练任务完成"))
        self.assertIsNotNone(version)
        self.assertEqual(version.version, "v1.0.1")
        self.assertIsNotNone(event)

    def test_failure_sync_records_high_risk_event(self):
        task_id = self._task()
        self._sync(task_id, status="failed", epoch=1, progress=40,
                   loss_history=[0.9], validation_loss_history=[],
                   checkpoints=[], error_message="上游执行异常退出")
        with self.Session() as db:
            event = db.scalar(select(ComplianceEvent).where(
                ComplianceEvent.event_type == "训练任务失败"))
        self.assertIsNotNone(event)
        self.assertEqual(event.level, "high")

    def test_legacy_short_id_task_sync_is_ignored(self):
        task_id = self._task(task_id="TR-legacy01")
        result = self._sync(task_id, status="succeeded", epoch=2, progress=100,
                            loss_history=[], validation_loss_history=[], checkpoints=[])
        self.assertIn("忽略同步", result["message"])
        with self.Session() as db:
            task = db.get(TrainingTask, task_id)
            self.assertEqual(task.status, "pending")


if __name__ == "__main__":
    unittest.main()
