from unittest.mock import patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.tables import Base, Dataset, DatasetRecord, DatasetVersion, DatasetVersionItem
from app.routers import risk_dispositions
from app.services.database_ingest import record_hash
from app.services.dataset_version_service import register_version


TOKEN = "test-model-token-for-risk-disposition-123"
API = "/api/v1/data-governance/risk-dispositions/risk-result"


@pytest.fixture
def workflow():
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    with sessions() as db:
        dataset = Dataset(name="风险处置测试", version="v1.0.0")
        db.add(dataset)
        db.flush()
        dataset_id = dataset.id
        payloads = [
            {"text": f"样本 {index}", "language": "zh", "reference_risk_level": "high"}
            for index in range(3)
        ]
        records = [DatasetRecord(dataset_id=dataset_id, task_id="upload",
                                 payload=payload, content_hash=record_hash(payload))
                   for payload in payloads]
        db.add_all(records)
        db.flush()
        ids = [row.id for row in records]
        version = register_version(db, dataset, dataset.version)
        version_id = version.version_id
        summary = {
            "id": "risk-result", "manifest_hash": version.manifest_hash,
            "scope": {"dataset_id": dataset_id, "version_id": version_id,
                      "scheme_id": "risk-v2", "language": "all"},
        }
        db.commit()

    def read_model(path, token, subject):
        if token != TOKEN:
            raise HTTPException(404, "无权读取该检测结果")
        if "/samples/" not in path:
            return summary
        sample_id = path.rsplit("/", 1)[-1]
        return {
            "id": sample_id, "dataset_id": dataset_id, "version_id": version_id,
            "revision_id": f"{version_id}:{sample_id}", "findings": [{"rule_id": "PII-03"}],
            "maximum_suggested_level": "HIGH", "primary_category": "个人信息暴露",
        }

    app = FastAPI()

    @app.middleware("http")
    async def actor(request, call_next):
        request.state.user = {"uid": 7, "username": "test-operator",
                              "tier": request.headers.get("x-test-role", "operator")}
        return await call_next(request)

    app.include_router(risk_dispositions.router, prefix="/api/v1")
    with (
        TestClient(app, headers={"X-Model-Access-Token": TOKEN}) as client,
        patch.object(risk_dispositions, "SessionLocal", sessions),
        patch.object(risk_dispositions, "read_model", side_effect=read_model),
        patch.object(risk_dispositions.dispositions, "load_secret", return_value=b"test-signing-key"),
    ):
        yield client, sessions, version_id, ids, summary
    engine.dispose()


def command(client, version, sample, action, **params):
    return client.post(f"{API}/samples/{sample}", json={
        "action": action, "input_sample_revision_id": f"{version}:{sample}", **params,
    })


def test_review_redact_exclude_and_publish_preserves_input_version(workflow):
    client, sessions, version, (first, second, third), _ = workflow
    assert client.get(f"{API}/state").json()["data"]["editable"]
    assert command(client, version, first, "approve", note="审核").status_code == 409
    assert command(client, version, first, "submit").json()["data"]["status"] == "PENDING_REVIEW"
    assert len(command(client, version, first, "submit").json()["data"]["events"]) == 1
    reviewed = command(client, version, first, "adjust", level="LOW", note="仅为有限风险").json()["data"]
    assert reviewed["reviewed_level"] == "LOW" and reviewed["original_level"] == "HIGH"
    assert command(client, version, first, "propose", treatment="redact", note="已处理",
                   replacement_text="样本 0").status_code == 422
    assert command(client, version, first, "propose", treatment="redact", note="正文脱敏",
                   replacement_text="脱敏后的正文").json()["data"]["status"] == "PENDING_APPROVAL"
    assert client.post(f"{API}/preview").status_code == 409
    assert command(client, version, first, "approve", note="已复查正文").json()["data"]["status"] == "APPROVED"
    stale = client.post(f"{API}/preview").json()["data"]
    assert command(client, version, second, "submit").status_code == 200
    assert command(client, version, second, "confirm", note="确认命中").status_code == 200
    assert command(client, version, second, "propose", treatment="exclude", note="不宜留在新版本").status_code == 200
    assert command(client, version, second, "approve", note="审核通过").status_code == 200
    assert command(client, version, third, "submit").status_code == 200
    assert command(client, version, third, "dismiss", note="证据不足").json()["data"]["status"] == "DISMISSED"
    assert client.post(f"{API}/publish", json={"validation_token": stale["token"]}).status_code == 409

    preview = client.post(f"{API}/preview").json()["data"]
    assert (preview["replaced_count"], preview["excluded_count"], preview["sample_count"]) == (1, 1, 2)
    payload = {"validation_token": preview["token"]}
    published = client.post(f"{API}/publish", json=payload).json()["data"]
    assert client.post(f"{API}/publish", json=payload).json()["data"] == published
    state = client.get(f"{API}/state").json()["data"]
    assert state["published"] == published and not state["editable"]
    assert [row["status"] for row in state["reviews"]] == ["PUBLISHED", "PUBLISHED", "DISMISSED"]
    assert state["reviews"][0]["events"][-1]["actor"] == "test-operator"
    assert command(client, version, third, "submit").status_code == 409

    with sessions() as db:
        original_ids = set(db.scalars(select(DatasetVersionItem.record_id).where(
            DatasetVersionItem.version_id == version,
        )))
        new_id = published["new_dataset_version_id"]
        new_ids = set(db.scalars(select(DatasetVersionItem.record_id).where(
            DatasetVersionItem.version_id == new_id,
        )))
        derived = db.scalar(select(DatasetRecord).where(DatasetRecord.source_record_id == first))
        assert original_ids == {first, second, third}
        assert new_ids == {derived.id, third}
        assert db.get(DatasetVersion, new_id).source_version_id == version
        assert derived.payload == {"text": "脱敏后的正文", "language": "zh"}
        assert db.get(DatasetRecord, first).payload["text"] == "样本 0"
        assert db.get(DatasetRecord, first).payload["reference_risk_level"] == "high"


def test_model_token_manifest_membership_and_role_are_required(workflow):
    client, _, version, (record, _, _), summary = workflow
    assert client.get(f"{API}/state", headers={"X-Model-Access-Token": "test-invalid-model-token-123456789"}).status_code == 404
    reader = {"x-test-role": "reader"}
    assert not client.get(f"{API}/state", headers=reader).json()["data"]["editable"]
    assert client.post(f"{API}/samples/{record}", headers=reader, json={
        "action": "submit", "input_sample_revision_id": f"{version}:{record}",
    }).status_code == 403
    assert command(client, "wrong-revision", record, "submit").status_code == 409
    assert command(client, version, 99, "submit").status_code == 409
    summary["manifest_hash"] = "outdated-manifest"
    assert command(client, version, record, "submit").status_code == 409
    assert client.get(f"{API}/state").json()["data"]["reviews"] == []
