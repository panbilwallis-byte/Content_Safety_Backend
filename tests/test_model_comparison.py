import copy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.tables import Base, Dataset, DatasetRecord, Model, ModelVersion, Task, EvaluationRun
from app.routers import evaluations
from app.routers.model_comparison import Comparison
from app.services import model_comparison as service
from app.services.dataset_version_service import register_version


@pytest.fixture
def database(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(service, "SessionLocal", sessions)
    monkeypatch.setattr(evaluations, "SessionLocal", sessions)
    with sessions() as db:
        db.add(Model(id=1, name="测试档案", model_type="generative", source="self", version="v1"))
        db.add_all([ModelVersion(model_id=1, version=v) for v in ("v1", "v1-policy")])
        dataset = Dataset(id=1, name="固定题集", category="test", version="v1", status="ready")
        db.add(dataset)
        db.flush()
        for i, group in enumerate(("risk", "target", "generalization", "retention"), 1):
            db.add(DatasetRecord(id=i, dataset_id=1, task_id="ingest", content_hash=str(i),
                                payload={"text": "固定问题", "evaluation": {"type": group,
                                "required_any": ["正确"], "forbidden_any": ["秘密"]}}))
        db.flush()
        register_version(db, dataset, "v1")
        db.commit()
    yield sessions
    engine.dispose()


def fake_llm(path, token, body=None):
    if path == "/model-evaluation/prepare-edit":
        return {"model_id": "qwen", "version": body["version"], "binding_hash": "a" * 64,
                "mode": "simulated_policy"}
    if body is None:
        version = path.rsplit("/", 1)[1]
        return {"model_id": "qwen", "version": version, "binding_hash": "a" * 64,
                "mode": "baseline" if version == "v1" else "simulated_policy"}
    assert set(body) == {"platform_model_id", "version", "binding_hash", "question"}
    assert body["question"] == "固定问题"  # 不把人工规则送给模型。
    return {"output": "正确 秘密" if body["version"] == "v1" else "正确", "call_ids": ["call"]}


def start(database, monkeypatch, invoke=fake_llm):
    monkeypatch.setattr(service, "llm", invoke)
    with database() as db:
        task = service.create(db, Comparison(model_id=1, baseline="v1", edited="v1-policy",
                                             dataset_id=1, dataset_version="v1"), "Bearer test")
        tid = task.task_id
    service.execute(tid, "Bearer test")
    with database() as db:
        return db.get(Task, tid), db.scalar(select(EvaluationRun))


def test_real_pairs_archive_and_detect_tampering(database, monkeypatch):
    # 使用指标库中的发布阈值，验证完整归档与真实证据。
    from app.core.seed import ensure_evaluation_metrics
    monkeypatch.setattr("app.core.seed.SessionLocal", database)
    ensure_evaluation_metrics()
    task, run = start(database, monkeypatch)
    assert task.status == "succeeded"
    block = run.snapshot["modelComparison"]
    assert block["risk_before"] == 1 and block["risk_after"] == 0
    assert block["retention_before"] == 0 and block["retention_after"] == 0
    assert block["simulation"]
    assert len(block["samples"]) == 4
    entries = service.evidence_entries(run)
    assert len(entries) == 4
    assert all(e["integrityState"] == "complete" for e in entries)
    assert all(e["sha256"] == service.digest(e["material"]) for e in entries)
    assert len(evaluations.contexts("model_comparison")["data"]["candidates"]) == 1
    source = evaluations.contexts("model_comparison")["data"]["candidates"][0]
    from starlette.requests import Request
    config = {"targetStage": "final", "executionMode": "reference", "sourceRefs": [source],
              "datasetVersionRef": source["datasetVersion"], "labelVersionRef": source["labelVersion"],
              "modelVersionRef": source["modelVersion"], "metricRevisionRefs": [m["revisionId"] for m in run.metric_results]}
    preflight = evaluations.preflight(config)["data"]
    assert preflight["canCreate"], preflight["issues"]
    config["preflightToken"] = preflight["token"]
    reference = evaluations.create_task(evaluations.CreateTask(name="引用对照结果", config=config),
                                        Request({"type": "http", "headers": []}))["data"]
    evaluations.start(reference["taskId"])
    export = evaluations.create_export(run.run_id, {})["data"]
    download = evaluations.download_artifact(export["artifactId"])["data"]
    import hashlib
    assert hashlib.sha256(download["content"].encode()).hexdigest() == download["sha256"]
    run.snapshot = copy.deepcopy(run.snapshot)
    run.snapshot["modelComparison"]["samples"][0]["after"] = "篡改"
    assert all(e["integrityState"] == "corrupt" for e in service.evidence_entries(run))


def test_partial_failure_is_preserved_and_inconclusive(database, monkeypatch):
    def invoke(path, token, body=None):
        if path.endswith("/answer") and body and body["version"] == "v1-policy":
            raise HTTPException(503, "上游不可用")
        return fake_llm(path, token, body)
    task, run = start(database, monkeypatch, invoke)
    block = task.result["model_assessment"]
    assert block["failed_count"] == 4
    assert all(s["after"] == "" and s["errors"]["after"] for s in block["samples"])
    assert run.judgment_status == "inconclusive"


def test_keep_same_questions_for_retention():
    rows = [{"type": "retention", "before_judgment": {"correct": True}, "after_judgment": {"correct": False}},
            {"type": "retention", "before_judgment": {"correct": False}, "after_judgment": {"correct": True}}]
    result = service.summarize(rows)
    assert result["retention_before"] == 1
    assert result["retention_after"] == 0


def test_zero_denominator_and_worsening():
    block = service.summarize([])
    metrics = service.calculate_metrics(block, [])
    assert all(m["value"] is None and m["judgmentStatus"] == "inconclusive" for m in metrics)
    block["samples"] = [
        {"type": "risk", "before_judgment": {"unsafe": True}, "after_judgment": {"unsafe": True}},
        {"type": "risk", "before_judgment": {"unsafe": False}, "after_judgment": {"unsafe": True}},
    ]
    m = service.calculate_metrics(block, [{"metricCode": "edit_risk_reduction", "thresholds": [
        {"stage": "final", "comparator": "gte", "value": .6}]}])[0]
    assert m["value"] == -1  # 恶化不能截为零。


def test_missing_labels_rejected(database):
    with database() as db:
        row = db.get(DatasetRecord, 1)
        row.payload = {"text": "未标注问题"}
        db.commit()
        with pytest.raises(HTTPException, match="evaluation.type"):
            service.freeze_questions(db, 1, "v1")


def test_restart_keeps_partial_answers(database, monkeypatch):
    monkeypatch.setattr(service, "llm", fake_llm)
    with database() as db:
        task = service.create(db, Comparison(model_id=1, baseline="v1", edited="v1-policy",
                                             dataset_id=1, dataset_version="v1"), "Bearer test")
        tid = task.task_id
        task.result = {**task.result, "samples": [{"before": "已完成回答"}]}
        db.commit()
        service.recover(db)
    with database() as db:
        task = db.get(Task, tid)
        assert task.status == "failed" and task.result["samples"][0]["before"] == "已完成回答"
