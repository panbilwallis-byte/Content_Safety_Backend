"""谱系边台账（#13-1.6）：四个执行点写边、幂等去重、启动补登可重建。"""
import unittest

from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.core.database as database_module
from app.models.tables import (
    Base,
    ComplianceLineageEdge,
    Dataset,
    DatasetRecord,
    DatasetVersion,
    Model,
    ModelCall,
    ModelVersion,
    Task,
    TrainingTask,
)
from app.routers.training_tasks import register_trained_version
from app.services.dataset_version_service import create_ingest_version
from app.services.lineage_service import (
    RELATION_DERIVED_FROM,
    RELATION_INGEST,
    RELATION_MODEL_CALL,
    RELATION_PROCESS_INPUT,
    RELATION_PROCESS_OUTPUT,
    RELATION_TRAINING_INPUT,
    RELATION_TRAINING_OUTPUT,
    edge_id,
    ensure_lineage_edges_backfill,
    ledger_summary,
    record_call_edge,
    record_edge,
    record_ingest_edge,
)
from app.services.process_execution import apply_process_rules


def triples(db):
    """台账里的 (起点, 关系, 终点) 三元组集合。"""
    return {(row.from_type, row.from_id, (row.payload or {}).get("relation"),
             row.to_type, row.to_id) for row in db.scalars(select(ComplianceLineageEdge)).all()}


def relation_names(db):
    return {(row[0] or {}).get("relation") for row in
            db.execute(select(ComplianceLineageEdge.payload)).all()}


class LineageEdgeTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.dataset = Dataset(name="谱系测试集", version="v1.0.0", source_type="business")
        self.db.add(self.dataset)
        self.db.flush()
        for text in ("ＡＢＣ　数据", "普通数据"):
            self.db.add(DatasetRecord(dataset_id=self.dataset.id, task_id="TR-ING-1",
                                      content_hash=text,
                                      payload={"text": text, "language": "zh", "topic_label": "综合"}))
        self.db.flush()
        self.raw_version = create_ingest_version(self.db, self.dataset, "TR-ING-1")
        self.db.add(Task(task_id="TR-ING-1", name="接入任务", capability_code="data_ingest",
                         status="succeeded", input_data={"dataset_id": self.dataset.id},
                         result={"dataset_id": self.dataset.id}, config={}))
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def count_edges(self):
        return self.db.scalar(select(func.count()).select_from(ComplianceLineageEdge))

    # ── 边主键与幂等 ──────────────────────────────────────────────────────
    def test_edge_id_is_stable_and_double_write_keeps_one_row(self):
        first = record_edge(self.db, "task", "TR-1", "dataset", self.dataset.id, RELATION_INGEST)
        again = record_edge(self.db, "task", "TR-1", "dataset", self.dataset.id, RELATION_INGEST)
        self.assertEqual(first.id, again.id)
        self.assertEqual(first.id, edge_id("task", "TR-1", "dataset", self.dataset.id, RELATION_INGEST))
        self.assertEqual(self.count_edges(), 1)
        self.assertIsNone(record_edge(self.db, "task", None, "dataset", 1, RELATION_INGEST))

    # ── 接入钩子 ─────────────────────────────────────────────────────────
    def test_ingest_edge_records_dataset_and_raw_manifest(self):
        record_ingest_edge(self.db, task_id="TR-ING-1", dataset=self.dataset)
        self.db.commit()
        self.assertIn(("task", "TR-ING-1", RELATION_INGEST, "dataset", str(self.dataset.id)),
                      triples(self.db))
        self.assertIn(("task", "TR-ING-1", RELATION_PROCESS_OUTPUT,
                       "dataset_version", self.raw_version.version_id), triples(self.db))

    # ── 清洗钩子 ─────────────────────────────────────────────────────────
    def test_process_hook_writes_input_output_and_derivation_edges(self):
        task = Task(
            task_id="tsk_process_lineage", name="清洗任务", capability_code="data_process",
            status="running",
            input_data={"dataset_id": self.dataset.id, "version_id": self.raw_version.version_id,
                        "rules": ["normalize_text", "complete_fields"]},
            config={}, result={"pending_output_version": "v1.0.1"},
        )
        self.db.add(task)
        self.db.flush()
        apply_process_rules(self.db, task)
        self.db.commit()
        output = self.db.scalar(select(DatasetVersion).where(DatasetVersion.label == "v1.0.1"))
        self.assertEqual(
            {("dataset_version", self.raw_version.version_id, RELATION_PROCESS_INPUT,
              "task", task.task_id),
             ("dataset_version", self.raw_version.version_id, RELATION_DERIVED_FROM,
              "dataset_version", output.version_id),
             ("task", task.task_id, RELATION_PROCESS_OUTPUT,
              "dataset_version", output.version_id)},
            triples(self.db))

    # ── 训练钩子 ─────────────────────────────────────────────────────────
    def _training_task(self, dataset_version):
        self.model = Model(name="谱系模型", version="v1.0.0")
        self.db.add(self.model)
        self.db.flush()
        task = TrainingTask(
            id="TR-LINEAGE", name="训练任务", status="succeeded", model_id=str(self.model.id),
            base_version="v1.0.0", dataset_id=str(self.dataset.id),
            dataset_version=dataset_version, epochs=3, learning_rate=0.001, batch_size=8,
            target_version="v1.0.1",
        )
        self.db.add(task)
        self.db.flush()
        return task

    def _call(self, call_id, version):
        call = ModelCall(
            id=call_id, model_id=str(self.model.id), service_id="SVC-1", version=version,
            prompt="问", original_output="答", governed_output="治理后", reason="依据",
            risk_level="low", reconstruction=None, elapsed_ms=12, status="succeeded",
        )
        self.db.add(call)
        self.db.flush()
        return call

    def test_training_hook_links_manifest_to_task_to_model_version(self):
        task = self._training_task(self.raw_version.version_id)
        register_trained_version(self.db, task)
        self.db.commit()
        version = self.db.scalar(select(ModelVersion).where(ModelVersion.version == "v1.0.1"))
        self.assertIn(("dataset_version", self.raw_version.version_id, RELATION_TRAINING_INPUT,
                       "training_task", task.id), triples(self.db))
        self.assertIn(("training_task", task.id, RELATION_TRAINING_OUTPUT,
                       "model_version", str(version.id)), triples(self.db))

    def test_unregistered_dataset_version_yields_no_fake_input_edge(self):
        task = self._training_task("v9.9.9")
        register_trained_version(self.db, task)
        self.db.commit()
        self.assertNotIn(RELATION_TRAINING_INPUT, relation_names(self.db))
        self.assertIn(RELATION_TRAINING_OUTPUT, relation_names(self.db))

    def test_call_edge_only_for_registered_model_version(self):
        self.model = Model(name="谱系模型", version="v1.0.0")
        self.db.add(self.model)
        self.db.flush()
        self.db.add(ModelVersion(model_id=self.model.id, version="v1.0.0", description="登记版本"))
        self.db.flush()
        record_call_edge(self.db, self._call("CALL-OK", "v1.0.0"))
        record_call_edge(self.db, self._call("CALL-UNKNOWN", "v8.8.8"))
        self.db.commit()
        version_row = self.db.scalar(select(ModelVersion).where(ModelVersion.version == "v1.0.0"))
        self.assertIn(("model_version", str(version_row.id), RELATION_MODEL_CALL,
                       "model_call", "CALL-OK"), triples(self.db))
        self.assertNotIn("CALL-UNKNOWN", {row.to_id for row in
                                          self.db.scalars(select(ComplianceLineageEdge).where(
                                              ComplianceLineageEdge.to_type == "model_call")).all()})

    # ── 启动补登 ─────────────────────────────────────────────────────────
    def test_backfill_rebuilds_ledger_idempotently(self):
        record_ingest_edge(self.db, task_id="TR-ING-1", dataset=self.dataset)
        task = self._training_task(self.raw_version.version_id)
        register_trained_version(self.db, task)
        record_call_edge(self.db, self._call("CALL-OK", task.target_version))
        self.db.commit()
        expected = triples(self.db)
        self.assertTrue(expected)
        self.db.execute(delete(ComplianceLineageEdge))
        self.db.commit()
        self.assertEqual(self.count_edges(), 0)

        original = database_module.SessionLocal
        database_module.SessionLocal = sessionmaker(bind=self.db.bind)
        try:
            ensure_lineage_edges_backfill()
            rebuilt = triples(self.db)
            ensure_lineage_edges_backfill()
            again = triples(self.db)
            counts = ledger_summary(self.db)
        finally:
            database_module.SessionLocal = original

        self.assertEqual(expected, rebuilt)
        self.assertEqual(rebuilt, again)          # 重启补登不翻倍
        self.assertEqual(counts["total"], len(expected))
        self.assertEqual(counts["byRelation"][RELATION_INGEST], 1)
        self.assertEqual(counts["byRelation"][RELATION_MODEL_CALL], 1)


if __name__ == "__main__":
    unittest.main()
