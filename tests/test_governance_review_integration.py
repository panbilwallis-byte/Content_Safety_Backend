"""Merge checks: real application registration and atomic version publishing."""
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import func, inspect, select

from test_agent_traces import env
from test_anomaly_reviews import API as ANOMALY_API, candidate, fixture_service
from test_risk_dispositions import API as RISK_API, command, workflow
from app.core.auth import create_token
from app.models.anomaly_reviews import AnomalyReview
from app.models.risk_disposition import RiskDisposition
from app.models.tables import Dataset, DatasetRecord, DatasetVersion, Task
from app.routers import anomaly_reviews, risk_dispositions


def test_new_models_and_routes_are_registered_in_main_application(env, monkeypatch):
    with env["sessions"]() as db:
        tables = inspect(db.get_bind()).get_table_names()
        assert {"anomaly_reviews", "risk_dispositions"} <= set(tables)

    def unavailable(*args, **kwargs):
        raise HTTPException(404, "isolated model result not found")

    monkeypatch.setattr(anomaly_reviews, "read_model", unavailable)
    monkeypatch.setattr(risk_dispositions, "read_model", unavailable)
    model_headers = {"X-Model-Access-Token": "isolated-valid-model-token-123456789"}
    for path in (ANOMALY_API, RISK_API):
        assert env["client"].get(path + "/state", headers={"Authorization": ""}).status_code == 401
        assert env["client"].get(path + "/state").status_code == 422
        response = env["client"].get(path + "/state", headers=model_headers)
        assert response.status_code == 404
        assert "isolated model result not found" in response.text  # reached new handler
    reader_headers = {**model_headers, "Authorization": "Bearer " + create_token(3, "user-3", "审计员")}
    assert env["client"].post(ANOMALY_API + "/change-set/validate", headers=reader_headers).status_code == 403
    assert env["client"].post(RISK_API + "/preview", headers=reader_headers).status_code == 403


def test_anomaly_publish_rolls_back_partial_version_on_lineage_failure():
    sessions, client, dataset_id, version, (first, _), read_model = fixture_service()
    try:
        with (
            patch.object(anomaly_reviews, "SessionLocal", sessions),
            patch.object(anomaly_reviews, "read_model", side_effect=read_model),
            patch.object(anomaly_reviews.reviews, "load_secret", return_value=b"isolated-secret"),
        ):
            assert candidate(client, version, first, "submit").status_code == 200
            assert candidate(client, version, first, "approve").status_code == 200
            token = client.post(ANOMALY_API + "/change-set/validate").json()["data"]["token"]
            with patch.object(anomaly_reviews.reviews, "record_process_edges", side_effect=HTTPException(409, "lineage conflict")):
                assert client.post(ANOMALY_API + "/change-set/generate-version", json={"validation_token": token}).status_code == 409
            with sessions() as db:
                assert db.scalar(select(func.count()).select_from(DatasetVersion)) == 1
                assert db.scalar(select(func.count()).select_from(DatasetRecord)) == 2
                assert db.scalar(select(func.count()).select_from(Task)) == 0
                assert db.get(Dataset, dataset_id).version == "v1.0.0"
                assert db.get(AnomalyReview, "candidate-1").status == "APPROVED"
            assert client.post(ANOMALY_API + "/change-set/generate-version", json={"validation_token": token}).status_code == 200
    finally:
        client.close()
        sessions.kw["bind"].dispose()


def test_risk_publish_rolls_back_partial_version_on_lineage_failure(workflow):
    client, sessions, version, (first, _, _), summary = workflow
    assert command(client, version, first, "submit").status_code == 200
    assert command(client, version, first, "confirm", note="确认风险").status_code == 200
    assert command(client, version, first, "propose", treatment="redact", replacement_text="处理后的正文", note="脱敏").status_code == 200
    assert command(client, version, first, "approve", note="通过").status_code == 200
    token = client.post(RISK_API + "/preview").json()["data"]["token"]
    with patch.object(risk_dispositions.dispositions, "record_process_edges", side_effect=HTTPException(409, "lineage conflict")):
        assert client.post(RISK_API + "/publish", json={"validation_token": token}).status_code == 409
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(DatasetVersion)) == 1
        assert db.scalar(select(func.count()).select_from(DatasetRecord)) == 3
        assert db.scalar(select(func.count()).select_from(Task)) == 0
        assert db.get(Dataset, summary["scope"]["dataset_id"]).version == "v1.0.0"
        assert db.get(RiskDisposition, ("risk-result", first)).status == "APPROVED"
    assert client.post(RISK_API + "/publish", json={"validation_token": token}).status_code == 200
