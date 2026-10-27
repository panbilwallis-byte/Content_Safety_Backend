"""版本样本回查：按记录 ID 从版本清单直接取原始样本（前端清洗对比校验契约）。

回归背景：/datasets/{id}/versions/{vid}/samples 只认异常/价值分析快照，
数据处理详情拿对比行 ID 回查时永远为空，前端判定"样本与版本不一致"后
整份详情报"任务详情加载失败"。修复后该接口先按 dataset_versions 清单直查。
"""
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from app.models.tables import Base, Dataset, DatasetRecord
from app.routers import data_governance as dg
from app.services.dataset_version_service import register_version


class VersionSamplesTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        db = self.Session()
        dataset = Dataset(name="回查测试集", version="v1.0.0", source_type="business")
        db.add(dataset)
        db.flush()
        first = DatasetRecord(dataset_id=dataset.id, task_id="T-1", content_hash="h1",
                              payload={"text": "第一段原文", "language": "zh"})
        second = DatasetRecord(dataset_id=dataset.id, task_id="T-1", content_hash="h2",
                               payload={"text": "第二段原文", "language": "zh"})
        db.add_all([first, second])
        db.flush()
        version = register_version(db, dataset, "v1.0.0")
        db.commit()
        self.dataset_id = dataset.id
        self.version_id = version.version_id
        self.ids = [first.id, second.id]
        self.db = db
        self.original = dg.SessionLocal
        dg.SessionLocal = self.Session

    def tearDown(self):
        dg.SessionLocal = self.original

    def _request(self, ids):
        query = "&".join("ids%5B%5D={}".format(i) for i in ids).encode()
        return Request({"type": "http", "method": "GET", "path": "/samples",
                        "query_string": query, "headers": []})

    def test_lookup_records_by_version_id(self):
        data = dg.governance_resource_samples(
            self.dataset_id, self.version_id, self._request(self.ids))["data"]
        self.assertEqual([item["id"] for item in data], [str(i) for i in self.ids])
        self.assertEqual(data[0]["text"], "第一段原文")
        self.assertEqual(data[0]["dataset_id"], self.dataset_id)
        self.assertEqual(data[0]["version_id"], self.version_id)

    def test_lookup_accepts_label_too(self):
        data = dg.governance_resource_samples(
            self.dataset_id, "v1.0.0", self._request(self.ids[:1]))["data"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], str(self.ids[0]))

    def test_unknown_id_returns_empty_without_error(self):
        data = dg.governance_resource_samples(
            self.dataset_id, self.version_id, self._request([999999]))["data"]
        self.assertEqual(data, [])


if __name__ == "__main__":
    unittest.main()
