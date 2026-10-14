import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.core.time import now_shanghai
from app.models.tables import Task
from app.routers import kpis


class GovernanceKpiConsistencyTest(unittest.TestCase):
    def test_dashboard_and_process_cards_share_database_count(self):
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        now = now_shanghai()
        with Session() as db:
            for index, capability in enumerate(("data_process", "data_process", "risk_detect")):
                db.add(Task(
                    task_id=f"task-{index}", name="测试任务", capability_code=capability,
                    status="succeeded", source_name="测试", dataset_name="测试数据集",
                    storage_gb=0, progress=100, success_count=10, duplicate_count=0,
                    anomaly_count=0, input_data={}, config={}, result={}, created_at=now,
                    finished_at=now,
                ))
            db.commit()
        with patch.object(kpis, "SessionLocal", Session):
            dashboard = kpis.get_kpis("dashboard")["data"]
            process = kpis.get_kpis("governance-process")["data"]
        self.assertEqual(
            next(item["value"] for item in dashboard if item["id"] == "dashboard-5"),
            next(item["value"] for item in process if item["id"] == "process-0"),
        )


if __name__ == "__main__":
    unittest.main()
