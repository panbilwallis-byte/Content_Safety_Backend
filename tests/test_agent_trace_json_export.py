"""Current JSON export requests from all four trace pages, through download."""
import hashlib
from uuid import uuid4

import pytest

from test_agent_traces import env, ROOT
from app.core.auth import create_token
from app.models.agent_traces import TraceEvent
from app.services import agent_trace_statistics as stats
from app.services import agent_trace_query as q


def command(env, view):
    return {"capability_code": "agent_trace_export", "name": "当前JSON文件导出", "input": {
        **env["scope"], "view": view, "export_kind": "view", "format": "json",
        "selected_trace_id": env["trace"],
        "scope": {"type": "sample" if view in {"execution", "tools"} else "filtered",
                  "trace_ids": [env["trace"]], "source_id": None, "issue_type": None, "q": None},
        "contents": ["normalized_json"], "redaction_profile": "standard", "anonymize_ids": True,
        "locale": "zh-CN", "timezone": "Asia/Shanghai", "ui_version": "1"}}


def download_export(env, body):
    response = env["client"].post("/api/v1/tasks/execute", json=body,
                                  headers={"X-Request-Id": uuid4().hex})
    assert response.status_code == 200, response.text
    task_id = response.json()["data"]["task_id"]
    task = env["client"].get("/api/v1/tasks/" + task_id).json()["data"]
    assert task["status"] == "succeeded", task
    artifact = task["result"]["artifacts"][0]
    assert artifact["mime_type"] == "application/json"
    assert artifact["filename"] == "agent-trace-" + body["input"]["view"] + ".json"
    response = env["client"].post(ROOT + f"/artifacts/{artifact['artifact_id']}/download-tickets",
                                  json={}, headers={"X-Request-Id": uuid4().hex})
    assert response.status_code == 200, response.text
    file = env["client"].get(response.json()["data"]["download_url"], headers={"Authorization": ""})
    assert file.status_code == 200, file.text
    assert file.headers["content-type"].startswith("application/json")
    assert hashlib.sha256(file.content).hexdigest() == artifact["sha256"]
    assert len(file.content) == artifact["size_bytes"]
    assert "person@example.com" not in file.text
    assert env["trace"] not in file.text
    assert env["scope"]["snapshot_id"] not in file.text
    return file.json()


@pytest.mark.parametrize("view", ["ingest", "statistics", "execution", "tools"])
def test_current_json_export_from_each_page(env, view):
    with env["sessions"]() as db:
        while stats.materialize(db):
            db.flush()
        db.commit()
    body = command(env, view)
    if view == "statistics":
        body["input"]["projection_version"] = stats.VERSION
        body["input"]["scope"].update(language="all", list_filter="all", trace_ids=[])
        body["input"].pop("selected_trace_id")
        advertised = env["client"].get(ROOT + "/statistics", params={"batch_id": env["batch"]}).json()["data"]
        assert next(k["formats"] for k in advertised["export_options"]["kinds"] if k["export_kind"] == "view") == ["json"]
        assert all(not c["code"].endswith("_png") for c in advertised["export_options"]["contents"])
    document = download_export(env, body)
    if view == "tools":
        assert document["sample"]["call_count"] == 7
        assert len(document["calls"]) == 7
        assert document["events"]
    else:
        assert document["view"] == view
        assert document["context_sample_count"] == 8
        assert len(document["samples"]) == (1 if view == "execution" else 8)
        assert all("rule_audit" in sample and "events" in sample for sample in document["samples"])
        if view == "statistics":
            assert "statistics" in document


@pytest.mark.parametrize("view", ["ingest", "execution"])
def test_filtered_and_empty_json_exports(env, view):
    body = command(env, view)
    body["input"].pop("selected_trace_id")
    body["input"]["scope"].update(type="filtered", trace_ids=[], q="AUD-0008")
    document = download_export(env, body)
    assert document["context_sample_count"] == 8
    assert len(document["samples"]) == 1
    body["input"]["scope"]["q"] = "no-such-sample"
    document = download_export(env, body)
    assert document["samples"] == []


def test_uploaded_five_openai_messages_export_current_json(env, tmp_path, monkeypatch):
    from test_agent_trace_workflow import upload
    monkeypatch.delenv("AGENT_TRACE_ENABLE_LEGACY_VALUE_SYNC", raising=False)
    records = [{"id": f"sample-{i}", "messages": [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好，有什么可以帮助你？"}]} for i in range(5)]
    bid, _ = upload(env, tmp_path, records)
    workspace = env["client"].get(ROOT + "/workspace", params={"batch_id": bid}).json()["data"]
    with env["sessions"]() as db:
        _, scope = q.resolve(db, bid, {})
    actual = {**env, "batch": bid, "scope": scope, "trace": workspace["resolved_selection"]["trace_id"]}
    body = command(actual, "ingest")
    with env["sessions"]() as db:
        body["input"]["selected_event_id"] = q.process_records(db, scope, actual["trace"], TraceEvent)[0]["event_id"]
    document = download_export(actual, body)
    assert document["context_sample_count"] == 5
    assert len(document["samples"]) == 5
    assert all(sample["data_origin"] == "actual" for sample in document["samples"])
    assert all(sample["rule_audit"]["result_state"] == "completed_empty" for sample in document["samples"])


@pytest.mark.parametrize("view", ["ingest", "tools"])
@pytest.mark.parametrize("change,status", [
    ({"contents": ["ingest_png"]}, 422),
    ({"format": "xml"}, 422),
    ({"redaction_profile": "none"}, 422),
    ({"anonymize_ids": False}, 422),
    ({"snapshot_id": "missing-snapshot"}, 410),
])
def test_json_export_keeps_validation(env, view, change, status):
    body = command(env, view)
    body["input"].update(change)
    response = env["client"].post("/api/v1/tasks/execute", json=body,
                                  headers={"X-Request-Id": uuid4().hex})
    assert response.status_code == status, response.text


@pytest.mark.parametrize("view", ["ingest", "statistics", "execution", "tools"])
def test_json_export_requires_export_permission(env, view):
    env["client"].headers["Authorization"] = "Bearer " + create_token(3, "user-3", "审计员")
    response = env["client"].post("/api/v1/tasks/execute", json=command(env, view),
                                  headers={"X-Request-Id": uuid4().hex})
    assert response.status_code == 403, response.text


@pytest.mark.parametrize("view", ["ingest", "statistics", "execution", "tools"])
def test_png_export_is_no_longer_supported(env, view):
    body = command(env, view)
    body["input"].update(format="png", contents=[view + "_png"])
    response = env["client"].post("/api/v1/tasks/execute", json=body,
                                  headers={"X-Request-Id": uuid4().hex})
    assert response.status_code == 422, response.text
    assert response.json()["message"] == "导出类型或格式不支持"
