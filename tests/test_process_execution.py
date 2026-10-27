"""数据清洗真执行（#13-1.4）：去重筛选、规范化派生、输出版本登记。"""
import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.tables import (
    Base,
    Dataset,
    DatasetRecord,
    DatasetVersion,
    DatasetVersionItem,
    Task,
)
from app.services.dataset_version_service import register_version
from app.services.process_execution import apply_process_rules


class ProcessExecutionTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.dataset = Dataset(name="清洗测试集", version="v1.0.0", source_type="business")
        self.db.add(self.dataset)
        self.db.flush()
        rows = [
            ("ＡＢＣ　全角数据", "hash-full"),          # 需规范化
            ("普通数据", "hash-plain"),                  # 原件保留
            ("普通数据", "hash-plain"),                  # 与上重复 → 去重丢弃
            ("干净文本", None),                          # payload 缺 language/topic_label
            ("  首尾空白  ", "hash-space"),              # 需规范化
        ]
        self.ids = []
        for text, digest in rows:
            payload = {"text": text}
            if text != "干净文本":
                payload.update({"language": "zh", "topic_label": "综合"})
            record = DatasetRecord(dataset_id=self.dataset.id, task_id="TR-ING",
                                   content_hash=digest, payload=payload)
            self.db.add(record)
            self.db.flush()
            self.ids.append(record.id)
        self.source_version = register_version(self.db, self.dataset, "v1.0.0")
        self.db.commit()

    def _task(self, rules):
        task = Task(
            task_id="tsk_process_test", name="清洗任务", capability_code="data_process",
            status="running", input_data={
                "dataset_id": self.dataset.id, "version_id": self.source_version.version_id,
                "rules": rules, "output_version_name": "v1.0.1"},
            config={}, result={"pending_output_version": "v1.0.1"},
        )
        self.db.add(task)
        self.db.flush()
        return task

    def test_dedup_normalize_complete_and_version_registration(self):
        outcome = apply_process_rules(self.db, self._task(
            ["deduplicate", "normalize_encoding", "complete_fields"]))
        self.db.commit()
        self.assertEqual(outcome["input_count"], 5)
        self.assertEqual(outcome["deduplicated_count"], 1)
        self.assertEqual(outcome["derived_count"], 3)  # 全角、缺字段、空白各派生一行
        self.assertEqual(outcome["output_sample_count"], 4)
        version = self.db.scalar(select(DatasetVersion).where(
            DatasetVersion.label == "v1.0.1"))
        self.assertEqual(version.sample_count, 4)
        self.assertEqual(version.source_version_id, self.source_version.version_id)
        self.assertEqual(version.producing_task_id, "tsk_process_test")
        item_ids = set(self.db.scalars(select(DatasetVersionItem.record_id).where(
            DatasetVersionItem.version_id == version.version_id)).all())
        self.assertIn(self.ids[1], item_ids)  # 未改动行直接引用原件
        derived = self.db.scalars(select(DatasetRecord).where(
            DatasetRecord.source_record_id.isnot(None)).order_by(DatasetRecord.id)).all()
        self.assertEqual({d.source_record_id for d in derived},
                         {self.ids[0], self.ids[3], self.ids[4]})
        self.assertEqual(derived[0].payload["text"], "ABC 全角数据")
        self.assertEqual(derived[1].payload["language"], "zh")
        self.assertEqual(derived[2].payload["text"], "首尾空白")

    def test_comparisons_match_frontend_contract(self):
        outcome = apply_process_rules(self.db, self._task(["normalize_text"]))
        first = outcome["comparisons"][0]
        self.assertEqual({"id", "original", "processed", "actions", "fields"} - set(first), set())
        self.assertTrue(all(field["before"] != field["after"] for field in first["fields"]))
        # 前端按该 ID 回查版本清单并逐字比对原文：必须是真实记录号与完整文本。
        self.assertEqual(first["id"], str(self.ids[0]))
        self.assertEqual(first["original"], "ＡＢＣ　全角数据")

    def test_no_rules_keeps_rows_untouched(self):
        outcome = apply_process_rules(self.db, self._task([]))
        self.db.commit()
        self.assertEqual(outcome["derived_count"], 0)
        self.assertEqual(outcome["deduplicated_count"], 0)
        self.assertEqual(outcome["output_sample_count"], 5)
        self.assertEqual(len(self.db.scalars(select(DatasetRecord)).all()), 5)  # 无规则不派生


if __name__ == "__main__":
    unittest.main()
