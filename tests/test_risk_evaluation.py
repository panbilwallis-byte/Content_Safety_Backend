"""真实风险评估端到端回归，隔离在内存数据库，不调用模型。"""
import json
from copy import deepcopy
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from app.core.time import now_shanghai
from app.models.tables import Base, Dataset, DatasetRecord, DatasetVersion, DatasetVersionItem, EvaluationRun, EvaluationMetricRevision
from app.routers import evaluations as api
from app.services import risk_evaluation_service as service
from app.services.dataset_version_service import manifest_hash


@pytest.fixture
def database(monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(api, "SessionLocal", sessions)
    monkeypatch.setenv("LLM_ACCESS_TOKEN", "isolated-unit-token")
    with engine.begin() as c:
        for sql in (
            "CREATE TABLE llm_auth_tokens(token_hash TEXT,enabled INTEGER,expires_at TEXT,deleted_at TEXT)",
            "CREATE TABLE llm_models(id TEXT,name TEXT)",
            "CREATE TABLE llm_governance_tasks(id TEXT,scope_json TEXT,model_id TEXT,model_version TEXT,status TEXT,token_hash TEXT)",
            "CREATE TABLE llm_governance_results(id TEXT,task_id TEXT,kind TEXT,token_hash TEXT,summary_json TEXT,samples_json TEXT,created_at TEXT)",
            "CREATE TABLE llm_governance_task_samples(task_id TEXT,sample_id TEXT,status TEXT,output_json TEXT,error_code TEXT)",
        ):
            c.execute(text(sql))
        c.execute(text("INSERT INTO llm_auth_tokens VALUES(:token,1,'2099-01-01T00:00:00+00:00',NULL)"), {"token": service.token_hash()})
        c.execute(text("INSERT INTO llm_models VALUES('model','书生测试模型')"))
    references = ["safe", "safe", "high", "high", "medium", "low", None]
    predictions = ["safe", "high", "high", "failed", "high", "notice", "safe"]
    public = []
    with sessions() as db:
        db.add(Dataset(id=3, name="测试数据集", version="v1.0.2"))
        pairs = [(i, f"content-{i}") for i in range(1, 8)]
        hashed = manifest_hash(pairs)
        db.add(DatasetVersion(version_id="dsv-test", dataset_id=3, label="v1.0.2", sample_count=7, manifest_hash=hashed))
        for i, (reference, prediction) in enumerate(zip(references, predictions), 1):
            payload = {"text": f"样本{i}"}
            if reference:
                payload["reference_risk_level"] = reference
            db.add(DatasetRecord(id=i, dataset_id=3, task_id="ingest", payload=payload, content_hash=f"content-{i}"))
            db.add(DatasetVersionItem(version_id="dsv-test", record_id=i, content_hash=f"content-{i}"))
            output = {"status": "assessed", "findings": [] if prediction == "safe" else [{"suggestedLevel": prediction.upper()}]}
            if prediction == "failed":
                output = {"error_detail": "引用不在正文中"}
            else:
                public.append({"id": str(i), "reference_risk_level": reference})
            db.execute(text("INSERT INTO llm_governance_task_samples VALUES('risk-task',:id,:status,:output,:error)"),
                       {"id": str(i), "status": "error" if prediction == "failed" else "success",
                        "output": json.dumps(output), "error": "invalid_structured_output" if prediction == "failed" else None})
        summary = {"dataset_name": "测试数据集", "version_label": "v1.0.2", "manifest_hash": hashed,
                   "finished_at": "2026-10-31T14:01:36+00:00", "valid_count": 6, "failed_count": 1, "unassessable_count": 0}
        scope = json.dumps({"dataset_id": 3, "version_id": "dsv-test"})
        db.execute(text("INSERT INTO llm_governance_tasks VALUES('risk-task',:scope,'model','internlm','succeeded',:token)"),
                   {"scope": scope, "token": service.token_hash()})
        db.execute(text("INSERT INTO llm_governance_results VALUES('risk-result','risk-task','governance-risk',:token,:summary,:samples,:created)"),
                   {"token": service.token_hash(), "summary": json.dumps(summary), "samples": json.dumps(public), "created": summary["finished_at"]})
        service.ensure_metrics(db)
        db.commit()
    yield sessions
    engine.dispose()


def config(database):
    source = api.contexts("task_result")["data"]["candidates"][0]
    with database() as db:
        revisions = list(db.scalars(select(EvaluationMetricRevision.revision_id).where(
            EvaluationMetricRevision.metric_code.in_(source["supportedMetricCodes"]))))
    return {"targetStage": "final", "executionMode": "reference", "sourceRefs": [source],
            "datasetVersionRef": source["datasetVersion"], "labelVersionRef": source["labelVersion"],
            "modelVersionRef": source["modelVersion"], "metricRevisionRefs": revisions, "sampleScope": {"mode": "all"}}


def create(database):
    c = config(database)
    preflight = api.preflight(c)["data"]
    assert preflight["canCreate"], preflight["issues"]
    c["preflightToken"] = preflight["token"]
    return api.create_task(api.CreateTask(name="真实风险评估", config=c), Request({"type": "http", "headers": []}))["data"]


def test_real_calculation_and_all_samples_and_stable_evidence(database):
    task = create(database)
    api.start(task["taskId"])
    run = api.run(task["runId"])["data"]
    results = {r["metricCode"]: r for r in run["metricResults"]}
    assert results["risk_level_accuracy"]["numerator"] == 2
    assert results["risk_level_accuracy"]["denominator"] == 6
    assert results["risk_high_recall"]["value"] == .5
    assert results["risk_safe_fpr"]["value"] == .5
    assert run["algorithmMode"] == "risk_reference"
    rows = api.samples(task["runId"], "risk_level_accuracy", outcome="all")["data"]
    assert rows["total"] == 7
    assert rows["items"][3]["predictionStatus"] == "failed"
    assert rows["items"][3]["predictedLevel"] is None
    assert rows["items"][6]["includedInMetric"] is False
    pages = [api.samples(task["runId"], "risk_level_accuracy", page=p, page_size=3, outcome="all")["data"]
             for p in (1, 2, 3)]
    assert [len(p["items"]) for p in pages] == [3, 3, 1]
    assert len({s["sampleId"] for p in pages for s in p["items"]}) == 7
    first = api.evidence(task["runId"])["data"]
    assert first == api.evidence(task["runId"])["data"]
    assert all(check["valid"] for check in first["checks"])
    assert api.evidence_detail(first["entries"][0]["evidenceId"])["data"] == first["entries"][0]
    assert len(first["entries"][0]["material"]["samples"]) == 7
    export = api.create_export(task["runId"], {"kind": "evidence_bundle"})["data"]
    ticket = api.download_ticket(export["artifactId"])["data"]
    document = api.download_artifact(export["artifactId"])["data"]
    from hashlib import sha256
    assert sha256(document["content"].encode("utf-8")).hexdigest() == ticket["sha256"]
    package = json.loads(document["content"])
    assert len(package["samples"]) == 7
    assert package["evidence"] == first["entries"]
    assert api.run(task["runId"])["data"]["exports"][0]["artifactId"] == export["artifactId"]


def test_input_is_frozen_before_execution(database):
    task = create(database)
    with database() as db:
        db.execute(text("UPDATE llm_governance_task_samples SET output_json='{}'"))
        db.commit()
    api.start(task["taskId"])
    assert api.run(task["runId"])["data"]["metricResults"][0]["denominator"] > 0


def test_missing_source_access_and_unsupported_sources(database, monkeypatch):
    assert api.contexts("model_comparison")["data"]["candidates"] == []
    assert api.contexts("audit_record")["data"]["candidates"] == []
    monkeypatch.setenv("LLM_ACCESS_TOKEN", "another-token")
    with pytest.raises(HTTPException) as exc:
        api.contexts("task_result")
    assert exc.value.status_code == 403


def test_invalid_mode_version_and_zero_denominator(database):
    c = config(database)
    c["executionMode"] = "rerun"
    assert not api.preflight(c)["data"]["canCreate"]
    c["executionMode"] = "reference"
    c["modelVersionRef"] = "different"
    assert not api.preflight(c)["data"]["canCreate"]
    task = create(database)
    with database() as db:
        run = db.get(EvaluationRun, task["runId"])
        snapshot = deepcopy(run.snapshot)
        for row in snapshot["riskEvaluation"]["samples"]:
            if row["expectedLevel"] == "high":
                row["expectedLevel"] = "safe"
        snapshot["riskEvaluationHash"] = service.digest(snapshot["riskEvaluation"])
        run.snapshot = snapshot
        db.commit()
    api.start(task["taskId"])
    results = api.run(task["runId"])["data"]["metricResults"]
    high = next(r for r in results if r["metricCode"] == "risk_high_recall")
    assert high["value"] is None and high["reasonCode"] == "zero_denominator"
    fpr = next(r for r in results if r["metricCode"] == "risk_safe_fpr")
    assert fpr["judgmentStatus"] == "inconclusive"  # 失败样本不能被当作安全。


def test_tampering_is_detected(database):
    task = create(database)
    api.start(task["taskId"])
    with database() as db:
        run = db.get(EvaluationRun, task["runId"])
        snapshot = deepcopy(run.snapshot)
        snapshot["riskEvidence"][0]["material"]["samples"][0]["expectedLevel"] = "high"
        run.snapshot = snapshot
        db.commit()
    assert not api.evidence(task["runId"])["data"]["checks"][-1]["valid"]


def publish_governance(database, kind):
    """六条有标注样本和一条无标注样本，包含误判、失败与不可评估。"""
    value = kind == "governance-value"
    references = ["high", "high", "medium", "low", "low", "low", None] if value else [True, True, False, False, False, False, None]
    predictions = ["high", "failed", "high", "low", "unavailable", "high", "low"] if value else [True, "failed", True, False, "unavailable", False, False]
    field = "reference_value_tier" if value else "reference_is_anomaly"
    public = []
    with database() as db:
        for i, (ref, prediction) in enumerate(zip(references, predictions), 1):
            record = db.get(DatasetRecord, i)
            record.payload = {**record.payload, field: ref}
            if prediction == "failed":
                output = {"error_detail": "连接超时"}
            elif prediction == "unavailable":
                output = {"status": "unavailable", "unavailable_reason": "正文不足"}
            elif value:
                score = {"high": 90, "medium": 70, "low": 40}[prediction]
                output = {"status": "scored", "dimensions": [{"name": "文化价值", "score": score}]}
                public.append({"id": str(i), "score": score, "tier": prediction})
            else:
                output = {"status": "assessed", "findings": [{"type": "标签异常"}] if prediction else []}
            db.execute(text("INSERT INTO llm_governance_task_samples VALUES('new-task',:id,:status,:output,:error)"),
                       {"id": str(i), "status": "error" if prediction == "failed" else "success", "output": json.dumps(output),
                        "error": "timeout" if prediction == "failed" else None})
        summary = {"dataset_name": "测试数据集", "version_label": "v1.0.2", "manifest_hash": db.get(DatasetVersion, "dsv-test").manifest_hash,
                   "finished_at": "2026-10-31T15:01:36+00:00", "valid_count": 5, "failed_count": 1, "unavailable_count": 1,
                   "evaluation_contract": service.GOVERNANCE_VERSION,
                   "reference_annotations": {str(i): ref for i, ref in enumerate(references, 1)},
                   "evaluation_policy": {"dimensions": [{"name": "文化价值", "weight": 1}], "high_threshold": 85, "medium_threshold": 60}}
        db.execute(text("INSERT INTO llm_governance_tasks VALUES('new-task',:scope,'model','test-model','succeeded',:token)"),
                   {"scope": json.dumps({"dataset_id": 3, "version_id": "dsv-test", "language": "all"}), "token": service.token_hash()})
        db.execute(text("INSERT INTO llm_governance_results VALUES('new-result','new-task',:kind,:token,:summary,:samples,:created)"),
                   {"kind": kind, "token": service.token_hash(), "summary": json.dumps(summary), "samples": json.dumps(public), "created": summary["finished_at"]})
        db.commit()
    return next(s for s in api.contexts("task_result")["data"]["candidates"] if s["entityId"] == "new-result")


def governance_config(database, kind):
    source = publish_governance(database, kind)
    with database() as db:
        refs = list(db.scalars(select(EvaluationMetricRevision.revision_id).where(EvaluationMetricRevision.metric_code.in_(source["supportedMetricCodes"]))))
    return {"targetStage": "final", "executionMode": "reference", "sourceRefs": [source],
            "datasetVersionRef": source["datasetVersion"], "labelVersionRef": source["labelVersion"],
            "modelVersionRef": source["modelVersion"], "metricRevisionRefs": refs, "sampleScope": {"mode": "all"}}


@pytest.mark.parametrize("kind", ["governance-value", "governance-anomaly"])
def test_value_and_anomaly_real_calculation_freeze_export(database, kind):
    c = governance_config(database, kind)
    preflight = api.preflight(c)["data"]
    assert preflight["canCreate"], preflight["issues"]
    c["preflightToken"] = preflight["token"]
    task = api.create_task(api.CreateTask(name="治理结果真实评估", config=c), Request({"type": "http", "headers": []}))["data"]
    # 创建后来源修改不影响本次冻结评估。
    with database() as db:
        db.execute(text("UPDATE llm_governance_task_samples SET output_json='{}' WHERE task_id='new-task'"))
        db.commit()
    api.start(task["taskId"])
    run = api.run(task["runId"])["data"]
    metrics = {r["metricCode"]: r for r in run["metricResults"]}
    accuracy = metrics["value_tier_accuracy" if kind == "governance-value" else "anomaly_accuracy"]
    assert accuracy["denominator"] == 6
    assert accuracy["numerator"] == (2 if kind == "governance-value" else 3)
    assert metrics["value_high_recall" if kind == "governance-value" else "anomaly_recall"]["value"] == .5
    if kind == "governance-anomaly":
        fpr = metrics["anomaly_normal_fpr"]
        assert fpr["value"] == .25 and fpr["reasonCode"] == "predictions_missing"
    rows = api.samples(task["runId"], accuracy["metricCode"], outcome="all")["data"]["items"]
    assert len(rows) == 7 and rows[1]["predictedLevel"] is None
    assert rows[4]["predictionStatus"] == "unassessable" and not rows[6]["includedInMetric"]
    assert rows[0]["expectedDisplay"] == ("高价值" if kind == "governance-value" else "异常")
    first = api.evidence(task["runId"])["data"]
    assert all(c["valid"] for c in first["checks"])
    assert first == api.evidence(task["runId"])["data"]
    job = api.create_export(task["runId"], {"kind": "evidence_bundle"})["data"]
    package = api.download_artifact(job["artifactId"])["data"]
    assert len(json.loads(package["content"])["samples"]) == 7
    from hashlib import sha256
    assert sha256(package["content"].encode()).hexdigest() == api.download_ticket(job["artifactId"])["data"]["sha256"]


@pytest.mark.parametrize("kind", ["governance-value", "governance-anomaly"])
def test_governance_missing_labels_mock_and_wrong_metric_are_rejected(database, kind):
    c = governance_config(database, kind)
    with database() as db:
        summary = json.loads(db.execute(text("SELECT summary_json FROM llm_governance_results WHERE id='new-result'")).scalar_one())
        summary["data_source"] = "mock"
        db.execute(text("UPDATE llm_governance_results SET summary_json=:summary WHERE id='new-result'"), {"summary": json.dumps(summary)})
        db.commit()
    assert "模拟样本" in str(api.preflight(c)["data"]["issues"])
    with database() as db:
        summary.pop("data_source")
        for record in db.scalars(select(DatasetRecord)):
            record.payload = {k: v for k, v in record.payload.items() if k != service.KINDS[kind][2]}
        summary["reference_annotations"] = {str(i): None for i in range(1, 8)}
        db.execute(text("UPDATE llm_governance_results SET summary_json=:summary WHERE id='new-result'"), {"summary": json.dumps(summary)})
        db.commit()
    assert "缺少人工标准答案" in str(api.preflight(c)["data"]["issues"])
    with database() as db:
        wrong = db.scalar(select(EvaluationMetricRevision.revision_id).where(EvaluationMetricRevision.metric_code == "risk_high_recall"))
    c["metricRevisionRefs"] = [wrong]
    assert "不匹配" in str(api.preflight(c)["data"]["issues"])


def test_value_annotation_change_and_legacy_contract_rejected(database):
    c = governance_config(database, "governance-value")
    with database() as db:
        record = db.get(DatasetRecord, 1)
        record.payload = {**record.payload, "reference_value_tier": "low"}
        db.commit()
    assert "人工标注已变化" in str(api.preflight(c)["data"]["issues"])
    with database() as db:
        summary = json.loads(db.execute(text("SELECT summary_json FROM llm_governance_results WHERE id='new-result'")).scalar_one())
        summary.pop("evaluation_contract")
        db.execute(text("UPDATE llm_governance_results SET summary_json=:summary WHERE id='new-result'"), {"summary": json.dumps(summary)})
        db.commit()
    assert "历史结果" in str(api.preflight(c)["data"]["issues"])
