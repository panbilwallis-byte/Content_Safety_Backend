import unittest
from unittest.mock import patch

from fastapi import Request
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.core.time import now_shanghai
from app.models.tables import ComplianceErrorComment, Dataset, Model, Task
from app.routers.compliance import (
    AlertAction,
    ErrorCommentBody,
    add_error_comment,
    act_on_alert,
    alert_detail,
    audit_detail,
    audits,
    contexts,
    evidence_detail,
    error_comments,
    lineage,
    issue_summary,
    overview,
    trace_detail,
)
from app.services.compliance_service import ensure_compliance_data, execute_compliance_task


class ComplianceApiTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine, expire_on_commit=False)
        self.db = self.Session()
        dataset = Dataset(
            name="合规训练数据集",
            category="测试",
            source_type="business",
            version="dsv_000001",
            status="ready",
            description="",
            metadata_json={"record_count": 120},
        )
        model = Model(
            name="内容安全模型",
            category="风险识别",
            version="v1.0.0",
            status="ready",
            description="",
            metadata_json={},
        )
        self.db.add_all([dataset, model])
        self.db.flush()
        now = now_shanghai()
        self.db.add(Task(
            task_id="PROCESS-001",
            name="去重与补全",
            capability_code="data_process",
            trace_id="TRACE-PROCESS-001",
            status="succeeded",
            source_name="数据库",
            dataset_name=dataset.name,
            storage_gb=0,
            progress=100,
            success_count=120,
            duplicate_count=2,
            anomaly_count=1,
            input_data={
                "dataset_id": dataset.id,
                "dataset_version_id": dataset.version,
                "template_name": "去重与补全",
                "rules": ["精确去重", "字段补全"],
            },
            config={},
            result={"total_count": 120, "output_version": "dsv_000002"},
            dataset_version=dataset.version,
            created_at=now,
            finished_at=now,
        ))
        self.db.commit()
        ensure_compliance_data(self.db)

    def tearDown(self):
        self.db.close()

    def test_contexts_return_one_candidate_per_version(self):
        response = contexts("dataset", None, None, self.db)
        candidates = response["data"]["candidates"]
        versions = {item["model_version"] for item in candidates}
        self.assertIn("dsv_000001", versions)
        self.assertIn("dsv_000002", versions)
        self.assertEqual({item["source_id"] for item in candidates}, {"1"})

    def test_lineage_expands_to_training_and_model(self):
        response = lineage("dataset", "1", "dsv_000001", "downstream", self.db)
        data = response["data"]
        relations = {edge["relation"] for edge in data["edges"]}
        self.assertEqual(
            relations,
            {"输入数据引用", "输出版本登记", "训练数据绑定", "训练产物登记"},
        )
        node_ids = {node["id"] for node in data["nodes"]}
        self.assertTrue(all(edge["from_id"] in node_ids and edge["to_id"] in node_ids for edge in data["edges"]))

    def test_issue_summary_aggregates_gaps_and_errors_in_one_read(self):
        response = issue_summary(self.db)
        data = response["data"]
        self.assertEqual(set(data["gaps"]), {"link", "task", "model"})
        self.assertGreaterEqual(data["gaps"]["link"]["expected"], 1)
        self.assertTrue(all(item["state"] == "open" for item in data["errors"]))
    def test_training_evidence_contains_five_traceability_fields(self):
        response = evidence_detail("evidence-training-TR-COMPLIANCE-001", self.db)
        fields = response["data"]["redacted_fields"]
        self.assertEqual([item["key"] for item in fields], ["input", "time", "interface", "version", "output"])
        self.assertEqual(fields[2]["value"], "低秩适配微调")
        self.assertEqual(response["data"]["integrity_state"], "verified")

    def test_audits_trace_overview_and_optimistic_alert_write(self):
        audit_page = audits("training_monitor", "training_task", "TR-COMPLIANCE-001", None, None, None, 1, 20, self.db)
        self.assertEqual(audit_page["data"]["total"], 1)
        detail = audit_detail(audit_page["data"]["items"][0]["id"], self.db)
        self.assertEqual(detail["data"]["result"]["kind"], "training_monitor")
        summary = overview("2026-01-01", "2027-01-01", "all", self.db)
        self.assertGreater(summary["data"]["expected_count"], 0)
        trace = trace_detail("TRACE-COMPLIANCE-001", True, self.db)
        self.assertGreaterEqual(len(trace["data"]["records"]), 3)
        current = alert_detail("ALERT-COMPLIANCE-001", self.db)["data"]
        request = Request({"type": "http", "headers": []})
        changed = act_on_alert(
            current["id"],
            "claim",
            AlertAction(expected_version=current["version"]),
            request,
            self.db,
        )["data"]
        self.assertEqual(changed["current_status"], "processing")
        self.assertEqual(changed["version"], current["version"] + 1)

    def test_all_compliance_capabilities_persist_a_pollable_task(self):
        capabilities = [
            "lineage_audit",
            "training_monitor",
            "reasoning_audit",
            "neuron_audit",
            "full_chain_audit",
        ]
        with patch("app.core.database.SessionLocal", self.Session):
            results = [
                execute_compliance_task(code, {"model_id": "1"}, f"trace-{code}")
                for code in capabilities
            ]
        self.assertTrue(all(item["status"] == "succeeded" for item in results))
        with self.Session() as verify:
            self.assertTrue(all(verify.get(Task, item["task_id"]) is not None for item in results))

    def test_error_comments_persist_for_chinese_error_id_in_chronological_order(self):
        error_id = "link:输入数据引用:edge-process-input-tsk_process_demo_06"
        first = Request({"type": "http", "headers": [(b"x-request-id", b"comment-001"), (b"x-user-id", b"alice")]})
        second = Request({"type": "http", "headers": [(b"x-request-id", b"comment-002")]})
        add_error_comment(error_id, ErrorCommentBody(content="已联系数据组补登记"), first, self.db)
        add_error_comment(error_id, ErrorCommentBody(content="等待复核完成"), second, self.db)
        comments = error_comments(error_id, self.db)["data"]
        self.assertEqual([item["content"] for item in comments], ["已联系数据组补登记", "等待复核完成"])
        self.assertEqual(comments[0]["error_id"], error_id)
        self.assertEqual(comments[0]["author_id"], "alice")
        self.assertEqual(comments[1]["author_name"], "当前用户")
        self.db.close()
        self.db = self.Session()
        self.assertEqual(self.db.query(ComplianceErrorComment).filter_by(error_id=error_id).count(), 2)

    def test_non_process_tasks_have_ordered_five_element_evidence(self):
        capabilities = [
            "data_ingest", "value_score", "model_risk_governance", "anomaly_detect",
            "evaluation", "reasoning_audit", "scenario_governance",
        ]
        now = now_shanghai()
        for index, capability in enumerate(capabilities):
            self.db.add(Task(
                task_id=f"TASK-{index}", name=capability, capability_code=capability,
                trace_id=None, status="succeeded", source_name=None, dataset_name=None,
                storage_gb=0, progress=100, success_count=0, duplicate_count=0, anomaly_count=0,
                input_data={"dataset_version_id": "dsv_000001", "interface": "测试接口"}, config={},
                result={"output": "测试结果", "version": "v1"}, dataset_version="dsv_000001",
                created_at=now, finished_at=now,
            ))
        self.db.commit()
        ensure_compliance_data(self.db)
        for index in range(len(capabilities)):
            payload = evidence_detail(f"evidence-task-TASK-{index}", self.db)["data"]
            fields = payload["redacted_fields"]
            self.assertEqual([item["key"] for item in fields], ["input", "time", "interface", "version", "output"])
            self.assertTrue(all(item["state"] in {"verified", "missing", "not_applicable"} for item in fields))


if __name__ == "__main__":
    unittest.main()
