"""Unified deletion HTTP contract against isolated assets and databases."""
import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from test_agent_traces import env, ROOT
from test_agent_trace_workflow import USER, upload
from app.core.auth import create_token
from app.core.database import Base
from app.models.agent_traces import (
    TraceArtifact, TraceAsset, TraceBatch, TraceBinding, TraceCall, TraceEvent, TraceEvidence,
    TraceProjection, TraceRelation, TraceSample, TraceSnapshot, TraceStatisticsSample,
)
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


def test_confirmed_batch_delete_removes_all_audit_snapshots(pending, tmp_path):
    sessions = pending["sessions"]
    record = {**EXAMPLE, "audit": {"decision": "risk_found", "risk_level": "high",
                                  "evidence": [{"event_idx": 2, "risk_type": "other"}]}}
    bid, fid = upload(pending, tmp_path, [record])
    from app.services.agent_trace_export import directory
    name = "art_test.json"
    artifact = directory() / name
    artifact.write_text("{}", encoding="utf-8")
    with sessions() as db:
        batch = db.get(TraceBatch, bid)
        version = batch.payload["batch_version"]
        old_snapshot = batch.active_snapshot_id
        task, _ = ingest.submit(db, {"batch_id": bid, "expected_version": version, "mode": "reparse_all"},
                                USER, "reparse", "reparse")
    ingest.run(task["task_id"], sessions)
    with sessions() as db:
        snapshot = db.get(TraceBatch, bid).active_snapshot_id
        assert snapshot != old_snapshot
        db.add(Task(task_id="batch-export", capability_code="agent_trace_export", name="导出", status="succeeded",
                    input_data={"batch_id": bid}, config={"owner_id": "1"}, result={}))
        db.add(TraceArtifact(artifact_id="art_test", task_id="batch-export", owner_id="1", batch_id=bid,
                            snapshot_id=snapshot, storage_name=name, filename=name, mime_type="application/json",
                            sha256="0" * 64, size_bytes=2, expires_at=ingest.now_shanghai()))
        db.commit()

    body = {"expected_version": version, "file_id": None, "expected_snapshot_id": snapshot}
    assert remove(pending, bid=bid, body={**body, "expected_snapshot_id": old_snapshot}).status_code == 409
    response = remove(pending, bid=bid, body=body, key="confirmed-batch")
    assert response.status_code == 200, response.text
    assert remove(pending, bid=bid, body=body, key="confirmed-batch").json()["data"] == response.json()["data"]
    assert pending["client"].post("/api/v1/tasks/execute", headers={"X-Request-Id": "late-export"}, json={
        "capability_code": "agent_trace_export", "input": {"batch_id": bid, "snapshot_id": snapshot, "view": "ingest"},
    }).status_code == 404
    assert not artifact.exists()
    with sessions() as db:
        assert db.get(TraceBatch, bid) is None
        assert db.get(TraceAsset, fid) is None
        assert db.get(TraceArtifact, "art_test") is None
        assert db.get(Task, "batch-export") is None
        assert db.get(Task, task["task_id"]) is None
        for model in (TraceSnapshot, TraceProjection, TraceSample, TraceEvent, TraceCall, TraceEvidence,
                      TraceRelation, TraceStatisticsSample):
            assert db.scalar(select(model).where(model.snapshot_id.in_([old_snapshot, snapshot]))) is None
        assert db.get(TraceBatch, pending["bid"]) is not None


def test_running_export_blocks_confirmed_batch_delete(pending):
    with pending["sessions"]() as db:
        db.add(Task(task_id="active-export", capability_code="agent_trace_export", name="export", status="running",
                    input_data={"batch_id": pending["bid"]}, config={}, result={}))
        db.commit()
    response = remove(pending, body={"expected_version": 2, "file_id": None, "expected_snapshot_id": None})
    assert response.status_code == 409
    assert pending["path"].exists()


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


@pytest.mark.parametrize(("reuse_key", "shared"), [(False, False), (True, False), (True, True)])
def test_same_file_can_bind_to_new_batch_after_deletion(tmp_path, monkeypatch, reuse_key, shared):
    from app.routers import files

    monkeypatch.setattr(files, "UPLOAD_DIR", tmp_path)
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'reupload.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    content = b'{"messages":[]}'
    digest = hashlib.sha256(content).hexdigest()
    filename = "same.jsonl"

    def register(db, file_id, key):
        path = tmp_path / f"{file_id}.jsonl"
        path.write_bytes(content)
        result = ingest.register_asset(db, USER, file_id, filename, path, len(content), digest, key)
        assert path.exists() == (result["file_id"] == file_id)
        return result["file_id"]

    def bind(db, batch, file_id, key):
        return ingest.bind(db, USER, batch["batch_id"],
                           {"expected_version": 1, "files": [{"file_id": file_id}]}, key)

    try:
        with sessions() as db:
            old = ingest.create_batch(db, USER, "旧批次", "create-old")
            old_id = register(db, "first-file", "upload-old")
            assert register(db, "lost-response-retry", "upload-old") == old_id
            bind(db, old, old_id, "bind-old")
            if shared:
                other = ingest.create_batch(db, USER, "共享批次", "create-shared")
                bind(db, other, old_id, "bind-shared")
            ingest.delete_item(db, USER, old["batch_id"],
                {"expected_version": 2, "file_id": None, "expected_snapshot_id": None}, "delete-old")
            assert (db.get(TraceAsset, old_id) is not None) == shared

            new = ingest.create_batch(db, USER, "新批次", "create-new")
            key = "upload-old" if reuse_key else "upload-new"
            new_id = register(db, "new-file", key)
            assert new_id == (old_id if shared else "new-file")
            assert register(db, "second-retry", key) == new_id
            assert bind(db, new, new_id, "bind-new")["affected_files"] == [new_id]
            bound = ingest.bindings(db, new["batch_id"])
            assert len(bound) == 1 and bound[0].payload["parse_status"] == "pending"
            assert [asset.file_id for asset in db.scalars(select(TraceAsset))] == [new_id]
    finally:
        engine.dispose()
