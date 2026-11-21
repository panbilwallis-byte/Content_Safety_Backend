from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.anomaly_reviews import AnomalyReview
from app.models.tables import Base, Dataset, DatasetRecord, DatasetVersion, DatasetVersionItem
from app.routers import anomaly_reviews
from app.services.dataset_version_service import register_version

MODEL_TOKEN = "actual-model-token-for-tests-12345"
API = "/api/v1/data-governance/anomaly-reviews/saved-result"


def fixture_service():
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    with sessions() as db:
        dataset = Dataset(name="异常复核测试", version="v1.0.0")
        db.add(dataset)
        db.flush()
        dataset_id = dataset.id
        records = [
            DatasetRecord(dataset_id=dataset_id, task_id="upload",
                          payload={"text": "基尔霍夫电路定律", "topic_label": "党史"}),
            DatasetRecord(dataset_id=dataset_id, task_id="upload",
                          payload={"text": "楞次定律", "topic_label": "历史"}),
        ]
        db.add_all(records)
        db.flush()
        record_ids = [record.id for record in records]
        version_id = register_version(db, dataset, "v1.0.0").version_id
        db.commit()
    snapshots = {}
    for index, record_id in enumerate(record_ids, 1):
        revision = f"{version_id}:{record_id}"
        snapshots[str(record_id)] = {
            "id": str(record_id), "revision_id": revision,
            "candidates": [{
                "candidate_id": f"candidate-{index}", "input_sample_revision_id": revision,
                "field_changes": [{"field": "topic_label", "before": "党史" if index == 1 else "历史",
                                   "after": "物理学"}],
                "validation_results": [{"name": "技术检查通过", "passed": True}],
            }],
        }

    def read_model(path, token):
        if token != MODEL_TOKEN:
            raise HTTPException(404, "不属于当前令牌")
        if "/samples/" in path:
            return snapshots[path.rsplit("/", 1)[-1]]
        return {"id": "saved-result", "scope": {"dataset_id": dataset_id, "version_id": version_id,
                                                "scheme_id": "anomaly-basic-v2.1", "language": "all"}}

    app = FastAPI()

    @app.middleware("http")
    async def actor(request, call_next):
        request.state.user = {"uid": 7, "username": "tester", "tier": request.headers.get("x-test-tier", "operator")}
        return await call_next(request)

    app.include_router(anomaly_reviews.router, prefix="/api/v1")
    client = TestClient(app, headers={"X-Model-Access-Token": MODEL_TOKEN})
    return sessions, client, dataset_id, version_id, record_ids, read_model


def candidate(client, version_id, record_id, action, number=1, headers=None, **fields):
    return client.post(f"{API}/samples/{record_id}/candidates/{action}", headers=headers,
                       json={"candidate_id": f"candidate-{number}",
                             "input_sample_revision_id": f"{version_id}:{record_id}", **fields})


def test_review_and_publish_keeps_original_snapshot():
    sessions, client, dataset_id, version_id, (first, second), read_model = fixture_service()
    with (
        patch.object(anomaly_reviews, "SessionLocal", sessions),
        patch.object(anomaly_reviews, "read_model", side_effect=read_model),
        patch.object(anomaly_reviews.reviews, "load_secret", return_value=b"test-signing-secret"),
    ):
        assert candidate(client, version_id, first, "approve").status_code == 409
        assert candidate(client, version_id, first, "submit").json()["data"]["status"] == "PENDING_REVIEW"
        assert candidate(client, version_id, first, "submit").status_code == 200
        assert candidate(client, version_id, first, "approve").json()["data"]["status"] == "APPROVED"
        state = client.get(f"{API}/state").json()["data"]
        assert len(state["reviews"][0]["timeline"]) == 2
        assert len(state["change_set"]["entries"]) == 1
        with sessions() as db:
            assert db.get(DatasetRecord, first).payload["topic_label"] == "党史"
            assert db.get(Dataset, dataset_id).version == "v1.0.0"
        check = client.post(f"{API}/change-set/validate").json()["data"]
        assert check["valid"] is True and check["count"] == 1
        body = {"validation_token": check["token"]}
        response = client.post(f"{API}/change-set/generate-version", json=body)
        assert response.status_code == 200, response.text
        published = response.json()["data"]
        assert client.post(f"{API}/change-set/generate-version", json=body).json()["data"] == published
        state = client.get(f"{API}/state").json()["data"]
        assert not state["editable"] and state["reviews"][0]["status"] == "PUBLISHED"
        assert state["change_set"]["entries"] == []
    with sessions() as db:
        version = db.get(DatasetVersion, published["new_dataset_version_id"])
        assert version.label == db.get(Dataset, dataset_id).version == "v1.0.1"
        assert version.dataset_id == dataset_id and version.source_version_id == version_id
        assert version.sample_count == db.get(DatasetVersion, version_id).sample_count == 2
        new_ids = set(db.scalars(select(DatasetVersionItem.record_id)
                               .where(DatasetVersionItem.version_id == version.version_id)))
        assert second in new_ids and first not in new_ids
        derived = db.get(DatasetRecord, (new_ids - {second}).pop())
        original = db.get(DatasetRecord, first)
        assert derived.source_record_id == first and original.payload["topic_label"] == "党史"
        assert derived.payload == {**original.payload, "topic_label": "物理学"}
        assert len(list(db.scalars(select(DatasetVersion)))) == 2


def test_review_rejects_wrong_token_reader_stale_revision_and_client_changes():
    sessions, client, _, version_id, (first, _), read_model = fixture_service()
    with (
        patch.object(anomaly_reviews, "SessionLocal", sessions),
        patch.object(anomaly_reviews, "read_model", side_effect=read_model),
    ):
        wrong = {"X-Model-Access-Token": "a-different-valid-form-model-token-000"}
        assert candidate(client, version_id, first, "submit", headers=wrong).status_code == 404
        reader = {"X-Test-Tier": "reader"}
        assert not client.get(f"{API}/state", headers=reader).json()["data"]["editable"]
        assert candidate(client, version_id, first, "submit", headers=reader).status_code == 403
        assert candidate(client, "old-revision", first, "submit").status_code == 409
        assert candidate(client, version_id, first, "submit", after_label="伪造标签").status_code == 422
    with sessions() as db:
        assert list(db.scalars(select(AnomalyReview))) == []


def test_approval_changes_invalidate_publish_preview():
    sessions, client, _, version_id, (first, second), read_model = fixture_service()
    with (
        patch.object(anomaly_reviews, "SessionLocal", sessions),
        patch.object(anomaly_reviews, "read_model", side_effect=read_model),
        patch.object(anomaly_reviews.reviews, "load_secret", return_value=b"test-signing-secret"),
    ):
        for action in ("submit", "approve"):
            assert candidate(client, version_id, first, action).status_code == 200
        check = client.post(f"{API}/change-set/validate").json()["data"]
        assert check["valid"]
        for action in ("submit", "approve"):
            assert candidate(client, version_id, second, action, number=2).status_code == 200
        assert client.post(f"{API}/change-set/generate-version",
                           json={"validation_token": check["token"]}).status_code == 409
        assert client.post(f"{API}/change-set/remove", json={"candidate_id": "candidate-2"}).status_code == 200
        assert client.get(f"{API}/state").json()["data"]["change_set"]["entries"][0]["candidate_id"] == "candidate-1"
    with sessions() as db:
        assert len(list(db.scalars(select(DatasetVersion)))) == 1
