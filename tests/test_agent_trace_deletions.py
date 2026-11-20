"""Unified deletion HTTP contract against isolated assets and databases."""
import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from test_agent_traces import env, ROOT
from test_agent_trace_workflow import USER, upload
from app.core.auth import create_token
from app.core.database import Base
from app.models.agent_traces import TraceAsset, TraceBatch, TraceBinding, TraceSample, TraceSnapshot
from app.models.tables import Task
from app.services import agent_trace_ingest as ingest
from app.services.agent_trace_trajectory import EXAMPLE


@pytest.fixture
def pending(env, tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_TRACE_ENABLE_LEGACY_VALUE_SYNC", raising=False)
    from app.routers import files
    monkeypatch.setattr(files, "UPLOAD_DIR", tmp_path)
    path = tmp_path / "pending.jsonl"
    path.write_text("invalid", encoding="utf-8")
    fid = "file_" + uuid4().hex
    with env["sessions"]() as db:
        batch = ingest.create_batch(db, USER, "待删除", uuid4().hex)
        ingest.register_asset(db, USER, fid, path.name, path, path.stat().st_size,
                              hashlib.sha256(path.read_bytes()).hexdigest(), uuid4().hex)
        ingest.bind(db, USER, batch["batch_id"], {"expected_version": 1, "files": [{"file_id": fid}]}, uuid4().hex)
    return {**env, "bid": batch["batch_id"], "fid": fid, "path": path}


def remove(pending, *, bid=None, body=None, key="delete-test", headers=None):
    return pending["client"].post(f"{ROOT}/batches/{bid or pending['bid']}/deletions",
        json=body if body is not None else {"expected_version": 2, "file_id": pending["fid"]},
        headers={"X-Request-Id": key, **(headers or {})})


def test_delete_file_then_empty_batch_and_replay(pending):
    response = remove(pending)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["deleted"] is True and data["asset_cleanup"] == "deleted"
    assert data["batch"]["batch_version"] == 3
    assert not pending["path"].exists()
    assert remove(pending).json()["data"] == data
    with pending["sessions"]() as db:
        assert not ingest.bindings(db, pending["bid"])
        assert db.get(TraceAsset, pending["fid"]) is None
    body = {"expected_version": 3, "file_id": None}
    response = remove(pending, body=body, key="empty")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["batch"] is None
    assert remove(pending, body=body, key="empty").json()["data"] == response.json()["data"]
    assert remove(pending, body=body, key="new-key").status_code == 404


@pytest.mark.parametrize("body,status", [
    ({"expected_version": 1, "file_id": "target"}, 409),
    ({"expected_version": 2, "file_id": None}, 409),
    ({"expected_version": True, "file_id": None}, 422),
    ({"expected_version": 0, "file_id": None}, 422),
    ({"expected_version": "2", "file_id": None}, 422),
    ({"expected_version": 2}, 422),
    ({"expected_version": 2, "file_id": ""}, 422),
    ({"expected_version": 2, "file_id": []}, 422),
    ({"expected_version": 2, "file_id": "missing"}, 404),
])
def test_invalid_requests_are_atomic(pending, body, status):
    if body.get("file_id") == "target":
        body = {**body, "file_id": pending["fid"]}
    response = remove(pending, body=body)
    assert response.status_code == status, response.text
    assert response.json()["code"] == status
    assert pending["path"].exists()
    with pending["sessions"]() as db:
        assert db.get(TraceBatch, pending["bid"]).payload["batch_version"] == 2
        assert len(ingest.bindings(db, pending["bid"])) == 1


@pytest.mark.parametrize("status,count,allowed", [
    ("failed", 0, True), ("pending", 0, True), ("failed", 1, False),
    ("partial", 1, False), ("succeeded", 0, False), ("running", 0, False),
])
def test_only_zero_success_pending_or_failed_files(pending, status, count, allowed):
    with pending["sessions"]() as db:
        row = ingest.bindings(db, pending["bid"])[0]
        row.payload = {**row.payload, "parse_status": status, "parsed_trace_count": count}
        db.commit()
    assert remove(pending).status_code == (200 if allowed else 409)


@pytest.mark.parametrize("status", ["pending", "running"])
def test_active_parse_blocks_deletion(pending, status):
    with pending["sessions"]() as db:
        db.add(Task(task_id="active-parse", capability_code="agent_trace_ingest", name="parse", status=status,
                    input_data={"batch_id": pending["bid"]}, config={}, result={}))
        db.commit()
    assert remove(pending).status_code == 409
    assert pending["path"].exists()


def test_permissions_missing_key_and_idempotency_conflict(pending):
    assert remove(pending, key="").status_code == 422
    for uid, role in ((2, "管理员"), (3, "审计员")):
        assert remove(pending, headers={"Authorization": "Bearer " + create_token(uid, f"user-{uid}", role)}).status_code == 403
    assert remove(pending).status_code == 200
    assert remove(pending, body={"expected_version": 3, "file_id": None}).status_code == 409
    assert remove(pending, headers={"Authorization": "Bearer " + create_token(3, "user-3", "审计员")}).status_code == 403


@pytest.mark.parametrize("reference", ["binding", "task", "snapshot"])
def test_referenced_assets_are_retained(pending, reference):
    with pending["sessions"]() as db:
        if reference == "binding":
            b = ingest.create_batch(db, USER, "共享文件", uuid4().hex)
            ingest.bind(db, USER, b["batch_id"], {"expected_version": 1, "files": [{"file_id": pending["fid"]}]}, uuid4().hex)
        elif reference == "task":
            db.add(Task(task_id="history", capability_code="data_ingest", name="history", status="failed",
                        input_data={"files": [pending["fid"]]}, config={}, result={}))
        else:
            db.add(TraceSnapshot(snapshot_id="history", batch_id="another", payload={}))
            db.add(TraceSample(id="history-row", snapshot_id="history", trace_id="history-trace", sample_id="s",
                               display_id="s", ingest_order=0, language="zh", payload={"source": {"source_id": pending["fid"]}}, result={}))
        db.commit()
    data = remove(pending).json()["data"]
    assert data["asset_cleanup"] == "retained" and data["deleted"] is True
    assert pending["path"].exists()
    with pending["sessions"]() as db:
        assert db.get(TraceAsset, pending["fid"]) is not None


def test_cleanup_failure_retries_on_replay(pending, monkeypatch):
    original = Path.unlink
    def fail_unlink(path, *args, **kwargs):
        if path == pending["path"]:
            raise PermissionError("temporarily locked")
        return original(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_unlink)
        response = remove(pending)
        assert response.status_code == 200, response.text
        assert response.json()["data"]["asset_cleanup"] == "pending"
        assert pending["path"].exists()
    assert remove(pending).json()["data"]["asset_cleanup"] == "deleted"
    assert not pending["path"].exists()


def test_cleanup_refuses_path_outside_upload_directory(pending, monkeypatch, tmp_path):
    from app.routers import files
    monkeypatch.setattr(files, "UPLOAD_DIR", tmp_path / "different-root")
    assert remove(pending).json()["data"]["asset_cleanup"] == "pending"
    assert pending["path"].exists()


def test_cannot_delete_historically_nonempty_batch(pending, tmp_path):
    bid, _ = upload(pending, tmp_path, [EXAMPLE])
    with pending["sessions"]() as db:
        for row in ingest.bindings(db, bid):
            db.delete(row)
        version = db.get(TraceBatch, bid).payload["batch_version"]
        db.commit()
    response = remove(pending, bid=bid, body={"expected_version": version, "file_id": None})
    assert response.status_code == 409, response.text


def test_two_stale_deletes_only_apply_once(pending):
    assert remove(pending).status_code == 200
    assert remove(pending, key="second-writer").status_code == 409
    with pending["sessions"]() as db:
        assert db.get(TraceBatch, pending["bid"]).payload["batch_version"] == 3


@pytest.mark.parametrize("other_operation", ["delete", "parse", "bind"])
def test_concurrent_mutations_serialize_on_batch(tmp_path, monkeypatch, other_operation):
    from app.routers import files
    monkeypatch.setattr(files, "UPLOAD_DIR", tmp_path)
    engine = create_engine("sqlite+pysqlite:///" + (tmp_path / "concurrent.db").as_posix(),
                           connect_args={"check_same_thread": False, "timeout": 10})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    path = tmp_path / "concurrent.jsonl"
    path.write_text("{}", encoding="utf-8")
    with sessions() as db:
        batch = ingest.create_batch(db, USER, "并发", "create")
        ingest.register_asset(db, USER, "file_concurrent", path.name, path, 2,
                              hashlib.sha256(path.read_bytes()).hexdigest(), "upload")
        bid = batch["batch_id"]
        ingest.bind(db, USER, bid, {"expected_version": 1, "files": [{"file_id": "file_concurrent"}]}, "bind")
    barrier = Barrier(2)
    def mutate(operation):
        with sessions() as db:
            barrier.wait(timeout=10)
            try:
                if operation == "parse":
                    ingest.submit(db, {"batch_id": bid, "expected_version": 2}, USER, "parse", "parse")
                elif operation == "bind":
                    ingest.bind(db, USER, bid, {"expected_version": 2,
                        "files": [{"file_id": "file_concurrent", "format_override": "openai_messages"}]}, "rebind")
                else:
                    ingest.delete_item(db, USER, bid, {"expected_version": 2, "file_id": "file_concurrent"}, operation)
                return 200
            except HTTPException as exc:
                return exc.status_code
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(mutate, "first-delete")
            second = pool.submit(mutate, other_operation)
            outcomes = sorted([first.result(timeout=15), second.result(timeout=15)])
        assert outcomes[0] == 200 and outcomes[1] in {404, 409}
        with sessions() as db:
            batch = db.get(TraceBatch, bid)
            assert batch.payload["batch_version"] in {2, 3}
            rows = ingest.bindings(db, bid)
            if rows:
                assert db.get(TraceAsset, rows[0].file_id) is not None
                assert path.exists()
    finally:
        engine.dispose()
