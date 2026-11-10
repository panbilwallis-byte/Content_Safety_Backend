"""External-only workflow in an isolated database; no live service writes."""
from copy import deepcopy
import hashlib
import json
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select, func

from test_agent_traces import env
from app.models.agent_traces import TraceBatch, TraceCall, TraceSample, TraceEvent
from app.models.tables import Task
from app.services import agent_trace_ingest as ingest, agent_trace_query as q, agent_trace_statistics as stats
from app.services.agent_trace_trajectory import EXAMPLE
from app.services.agent_trace_sync import TraceSynchronizer
from app.services.agent_trace_internal import record_model_call

USER = {"uid": 1, "tier": "admin", "role": "管理员"}


def upload(env, tmp_path, records):
    path = tmp_path / (uuid4().hex + ".jsonl")
    raw = "\n".join(json.dumps(r, ensure_ascii=False) for r in records).encode()
    path.write_bytes(raw)
    fid = "file_" + uuid4().hex
    with env["sessions"]() as db:
        b = ingest.create_batch(db, USER, "外部轨迹", uuid4().hex)
        ingest.register_asset(db, USER, fid, path.name, path, len(raw), hashlib.sha256(raw).hexdigest(), uuid4().hex)
        bound = ingest.bind(db, USER, b["batch_id"], {"expected_version": 1,
            "files": [{"file_id": fid, "format_override": "auto"}]}, uuid4().hex)
        task, _ = ingest.submit(db, {"batch_id": b["batch_id"], "expected_version": bound["batch"]["batch_version"]}, USER, uuid4().hex, "parse")
        tid = task["task_id"]
    ingest.run(tid, env["sessions"])
    with env["sessions"]() as db:
        t = db.get(Task, tid)
        assert t.status == "succeeded", t.result
    return b["batch_id"], fid


def test_ingest_event_selection_reaches_beyond_first_page(env, monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_TRACE_ENABLE_LEGACY_VALUE_SYNC", raising=False)
    record = deepcopy(EXAMPLE)
    record["events"] = [dict(deepcopy(EXAMPLE["events"][0]), idx=i, step_id=f"step-{i}") for i in range(61)]
    record["tool_events"] = []
    bid, _ = upload(env, tmp_path, [record])
    with env["sessions"]() as db:
        _, scope = q.resolve(db, bid, {})
        sample = db.scalar(select(TraceSample).where(TraceSample.snapshot_id == scope["snapshot_id"]))
        first = ingest.detail(db, scope, sample.trace_id, {}, USER)
        assert len(first["event_options"]) == 61
        last_id = first["event_options"][-1]["event_id"]
        last = ingest.detail(db, scope, sample.trace_id, {"event_id": last_id}, USER)
        assert last["selected_event_page"] == 2
        assert last["events"]["page"] == 2
        assert last["normalized_event"]["event_id"] == last_id
        assert last["source_ref"]["source_path"] == "events[60]"


def test_default_hides_legacy_and_worker_keeps_processing(env, monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_TRACE_ENABLE_LEGACY_VALUE_SYNC", raising=False)
    with env["sessions"]() as db:
        assert q.workspace(db, {}, "tools", USER)["batch_options"]["total"] == 0
        assert ingest.workspace(db, {}, USER)["batch"] is None
        assert stats.workspace(db, {}, USER)["scope"] is None
        with pytest.raises(HTTPException) as exc:
            q.resolve(db, env["batch"], {})
        assert exc.value.status_code == 404
    worker = TraceSynchronizer(env["sessions"])
    monkeypatch.setattr(worker, "sync_local", lambda: pytest.fail("must not generate historical value trajectories"))
    monkeypatch.setattr(worker, "sync_llm", lambda: pytest.fail("must not read legacy model value results"))
    worker.tick()
    bid, _ = upload(env, tmp_path, [EXAMPLE])
    with env["sessions"]() as db:
        w = ingest.workspace(db, {}, USER)
        assert w["batch"]["batch_id"] == bid
        assert w["summary"]["parsed_trace_count"] == 1
        assert db.scalar(select(func.count()).select_from(TraceBatch)) == 2  # old data retained


def test_recorded_fields_statistics_evidence_and_reparse(env, monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_TRACE_ENABLE_LEGACY_VALUE_SYNC", raising=False)
    records = []
    for i, status in enumerate(["success", "blocked", "error"]):
        r = deepcopy(EXAMPLE)
        r["sample_id"] = f"SOURCE-{i}"
        r["tokens"]["estimated"] = i == 1
        r["events"][3]["tool_status"] = status
        r["tool_events"][0].update(status=status, error_code=None if i == 0 else "POLICY_DENIED" if i == 1 else "HTTP_404")
        if i == 2:
            r.pop("outcome")
            r["audit"] = {"decision": "risk_found", "risk_level": "high", "provider": "external-evaluator", "evidence": [
                {"event_idx": 3, "risk_type": "privacy_leak", "label": "隐私泄露", "matched_text": "producer evidence", "severity": "high"}]}
        records.append(r)
    bid, _ = upload(env, tmp_path, records)
    with env["sessions"]() as db:
        while stats.materialize(db):
            db.commit()
        db.commit()
        w = stats.workspace(db, {"batch_id": bid}, USER)
        assert w["kpis"]["sample_count"]["value"] == 3
        assert w["kpis"]["tool_call_count"]["value"] == 3
        assert w["kpis"]["recorded_tokens"]["estimated_sample_count"] == 1
        assert w["kpis"]["task_success_rate"]["value"] == 100
        assert w["kpis"]["task_success_rate"]["valid_sample_count"] == 2
        assert w["kpis"]["risk_sample_count"]["value"] == 1
        assert not w["warnings"]
        batch, scope = q.resolve(db, bid, {})
        samples = list(db.scalars(select(TraceSample).where(TraceSample.snapshot_id == scope["snapshot_id"]).order_by(TraceSample.ingest_order)))
        assert [s.sample_id for s in samples] == ["SOURCE-0", "SOURCE-1", "SOURCE-2"]
        assert q.execution_detail(db, scope, samples[2].trace_id, {}, USER)["risk_distribution"]["items"][0]["code"] == "privacy_leak"
        calls = [q.tool_detail(db, batch, scope, s.trace_id, {}, USER)["calls"]["items"][0] for s in samples]
        assert [c["execution_status"] for c in calls] == ["succeeded", "not_executed", "failed"]
        assert calls[1]["duration_ms"] is None
        old_scope = scope
        task, _ = ingest.submit(db, {"batch_id": bid, "expected_version": batch.payload["batch_version"], "mode": "reparse_all"}, USER, uuid4().hex, "reparse")
        tid = task["task_id"]
    ingest.run(tid, env["sessions"])
    with env["sessions"]() as db:
        _, scope = q.resolve(db, bid, {})
        assert scope["snapshot_id"] != old_scope["snapshot_id"]
        assert db.scalar(select(func.count()).select_from(TraceSample).where(TraceSample.snapshot_id == scope["snapshot_id"])) == 3
        assert db.scalar(select(func.count()).select_from(TraceCall).where(TraceCall.snapshot_id == scope["snapshot_id"])) == 3
        assert q.resolve(db, bid, old_scope)[1] == old_scope


def test_internal_capture_is_actual_idempotent_and_has_no_fake_tools(env, monkeypatch):
    monkeypatch.delenv("AGENT_TRACE_ENABLE_LEGACY_VALUE_SYNC", raising=False)
    with env["sessions"]() as db:
        args = dict(invocation_id="real-call-1", model="test-model", messages=[{"role": "user", "content": "hello"}],
            response={"role": "assistant", "content": "world"}, usage={"prompt_tokens": 10, "completion_tokens": 5}, duration_ms=120)
        one = record_model_call(db, **args)
        two = record_model_call(db, **args)
        assert one == two
        db.commit()
        while stats.materialize(db):
            db.commit()
        db.commit()
        batch, scope = q.resolve(db, one["batch_id"], {})
        s = q.get_sample(db, scope, one["trace_id"])
        assert s.payload["data_origin"] == "actual"
        assert s.payload["call_count"] == 0
        assert len(q.process_records(db, scope, s.trace_id, TraceEvent)) == 2
        assert q.tool_detail(db, batch, scope, s.trace_id, {}, USER)["metrics"]["call_count"] == 0
        assert stats.workspace(db, {"batch_id": batch.batch_id}, USER)["kpis"]["recorded_tokens"]["value"] == 15


def test_export_uses_frozen_snapshot_and_all_pages(env, monkeypatch, tmp_path):
    import io
    import zipfile
    from app.services import agent_trace_export as exports
    from app.models.agent_traces import TraceArtifact

    monkeypatch.delenv("AGENT_TRACE_ENABLE_LEGACY_VALUE_SYNC", raising=False)
    records = [{**deepcopy(EXAMPLE), "sample_id": f"EXPORT-{i}"} for i in range(9)]
    bid, _ = upload(env, tmp_path, records)
    with env["sessions"]() as db:
        _, scope = q.resolve(db, bid, {})
        tasks = []
        for kind, fmt, contents in [("package", "zip", ["normalized_json", "evidence_index", "manifest"]),
                                     ("view", "png", ["ingest_png"])]:
            t, _ = exports.create_task(db, {**scope, "view": "ingest", "export_kind": kind, "format": fmt,
                "scope": {"type": "batch", "trace_ids": []}, "contents": contents,
                "redaction_profile": "standard", "anonymize_ids": True}, USER, uuid4().hex)
            tasks.append(t.task_id)
        b = db.get(TraceBatch, bid)
        reparse, _ = ingest.submit(db, {"batch_id": bid, "expected_version": b.payload["batch_version"], "mode": "reparse_all"}, USER, uuid4().hex, "new snapshot")
    ingest.run(reparse["task_id"], env["sessions"])
    for tid in tasks:
        exports.run_export(tid, env["sessions"])
        with env["sessions"]() as db:
            t = db.get(Task, tid)
            assert t.status == "succeeded", t.result
            assert t.input_data["snapshot_id"] == scope["snapshot_id"]
            artifact = db.get(TraceArtifact, t.result["artifacts"][0]["artifact_id"])
            data = (exports.directory() / artifact.storage_name).read_bytes()
            if artifact.mime_type == "image/png":
                assert data.startswith(b"\x89PNG")
            else:
                archive = zipfile.ZipFile(io.BytesIO(data))
                normalized = json.loads(archive.read("normalized.json"))
                assert len(normalized["samples"]) == 9  # beyond the six-row first page
                assert sum(len(s["calls"]) for s in normalized["samples"]) == 9
                assert b"EXPORT-0" not in archive.read("normalized.json")
