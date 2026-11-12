"""Real HTTP contracts over an isolated DB; no model calls or production DB writes."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import io
import json
import time
import zipfile

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.auth import create_token
from app.core.database import Base, get_db
from app.core.time import now_shanghai
from app.main import app
from app.models.tables import SystemUser, Task
from app.models.agent_traces import (
    TraceBatch, TraceSample, TraceEvent, TraceCall, TraceProjection,
    TraceSnapshot, TraceArtifact, TraceIngestCheckpoint,
)
from app.routers import tasks
from app.services import agent_trace_sync as sync, agent_trace_query as query, agent_trace_export as exports
from app.services.agent_trace_generation import generated_sample, stable_id, VERSION

ROOT = "/api/v1/compliance/agent-traces"


@pytest.fixture
def env(monkeypatch, tmp_path):
    # These legacy regressions explicitly opt into historical simulated value fixtures.
    monkeypatch.setenv("AGENT_TRACE_ENABLE_LEGACY_VALUE_SYNC", "true")
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setenv("AUTH_SECRET", "agent-trace-isolated-test-secret")
    monkeypatch.setenv("AGENT_TRACE_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    monkeypatch.delenv("AGENT_TRACE_LLM_DATABASE_URL", raising=False)
    monkeypatch.delenv("LLM_ACCESS_TOKEN", raising=False)
    def db_override():
        with sessions() as db:
            yield db
    app.dependency_overrides[get_db] = db_override
    monkeypatch.setattr(tasks, "SessionLocal", sessions)
    original = exports.run_export
    monkeypatch.setattr(exports, "run_export", lambda tid, *args: original(tid, sessions))
    with sessions() as db:
        for uid, role in ((1, "管理员"), (2, "管理员"), (3, "审计员")):
            db.add(SystemUser(id=uid, username=f"user-{uid}", display_name="测试", email=f"user{uid}@example.com",
                              role=role, status="active"))
        source = {"system": "business", "task_id": "value-001", "result_id": "result-001", "scope": {"dataset_id": 1, "version_id": "dsv-1"}}
        samples = [{"id": str(i), "text": f"样本 {i} 联系邮箱 person@example.com", "language": "zh" if i % 2 else "en",
                    "score": 90 - i, "tier": "high", "dimensions": [{"name": "质量", "score": 88, "reason": "实际评分"}]} for i in range(1, 9)]
        bid = sync.ingest(db, source, samples)
        db.commit()
        batch, scope = query.resolve(db, bid, {})
        rows = list(db.scalars(select(TraceSample).order_by(TraceSample.ingest_order)))
        trace = rows[0].trace_id
    client = TestClient(app)
    token = create_token(1, "user-1", "管理员")
    client.headers["Authorization"] = "Bearer " + token
    yield {"sessions": sessions, "client": client, "batch": bid, "scope": scope,
           "trace": trace, "rows": rows, "source": source, "samples": samples, "tmp": tmp_path}
    client.close()
    app.dependency_overrides.clear()
    engine.dispose()


def detail(env, **params):
    return env["client"].get(f"{ROOT}/batches/{env['batch']}/samples/{env['trace']}",
        params={"view": "tools", **env["scope"], **params})


def command(env, **changes):
    data = {**env["scope"], "view": "tools", "export_kind": "package", "format": "zip",
            "selected_trace_id": env["trace"], "scope": {"type": "sample", "trace_ids": [env["trace"]]},
            "contents": ["normalized_json", "evidence_index", "manifest"],
            "redaction_profile": "standard", "anonymize_ids": True, **changes}
    return {"capability_code": "agent_trace_export", "name": "工具交互导出", "input": data}


def test_initial_workspace_and_search(env):
    client = env["client"]
    response = client.get(ROOT + "/workspace", params={"view": "tools", "include": "options"})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["batch_options"]["items"][0]["batch_id"] == env["batch"]
    assert data["candidate_scope"] is None
    data = client.get(ROOT + "/workspace", params={"view": "tools", "batch_id": env["batch"]}).json()["data"]
    assert data["candidate_scope"] == env["scope"]
    assert data["sample_options"]["total"] == 8
    data = client.get(f"{ROOT}/batches/{env['batch']}/samples", params={"view": "tools", **env["scope"], "q": "AUD-0008"}).json()["data"]
    assert data["total"] == 1 and data["items"][0]["sample_id"] == "8"


def test_overview_is_coherent_and_not_mutating(env):
    with env["sessions"]() as db:
        before = db.scalar(select(func.count()).select_from(TraceEvent))
    response = detail(env)
    assert response.status_code == 200
    d = response.json()["data"]
    assert d["metrics"] == {"call_count": 7, "tool_kind_count": 4, "success_count": 5,
        "error_count": 1, "not_executed_count": 1, "unknown_status_count": 0, "overreach_count": 1, "inefficient_count": 2}
    assert d["timing"]["tool_duration_sum_ms"] == 5000
    assert d["timing"]["tool_wall_time_ms"] == 5000
    assert d["timing"]["other_duration_ms"] == 3600
    assert d["selection"]["display_id"] == "CALL-05"
    assert d["selection"]["duration_ms"] is None
    assert d["summary"]["event_count"] == 18
    assert "含模拟" in d["summary"]["source"]["format_label"]
    for call in d["sequence"]["items"]:
        assert call["analysis"]["permission_comparison"]["rows"][0]["granted"] == ["public.read"]
    assert detail(env).json()["data"] == d
    with env["sessions"]() as db:
        assert db.scalar(select(func.count()).select_from(TraceEvent)) == before
        assert db.scalar(select(TraceSample).where(TraceSample.trace_id == env["trace"])).result["score"] == 89
        events = query.process_records(db, env["scope"], env["trace"], TraceEvent)
        event_lookup = {e["event_id"]: e for e in events}
        for c in d["calls"]["items"]:
            assert event_lookup[c["event_id"]]["relative_time_ms"] == c["relative_start_ms"]


def test_calls_selection_raw_and_paging(env):
    d = detail(env).json()["data"]
    cid = d["calls"]["items"][0]["call_id"]
    selection = detail(env, section="selection", call_id=cid, include_raw="true").json()["data"]["selection"]
    assert len(selection["raw_events"]["items"]) == 2
    data = detail(env, section="calls", call_page=2, call_page_size=3).json()["data"]
    assert data["calls"]["total"] == 7 and len(data["calls"]["items"]) == 3
    assert detail(env, section="selection").status_code == 422
    assert detail(env, section="selection", call_id="other").status_code == 404
    assert detail(env, section="bogus").status_code == 422
    assert detail(env, call_page=0).status_code == 422


def test_long_sequence_no_duplicates_and_cursor_is_bound(env):
    with env["sessions"]() as db:
        first = db.scalar(select(TraceCall).where(TraceCall.trace_id == env["trace"]).order_by(TraceCall.ordinal))
        for i in range(7, 215):
            c = {**deepcopy(first.payload), "call_id": f"extra-{i}", "order": i + 1}
            db.add(TraceCall(id=f"extra-row-{i}", snapshot_id=env["scope"]["snapshot_id"], trace_id=env["trace"],
                            call_id=c["call_id"], ordinal=i + 1, payload=c))
        db.commit()
    d = detail(env).json()["data"]
    assert d["metrics"]["call_count"] == 215
    assert len(d["sequence"]["items"]) == 200
    cursor = d["sequence"]["next_cursor"]
    more = detail(env, sequence_cursor=cursor).json()["data"]["sequence"]
    assert len(more["items"]) == 15 and more["complete"] and more["next_cursor"] is None
    assert not {c["call_id"] for c in d["sequence"]["items"]} & {c["call_id"] for c in more["items"]}
    assert detail(env, section="sequence", sequence_cursor=cursor).json()["data"]["sequence"] == more
    other = env["rows"][1].trace_id
    assert env["client"].get(f"{ROOT}/batches/{env['batch']}/samples/{other}",
        params={"view": "tools", **env["scope"], "sequence_cursor": cursor}).status_code == 409


def test_execution_exact_nonfirst_page_and_event_pages(env):
    target = env["rows"][-1].trace_id
    d = env["client"].get(ROOT + "/workspace", params={"view": "execution", "include": "dashboard",
        **env["scope"], "trace_id": target}).json()["data"]
    assert d["selected_trace_id"] == target and d["sample_page"]["page"] == 2
    selected = detail(env).json()["data"]["selection"]
    event_id = selected["event_id"]
    url = f"{ROOT}/batches/{env['batch']}/samples/{env['trace']}"
    r = env["client"].get(url, params={"view": "execution", **env["scope"], "section": "selection",
                                     "event_id": event_id, "event_page_size": 5})
    d = r.json()["data"]
    assert d["selection"]["event_id"] == event_id and d["events"]["page"] == 3
    assert d["selection"]["previous_event_id"] and d["selection"]["next_event_id"]
    overview = env["client"].get(url, params={"view": "execution", **env["scope"]}).json()["data"]
    assert overview["graph"]["total_nodes"] == 18
    assert len(overview["graph"]["edges"]) == 17
    assert overview["risk_distribution"]["items"][0]["event_count"] == 1


def test_zero_calls_and_empty_snapshot(env):
    with env["sessions"]() as db:
        bid = sync.ingest(db, {"system": "business", "task_id": "empty", "result_id": "empty"}, [])
        bid2 = sync.ingest(db, {"system": "business", "task_id": "zero", "result_id": "zero"},
            [{"id": "0", "duration_ms": 0, "score": 0, "execution": {"events": [], "calls": [], "duration_ms": 0}}])
        db.commit()
        _, scope = query.resolve(db, bid2, {})
        sample = db.scalar(select(TraceSample).where(TraceSample.snapshot_id == scope["snapshot_id"]))
        d = query.tool_detail(db, db.get(TraceBatch, bid2), scope, sample.trace_id, {}, {"uid": 1})
        assert d["metrics"]["call_count"] == 0 and d["selection"] is None
        assert d["calls"]["items"] == [] and d["timing"]["total_duration_ms"] == 0
        assert sample.result["score"] == 0
    r = env["client"].get(ROOT + "/workspace", params={"view": "tools", "batch_id": bid})
    assert r.json()["data"]["sample_options"]["total"] == 0


def test_scope_validation_and_old_projection(env):
    assert detail(env, projection_version="reclaimed").status_code == 410
    assert detail(env, snapshot_id="missing").status_code == 410
    with env["sessions"]() as db:
        old = "old-snapshot"
        db.add(TraceSnapshot(snapshot_id=old, batch_id=env["batch"], payload={}))
        db.add(TraceProjection(projection_id="old-proj", snapshot_id=old, version="old", payload={}))
        db.commit()
        _, scope = query.resolve(db, env["batch"], {"snapshot_id": old, "projection_version": "old"})
        assert scope["snapshot_id"] == old
    assert detail(env, snapshot_id=old, projection_version="old").status_code == 404


def test_roles_and_raw_redaction(env):
    client = env["client"]
    assert client.get(ROOT + "/workspace", params={"view": "tools"}, headers={"Authorization": ""}).status_code == 401
    client.headers["Authorization"] = "Bearer " + create_token(3, "user-3", "审计员")
    overview = detail(env).json()["data"]
    assert overview["permissions"]["can_export"] is False
    assert detail(env, include_raw="true").status_code == 403
    r = client.get(f"{ROOT}/batches/{env['batch']}/samples/{env['trace']}", params={"view": "execution", **env["scope"]})
    assert "person@example.com" not in r.text
    assert "邮箱已脱敏" in r.text
    assert client.post("/api/v1/tasks/execute", json=command(env)).status_code == 403


def test_local_sync_is_idempotent_and_diagnoses_bad_sources(env):
    with env["sessions"]() as db:
        for tid, result in (("stored", {"id": "stored-result", "samples": env["samples"]}), ("summary-only", {"mean_score": 85})):
            db.add(Task(task_id=tid, name="价值分析", capability_code="value_score", status="succeeded", input_data={}, config={}, result=result))
        db.commit()
    worker = sync.TraceSynchronizer(env["sessions"])
    worker.sync_local()
    worker.sync_local()
    with env["sessions"]() as db:
        assert db.scalar(select(func.count()).select_from(TraceBatch)) == 2
        assert db.get(TraceIngestCheckpoint, "business:summary-only").status == "failed"
        assert db.scalar(select(func.count()).select_from(TraceSample)) == 16
    worker.tick()
    with env["sessions"]() as db:
        assert db.get(TraceIngestCheckpoint, "system:llm").status == "unavailable"


def test_atomic_import_rollback(env):
    bad = deepcopy(env["samples"])
    bad[1]["execution"] = {"events": [], "calls": [{"call_id": "bad", "event_id": "missing"}]}
    worker = sync.TraceSynchronizer(env["sessions"])
    worker.publish({"system": "business", "task_id": "bad", "result_id": "bad"}, bad)
    with env["sessions"]() as db:
        assert db.get(TraceBatch, stable_id("batch_", "business:bad")) is None
        assert db.get(TraceIngestCheckpoint, "business:bad").status == "failed"


def test_llm_import_token_isolation_and_revocation(env, monkeypatch):
    monkeypatch.setenv("LLM_ACCESS_TOKEN", "test-llm-owner")
    owner = sync.llm_owner()
    with env["sessions"]() as db:
        for sql in (
            "CREATE TABLE llm_auth_tokens(token_hash TEXT,enabled INTEGER,expires_at TEXT,deleted_at TEXT)",
            "CREATE TABLE llm_governance_tasks(id TEXT,kind TEXT,scope_json TEXT,model_version TEXT,status TEXT,token_hash TEXT)",
            "CREATE TABLE llm_governance_results(id TEXT,task_id TEXT,kind TEXT,token_hash TEXT,summary_json TEXT,samples_json TEXT,created_at TEXT)",
            "CREATE TABLE llm_governance_task_samples(task_id TEXT,sample_id TEXT,status TEXT,output_json TEXT,error_code TEXT)"):
            db.execute(text(sql))
        db.execute(text("INSERT INTO llm_auth_tokens VALUES(:owner,1,'2099-01-01T00:00:00+00:00',NULL)"), {"owner": owner})
        for tid, token in (("mine", owner), ("other", "other-token")):
            db.execute(text("INSERT INTO llm_governance_tasks VALUES(:id,'governance-value','{}','v1','succeeded',:token)"), {"id": tid, "token": token})
            db.execute(text("INSERT INTO llm_governance_results VALUES(:id,:id,'governance-value',:token,'{}',:samples,'2026-01-01')"),
                {"id": tid, "token": token, "samples": json.dumps(env["samples"])})
        db.commit()
    worker = sync.TraceSynchronizer(env["sessions"])
    worker.sync_llm()
    with env["sessions"]() as db:
        batches = list(db.scalars(select(TraceBatch).where(TraceBatch.source_system == "llm")))
        assert len(batches) == 1 and batches[0].source_task_id == "mine"
        bid = batches[0].batch_id
        db.execute(text("UPDATE llm_auth_tokens SET enabled=0"))
        db.commit()
    r = env["client"].get(ROOT + "/workspace", params={"view": "tools", "batch_id": bid})
    assert r.status_code == 403


def test_timing_parallel_and_incomplete():
    calls = [{"execution_status": "succeeded", "duration_ms": 200, "relative_start_ms": 0, "relative_end_ms": 200},
             {"execution_status": "succeeded", "duration_ms": 200, "relative_start_ms": 100, "relative_end_ms": 300}]
    t = query.timing({"duration_ms": 400}, calls)
    assert t["tool_duration_sum_ms"] == 400 and t["tool_wall_time_ms"] == 300 and t["other_duration_ms"] == 100
    assert t["has_overlap"]
    calls[1]["relative_end_ms"] = None
    assert query.timing({"duration_ms": 400}, calls)["other_duration_ms"] is None


@pytest.mark.parametrize("kind,format,contents", [("view", "png", ["tools_png"]), ("package", "zip", ["normalized_json", "evidence_index", "manifest"])])
def test_real_export_ticket_file_and_idempotency(env, kind, format, contents):
    client = env["client"]
    payload = command(env, export_kind=kind, format=format, contents=contents)
    headers = {"X-Request-Id": "export-001"}
    r = client.post("/api/v1/tasks/execute", json=payload, headers=headers)
    assert r.status_code == 200, r.text
    tid = r.json()["data"]["task_id"]
    task = client.get("/api/v1/tasks/" + tid).json()["data"]
    assert task["status"] == "succeeded", task
    artifact = task["result"]["artifacts"][0]
    r2 = client.post("/api/v1/tasks/execute", json=payload, headers=headers)
    assert r2.json()["data"]["task_id"] == tid
    changed = deepcopy(payload)
    changed["name"] = "different"
    assert client.post("/api/v1/tasks/execute", json=changed, headers=headers).status_code == 409
    ticket_url = f"{ROOT}/artifacts/{artifact['artifact_id']}/download-tickets"
    ticket = client.post(ticket_url, json={}, headers={"X-Request-Id": "ticket-001"}).json()["data"]
    assert client.post(ticket_url, json={}, headers={"X-Request-Id": "ticket-001"}).json()["data"] == ticket
    file = client.get(ticket["download_url"], headers={"Authorization": ""})
    assert file.status_code == 200
    assert hashlib.sha256(file.content).hexdigest() == artifact["sha256"]
    if format == "png":
        image = Image.open(io.BytesIO(file.content))
        assert image.format == "PNG" and image.width == 1500
    else:
        with zipfile.ZipFile(io.BytesIO(file.content)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            assert "模拟" in manifest["notice"] and manifest["anonymize_ids"]
            for item in manifest["files"]:
                assert hashlib.sha256(archive.read(item["path"])).hexdigest() == item["sha256"]
            normalized = archive.read("normalized.json").decode()
            assert env["trace"] not in normalized and "person@example.com" not in normalized
            assert "CALL-05" not in normalized
    client.headers["Authorization"] = "Bearer " + create_token(2, "user-2", "管理员")
    assert client.get("/api/v1/tasks/" + tid).status_code == 403
    assert client.post(ticket_url, json={}, headers={"X-Request-Id": "other"}).status_code == 403
    with env["sessions"]() as db:
        user = db.get(SystemUser, 1)
        user.status = "disabled"
        db.commit()
    assert client.get(ticket["download_url"], headers={"Authorization": ""}).status_code == 403


def test_export_validation_and_expired_ticket(env):
    client = env["client"]
    for changes in ({"format": "pdf"}, {"contents": ["raw_files"]}, {"anonymize_ids": False}, {"view": "unsupported"}):
        assert client.post("/api/v1/tasks/execute", json=command(env, **changes), headers={"X-Request-Id": "bad"}).status_code == 422
    assert client.post("/api/v1/tasks/execute", json=command(env)).status_code == 422
    assert client.get(ROOT + "/downloads", params={"t": "bad"}, headers={"Authorization": ""}).status_code == 422
    token = query.sign({"kind": "download", "actor": "1", "artifact": "x", "expires": time.time() - 1})
    assert client.get(ROOT + "/downloads", params={"t": token}, headers={"Authorization": ""}).status_code == 410


def test_failed_font_export_and_expired_artifact(env, monkeypatch):
    def no_font(document):
        raise ValueError("测试字体不可用")
    monkeypatch.setattr(exports, "render_png", no_font)
    client = env["client"]
    tid = client.post("/api/v1/tasks/execute", json=command(env, export_kind="view", format="png", contents=["tools_png"]),
        headers={"X-Request-Id": "font"}).json()["data"]["task_id"]
    task = client.get("/api/v1/tasks/" + tid).json()["data"]
    assert task["status"] == "failed" and not task["result"]["artifacts"]
    assert "字体" in task["error_message"]


def test_content_bounds_unicode_and_original_json_keys(env):
    text = "😀" * 40000
    with env["sessions"]() as db:
        call = db.scalar(select(TraceCall).where(TraceCall.trace_id == env["trace"]).order_by(TraceCall.ordinal))
        cid = call.call_id
        call.payload = {**call.payload, "input_json": json.dumps({"User_ID": "original_case", "tool_call_id": "source", "long": text}, ensure_ascii=False)}
        event = db.scalar(select(TraceEvent).where(TraceEvent.trace_id == env["trace"]).order_by(TraceEvent.ordinal))
        event.payload = {**event.payload, "source_excerpt": text}
        eid = event.event_id
        db.commit()
    d = detail(env).json()["data"]
    row = next(c for c in d["calls"]["items"] if c["call_id"] == cid)
    assert "User_ID" in row["input_json"] and "tool_call_id" in row["input_json"]
    assert len(row["input_json"].encode("utf-16-le")) <= 8192
    assert "input_json" in row["truncated_fields"]
    d = detail(env, section="selection", call_id=cid).json()["data"]["selection"]
    assert len(d["input_json"].encode("utf-16-le")) <= 131072
    d = env["client"].get(f"{ROOT}/batches/{env['batch']}/samples/{env['trace']}", params={
        "view": "execution", **env["scope"], "section": "selection", "event_id": eid}).json()["data"]
    assert d["selection"]["content"]["source_excerpt"]["truncated"]


def test_expired_artifact_corruption_and_revoked_role(env):
    client = env["client"]
    tid = client.post("/api/v1/tasks/execute", json=command(env), headers={"X-Request-Id": "corruption"}).json()["data"]["task_id"]
    aid = client.get("/api/v1/tasks/" + tid).json()["data"]["result"]["artifacts"][0]["artifact_id"]
    path = f"{ROOT}/artifacts/{aid}/download-tickets"
    url = client.post(path, json={}, headers={"X-Request-Id": "first"}).json()["data"]["download_url"]
    with env["sessions"]() as db:
        artifact = db.get(TraceArtifact, aid)
        file = exports.directory() / artifact.storage_name
        original = file.read_bytes()
        file.write_bytes(b"corrupt")
    assert client.get(url, headers={"Authorization": ""}).status_code == 409
    file.write_bytes(original)
    with env["sessions"]() as db:
        user = db.get(SystemUser, 1)
        user.role = "审计员"
        db.commit()
    assert client.get(url, headers={"Authorization": ""}).status_code == 403
    with env["sessions"]() as db:
        db.get(SystemUser, 1).role = "管理员"
        db.get(TraceArtifact, aid).expires_at = now_shanghai() - timedelta(seconds=1)
        db.commit()
    assert client.post(path, json={}, headers={"X-Request-Id": "new"}).status_code == 410
    assert client.get(url, headers={"Authorization": ""}).status_code == 410


def test_source_defaults_do_not_overwrite_real_zero_and_scaled_time():
    trace, payload, events, calls, _ = generated_sample("zero-time", {"id": "1", "duration_ms": 0, "token_usage": {"input_tokens": 0}}, 0)
    assert payload["duration_ms"] == 0 and payload["token_usage"]["input_tokens"] == 0
    assert all(e["relative_time_ms"] == 0 for e in events)
    assert all(c["duration_ms"] in (None, 0) for c in calls)


def test_canonical_idempotency_payload_key_order(env):
    client = env["client"]
    payload = command(env)
    first = client.post("/api/v1/tasks/execute", json=payload, headers={"X-Request-Id": "reorder"}).json()["data"]
    payload["input"] = dict(reversed(list(payload["input"].items())))
    second = client.post("/api/v1/tasks/execute", json=payload, headers={"X-Request-Id": "reorder"}).json()["data"]
    assert first["task_id"] == second["task_id"]
    for changes in ({"scope": "bad"}, {"scope": {"type": "sample", "trace_ids": [{}]}}, {"selected_call_id": []}):
        assert client.post("/api/v1/tasks/execute", json=command(env, **changes), headers={"X-Request-Id": "malformed"}).status_code == 422


def test_graph_segments_and_cursor_authorization(env):
    with env["sessions"]() as db:
        first = db.scalar(select(TraceEvent).where(TraceEvent.trace_id == env["trace"]).order_by(TraceEvent.ordinal))
        for i in range(18, 520):
            payload = {**deepcopy(first.payload), "event_id": f"extra-event-{i}", "ordinal": i + 1, "display_id": f"STEP-{i + 1}"}
            db.add(TraceEvent(id=f"extra-event-row-{i}", snapshot_id=env["scope"]["snapshot_id"],
                             trace_id=env["trace"], event_id=payload["event_id"], ordinal=i + 1, payload=payload))
        db.commit()
    url = f"{ROOT}/batches/{env['batch']}/samples/{env['trace']}"
    params = {"view": "execution", **env["scope"], "section": "graph"}
    first = env["client"].get(url, params=params).json()["data"]["graph"]
    assert len(first["nodes"]) == 500 and not first["complete"]
    second = env["client"].get(url, params={**params, "graph_cursor": first["next_cursor"]}).json()["data"]["graph"]
    assert len(second["nodes"]) == 20 and second["complete"]
    assert not {n["event_id"] for n in first["nodes"]} & {n["event_id"] for n in second["nodes"]}
    env["client"].headers["Authorization"] = "Bearer " + create_token(2, "user-2", "管理员")
    assert env["client"].get(url, params={**params, "graph_cursor": first["next_cursor"]}).status_code == 409


def test_restart_pending_export_and_cleanup(env):
    with env["sessions"]() as db:
        task, created = exports.create_task(db, command(env)["input"], {"uid": 1, "tier": "operator"}, "pending")
        tid = task.task_id
        assert created
    worker = sync.TraceSynchronizer(env["sessions"])
    worker.tick()
    with env["sessions"]() as db:
        assert db.get(Task, tid).status == "succeeded"
        artifact = db.scalar(select(TraceArtifact).where(TraceArtifact.task_id == tid))
        aid, path = artifact.artifact_id, exports.directory() / artifact.storage_name
        artifact.expires_at = now_shanghai() - timedelta(seconds=1)
        db.commit()
    worker.tick()
    assert not path.exists()
    with env["sessions"]() as db:
        assert db.get(TraceArtifact, aid) is None


def test_access_logs_hide_download_capability():
    import logging
    from app.core.audit_logging import DownloadTicketFilter
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 1, '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1", "GET", "/api/v1/compliance/agent-traces/downloads?t=sensitive-token", "1.1", 200), None)
    assert DownloadTicketFilter().filter(record)
    assert "sensitive-token" not in record.getMessage()
    assert "[REDACTED]" in record.getMessage()


def test_redaction_preserves_system_identity_but_not_phone_content(env):
    event_id = "ev_13912345678abcdef"
    value = {"event_id": event_id, "source_excerpt": "联系电话 13912345678", "input_json": '{"User_ID":"13912345678"}'}
    visible = query.redact(value, preserve_ids=True)
    assert visible["event_id"] == event_id
    assert "13912345678" not in visible["source_excerpt"]
    assert json.loads(visible["input_json"])["User_ID"] == "[手机号已脱敏]"
    with env["sessions"]() as db:
        event = db.scalar(select(TraceEvent).where(TraceEvent.trace_id == env["trace"], TraceEvent.ordinal == 16))
        event.event_id = event_id
        event.payload = {**event.payload, "event_id": event_id}
        db.commit()
    url = f"{ROOT}/batches/{env['batch']}/samples/{env['trace']}"
    d = env["client"].get(url, params={"view": "execution", **env["scope"], "section": "selection", "event_id": event_id}).json()["data"]
    assert d["selection"]["event_id"] == event_id
    assert any(e["event_id"] == event_id for e in d["events"]["items"])
