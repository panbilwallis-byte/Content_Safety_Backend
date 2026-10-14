import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.tables import Dataset
from app.routers import data_governance as risk


class RiskOverviewPersistenceTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine, expire_on_commit=False)
        with self.Session() as db:
            db.add(Dataset(
                name="社交媒体中文语料库", category="测试", source_type="business",
                version="dsv_000003", status="ready", description="", metadata_json={},
            ))
            db.commit()
        risk.RISK_RESULTS.clear()
        risk.RISK_TASKS.clear()
        risk.RISK_RUN_COUNTS.clear()
        risk.RISK_TOTALS.update(valid_count=0, risk_count=0, high_count=0, pending_count=0)

    def test_completed_risk_task_restores_overview_after_memory_reset(self):
        with patch.object(risk, "SessionLocal", self.Session):
            created = risk.create_risk_task({"input": {
                "dataset_id": 1, "version_id": "dsv_000003", "language": "all", "scheme_id": "risk-v1",
            }})["data"]
            completed = risk.get_risk_task(created["id"])["data"]
            before_restart = risk.risk_overview("governance-risk")["data"]["cards"][0]["value"]
            risk.RISK_RESULTS.clear()
            risk.RISK_TOTALS.update(valid_count=0, risk_count=0, high_count=0, pending_count=0)
            after_restart = risk.risk_overview("governance-risk")["data"]
        self.assertEqual(after_restart["cards"][0]["value"], before_restart)
        self.assertEqual(after_restart["records"][0]["result_id"], completed["result_id"])


if __name__ == "__main__":
    unittest.main()
