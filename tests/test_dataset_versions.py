"""数据集版本清单服务（#13）：登记、幂等、双标识解析与清单指纹。"""
import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.tables import Base, Dataset, DatasetRecord, DatasetVersion, DatasetVersionItem
from app.services.dataset_version_service import (
    manifest_hash,
    refresh_raw_version,
    register_version,
    resolve_version,
)


class DatasetVersionServiceTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.dataset = Dataset(name="测试集", version="v1.0.0", source_type="business")
        self.db.add(self.dataset)
        self.db.flush()
        for index in range(1, 4):
            self.db.add(DatasetRecord(
                dataset_id=self.dataset.id, task_id="TR-T",
                content_hash="h{}".format(index), payload={"text": "样本{}".format(index)}))
        self.db.commit()

    def _record_ids(self, version_id):
        return [item.record_id for item in self.db.scalars(
            select(DatasetVersionItem).where(DatasetVersionItem.version_id == version_id)
            .order_by(DatasetVersionItem.id)).all()]

    def test_register_full_snapshot_is_idempotent(self):
        first = register_version(self.db, self.dataset, "v1.0.0")
        self.db.commit()
        self.assertEqual(first.version_id, "dsv_000001")
        self.assertEqual(first.sample_count, 3)
        self.assertEqual(self._record_ids(first.version_id), sorted(self._record_ids(first.version_id)))
        expected = manifest_hash([(rid, "h{}".format(rid)) for rid in self._record_ids(first.version_id)])
        self.assertEqual(first.manifest_hash, expected)
        again = register_version(self.db, self.dataset, "v1.0.0")
        self.db.commit()
        self.assertEqual(again.version_id, first.version_id)
        self.assertEqual(len(self.db.scalars(select(DatasetVersion)).all()), 1)
        self.assertEqual(len(self._record_ids(first.version_id)), 3)

    def test_resolve_accepts_dsv_id_and_label(self):
        version = register_version(self.db, self.dataset, "v1.0.0")
        self.db.commit()
        self.assertEqual(resolve_version(self.db, self.dataset.id, "dsv_000001").label, "v1.0.0")
        self.assertEqual(resolve_version(self.db, self.dataset.id, "v1.0.0").version_id,
                         version.version_id)
        self.assertIsNone(resolve_version(self.db, self.dataset.id, "v9.9.9"))
        self.assertIsNone(resolve_version(self.db, 999, "v1.0.0"))

    def test_derived_version_subset_and_lineage(self):
        parent = register_version(self.db, self.dataset, "v1.0.0")
        child = register_version(self.db, self.dataset, "v1.0.1", [1, 3],
                                 source_version_id=parent.version_id,
                                 producing_task_id="TR-CLEAN",
                                 change_summary="去重 3→2")
        self.db.commit()
        self.assertEqual(child.sample_count, 2)
        self.assertEqual(self._record_ids(child.version_id), [1, 3])
        self.assertEqual(child.source_version_id, parent.version_id)
        self.assertNotEqual(child.manifest_hash, parent.manifest_hash)

    def test_foreign_record_in_manifest_raises(self):
        foreign = DatasetRecord(dataset_id=self.dataset.id + 100, task_id="TR-X",
                                content_hash="other", payload={})
        self.db.add(foreign)
        self.db.commit()
        with self.assertRaises(ValueError):
            register_version(self.db, self.dataset, "v1.0.1", [1, 2, foreign.id])

    def test_refresh_raw_version_tracks_new_raw_rows(self):
        first = refresh_raw_version(self.db, self.dataset, "TR-ING-1")
        self.db.commit()
        self.assertEqual(first.sample_count, 3)
        first_hash = first.manifest_hash
        self.db.add(DatasetRecord(dataset_id=self.dataset.id, task_id="TR-ING-2",
                                  content_hash="h4", payload={"text": "新样本"}))
        self.db.commit()
        second = refresh_raw_version(self.db, self.dataset, "TR-ING-2")
        self.db.commit()
        self.assertEqual(second.version_id, first.version_id)  # 同一原始版本滚动，不新建
        self.assertEqual(second.sample_count, 4)
        self.assertNotEqual(second.manifest_hash, first_hash)

    def test_refresh_ignores_derived_rows(self):
        derived = DatasetRecord(dataset_id=self.dataset.id, task_id="TR-CLEAN",
                                content_hash="hd", source_record_id=1, payload={"text": "派生"})
        self.db.add(derived)
        self.db.commit()
        version = refresh_raw_version(self.db, self.dataset, "TR-ING-1")
        self.db.commit()
        self.assertEqual(version.sample_count, 3)  # 派生行不进原始快照

    def test_version_id_survives_deleted_middle_row(self):
        for label in ("v1.0.0", "v1.1.0", "v1.2.0"):
            register_version(self.db, self.dataset, label)
        self.db.commit()
        self.db.delete(self.db.get(DatasetVersion, "dsv_000001"))
        self.db.commit()
        # 表里只剩两行；若按“行数+1”发号会重发已存在的 dsv_000002 并撞主键。
        newest = register_version(self.db, self.dataset, "v1.3.0")
        self.db.commit()
        self.assertEqual(newest.version_id, "dsv_000004")


if __name__ == "__main__":
    unittest.main()
