"""Opt-in live MySQL/HTTP check. Never prints tokens or source sample text.

Run from the backend root after starting the normal backend. Creates two export
tasks when --http is supplied; source value-analysis rows remain unchanged.
"""
import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select
import httpx

from app.core.database import SessionLocal
from app.core import auth
from app.models.tables import SystemUser
from app.models.agent_traces import TraceBatch, TraceSample, TraceCall, TraceEvent, TraceIngestCheckpoint


def run(http=False):
    root = Path("storage/browser-check")
    root.mkdir(parents=True, exist_ok=True)
    with SessionLocal() as db:
        counts = {name: db.scalar(select(func.count()).select_from(model)) for name, model in (
            ("batches", TraceBatch), ("samples", TraceSample), ("events", TraceEvent), ("calls", TraceCall))}
        checkpoints = [{"source_key": r.source_key, "status": r.status, "message": r.message}
                       for r in db.scalars(select(TraceIngestCheckpoint).where(TraceIngestCheckpoint.status != "succeeded"))]
        user = next((u for u in db.scalars(select(SystemUser).where(SystemUser.status.in_(["active", "normal"])).order_by(SystemUser.id))
                     if auth.permission_tier(u.role) == "operator"), None)
        if not user:
            raise RuntimeError("No active operator account available")
        auth.TOKEN_TTL_SECONDS = 3600
        session = {"token": auth.create_token(user.id, user.username, user.role),
                   "user": {"username": user.username, "displayName": user.display_name, "role": user.role, "tier": "operator"}}
        (root / "session.json").write_text(json.dumps(session, ensure_ascii=False), encoding="utf-8")
        # Upload verification batches may sort first. Exercise a value batch with enough rows
        # for the cross-page browser check rather than depending on catalog ordering.
        verification_batch = db.scalar(select(TraceBatch.batch_id)
            .join(TraceSample, TraceSample.snapshot_id == TraceBatch.active_snapshot_id)
            .where(TraceBatch.source_system == "business")
            .group_by(TraceBatch.batch_id).having(func.count() >= 7)
            .order_by(TraceBatch.batch_id).limit(1))
    report = {"database": "actual_mysql", "counts": counts, "source_diagnostics": checkpoints}
    print(json.dumps(report, ensure_ascii=True), flush=True)
    if not http:
        return
    with httpx.Client(base_url="http://127.0.0.1:8000/api/v1", headers={"Authorization": "Bearer " + session["token"]}, timeout=30) as client:
        def get(path, **params):
            response = client.get(path, params=params)
            response.raise_for_status()
            payload = response.json()
            assert payload["code"] == 0
            return payload["data"]
        workspace = get("/compliance/agent-traces/workspace", view="tools", include="options")
        assert workspace["batch_options"]["items"], "No published value-analysis batch"
        assert verification_batch, "No value-analysis batch with at least seven samples"
        bid = verification_batch
        workspace = get("/compliance/agent-traces/workspace", view="tools", batch_id=bid)
        scope = workspace["candidate_scope"]
        trace = workspace["sample_options"]["items"][0]["trace_id"]
        path = f"/compliance/agent-traces/batches/{bid}/samples/{trace}"
        detail = get(path, view="tools", **scope)
        assert detail["metrics"]["call_count"] == detail["calls"]["total"]
        call = detail["selection"]
        if call:
            raw = get(path, view="tools", section="selection", call_id=call["call_id"], include_raw="true", **scope)
            assert raw["selection"]["raw_events"]["items"]
            execution = get(path, view="execution", section="selection", event_id=call["event_id"], **scope)
            assert execution["selection"]["event_id"] == call["event_id"]
        listing = get(f"/compliance/agent-traces/batches/{bid}/samples", view="tools", page=2, page_size=3, **scope)
        assert listing["page"] == 2
        report["http"] = {"batch_id": bid, "trace_id": trace, "sample_total": workspace["sample_options"]["total"],
                          "metrics": detail["metrics"], "raw_and_step_location": True, "exports": []}
        for kind, fmt, contents in (("view", "png", ["tools_png"]),
                                    ("package", "zip", ["normalized_json", "evidence_index", "manifest"])):
            response = client.post("/tasks/execute", headers={"X-Request-Id": "live-check-" + str(time.time_ns())}, json={
                "capability_code": "agent_trace_export", "name": "工具交互真实联调 " + fmt,
                "input": {**scope, "view": "tools", "export_kind": kind, "format": fmt,
                    "selected_trace_id": trace, "selected_call_id": call["call_id"] if call else None,
                    "scope": {"type": "sample", "trace_ids": [trace]}, "contents": contents,
                    "redaction_profile": "standard", "anonymize_ids": True}})
            response.raise_for_status()
            task_id = response.json()["data"]["task_id"]
            for _ in range(60):
                task = get("/tasks/" + task_id)
                if task["status"] not in {"pending", "running"}:
                    break
                time.sleep(1)
            assert task["status"] == "succeeded", task.get("error_message")
            artifact = task["result"]["artifacts"][0]
            ticket = client.post("/compliance/agent-traces/artifacts/" + artifact["artifact_id"] + "/download-tickets",
                headers={"X-Request-Id": "live-ticket-" + str(time.time_ns())}, json={})
            ticket.raise_for_status()
            with httpx.Client(base_url="http://127.0.0.1:8000", timeout=30) as unauthenticated:
                file = unauthenticated.get(ticket.json()["data"]["download_url"])
                file.raise_for_status()
            import hashlib
            assert hashlib.sha256(file.content).hexdigest() == artifact["sha256"]
            (root / ("server-report." + fmt)).write_bytes(file.content)
            report["http"]["exports"].append({"format": fmt, "task_id": task_id,
                "size_bytes": len(file.content), "sha256_verified": True})
    (root / "mysql-http-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["http"], ensure_ascii=True), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--http", action="store_true")
    run(parser.parse_args().http)
