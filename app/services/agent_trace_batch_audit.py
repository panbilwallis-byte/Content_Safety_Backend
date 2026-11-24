"""Checkpoint rule results, then enrich the staged snapshot before publishing."""
from datetime import timedelta
import logging

from sqlalchemy import delete, select, update

from app.core.time import now_shanghai
from app.models.agent_traces import (
    TraceBatch, TraceAsset, TraceSnapshot, TraceSample, TraceEvent,
    TraceCall, TraceRelation, TraceEvidence, TraceStatisticsSample,
)
from app.models.tables import Task
from app.services import agent_trace_ingest as ingest, agent_trace_query as query
from app.services.agent_trace_audit import apply_model_audit
from app.services.agent_trace_generation import stable_id, validate_process
from app.services import agent_trace_model_audit as model_audit

LOGGER = logging.getLogger(__name__)
AUDIT_LIMIT = timedelta(hours=23)
PUBLISH_RESERVE = timedelta(minutes=5)
STOP_AFTER = AUDIT_LIMIT - PUBLISH_RESERVE
CAPABILITY = "agent_trace_ingest"


class AuditStopped(Exception):
    """The task lost its publication window or another worker finalized it."""


def snapshot_id(task):
    return stable_id("snap_", task.input_data["batch_id"], task.task_id)


def running_task(db, task_id):
    # The no-op write also serializes checkpoint and finalization on SQLite.
    changed = db.execute(update(Task).where(Task.task_id == task_id, Task.status == "running")
                         .values(progress=Task.progress).execution_options(synchronize_session=False))
    return db.get(Task, task_id, populate_existing=True) if changed.rowcount == 1 else None


def work_task(db, task_id):
    task = running_task(db, task_id)
    if not task or now_shanghai() >= task.created_at + STOP_AFTER:
        raise AuditStopped
    return task


def make_stage(db, task):
    data = task.input_data
    batch = db.get(TraceBatch, data["batch_id"])
    if not batch or batch.payload["batch_version"] != data["expected_version"]:
        raise ValueError("解析输入版本已变化")
    sid = snapshot_id(task)
    if db.get(TraceSnapshot, sid) is None:
        ingest.publish(db, batch, task.task_id, [], set(data["file_ids"]),
                       staging_task_id=task.task_id)
    return db.get(TraceSnapshot, sid)


def owned_files(db, task, batch):
    selected = set(task.input_data["file_ids"])
    files = {row.file_id: row for row in ingest.bindings(db, batch.batch_id, lock=True)
             if row.file_id in selected}
    if set(files) != selected or any(f.payload.get("last_task_id") != task.task_id for f in files.values()):
        raise ValueError("解析任务不再拥有文件绑定")
    return files


def finish(sessions, task_id, reason):
    with sessions() as db:
        initial = db.get(Task, task_id)
        if not initial or initial.status != "running":
            return
        batch = ingest.lock_batch(db, initial.input_data["batch_id"])
        task = running_task(db, task_id)
        if not task:
            return
        if not batch or batch.payload["batch_version"] != task.input_data["expected_version"]:
            raise ValueError("解析输入版本已变化")
        files = owned_files(db, task, batch)
        snapshot = db.get(TraceSnapshot, snapshot_id(task))
        if snapshot is None:
            snapshot = make_stage(db, task)
        expired = now_shanghai() >= task.created_at + STOP_AFTER
        rules_complete = snapshot.payload.get("rules_complete", False)
        updates = snapshot.payload["file_updates"]
        parsed = snapshot.payload["parsed_count"]
        failed = sum(value["failed_record_count"] for value in updates.values())
        if reason == "rules_complete" and not parsed and failed:
            task.status = "failed"
            task.result = {**task.result, "error_message": "所选文件没有可发布的有效轨迹，保留原快照"}
            for key, file in files.items():
                file.payload = {**file.payload, **updates[key]}
        else:
            stop_reason = "deadline" if expired else reason
            model_completed = snapshot.payload.get("model_completed_trace_count", 0)
            model_unavailable = snapshot.payload.get("model_unavailable_trace_count", 0)
            completion = {
                "mode": "rules_and_model" if model_completed else "rules",
                "reason": stop_reason, "rules_complete": rules_complete,
                "completed_trace_count": parsed,
                "model_completed_trace_count": model_completed,
                "model_unavailable_trace_count": model_unavailable,
                "model_unattempted_trace_count": parsed - model_completed - model_unavailable,
            }
            snapshot.payload = {**snapshot.payload, "audit_completion": completion}
            ingest.activate_snapshot(db, batch, snapshot)
            for key, file in files.items():
                file.payload = {**file.payload, **updates.get(key, {"parse_status": "succeeded",
                    "recognition_state": "unknown", "detected_format": None, "parsed_trace_count": 0,
                    "failed_record_count": 0, "input_candidate_count": None, "error_examples": []}),
                    "snapshot_id": snapshot.snapshot_id}
            task.status = "succeeded"
            task.result = {"kind": CAPABILITY, "batch_id": batch.batch_id, "snapshot_id": snapshot.snapshot_id,
                "outcome": "partial" if failed or not rules_complete else "complete" if parsed else "empty",
                "parsed_trace_count": parsed,
                "failed_record_count": failed, "batch_version": batch.payload["batch_version"],
                "file_count": len(files), "schema_version": "1", "audit_completion": completion}
        task.progress = 100
        task.finished_at = now_shanghai()
        db.commit()


def stage_rules(sessions, task_id):
    with sessions() as db:
        task = work_task(db, task_id)
        baseline = make_stage(db, task)
        sid, order = baseline.snapshot_id, baseline.payload["next_order"]
        assets = []
        for binding in task.config["bindings"]:
            asset = db.get(TraceAsset, binding["file_id"])
            assets.append((asset.file_id, asset.filename, asset.storage_path, asset.sha256, binding["format_override"]))
        db.commit()

    for asset in assets:
        file_id = asset[0]
        if not still_working(sessions, task_id):
            raise AuditStopped
        samples, final_update = ingest.parse_asset(*asset)
        for start in range(0, len(samples), 16):
            if not still_working(sessions, task_id):
                raise AuditStopped
            chunk = samples[start:start + 16]
            prepared = [ingest.prepare_sample(sample, order + i) for i, sample in enumerate(chunk)]
            with sessions() as db:
                work_task(db, task_id)
                baseline = db.get(TraceSnapshot, sid)
                for index, (sample, projection) in enumerate(zip(chunk, prepared, strict=True), order):
                    ingest.store_prepared(db, sid, sample, index, projection, stage=True)
                order += len(prepared)
                completed = start + len(chunk)
                updates = {**baseline.payload["file_updates"], file_id: {
                    **final_update, "parse_status": "succeeded", "parsed_trace_count": completed,
                    "failed_record_count": 0, "error_examples": [], "input_candidate_count": completed,
                }}
                baseline.payload = {**baseline.payload, "sample_count": baseline.payload["sample_count"] + len(chunk),
                    "parsed_count": baseline.payload["parsed_count"] + len(chunk), "file_updates": updates}
                db.commit()
        with sessions() as db:
            work_task(db, task_id)
            baseline = db.get(TraceSnapshot, sid)
            baseline.payload = {**baseline.payload, "file_updates": {**baseline.payload["file_updates"], file_id: final_update}}
            db.commit()

    with sessions() as db:
        task = work_task(db, task_id)
        baseline = db.get(TraceSnapshot, sid)
        baseline.payload = {**baseline.payload, "rules_complete": True}
        task.progress = 60
        db.commit()
        return baseline.payload["parsed_count"], baseline.payload["next_order"]


def still_working(sessions, task_id):
    with sessions() as db:
        task = db.get(Task, task_id)
        return bool(task and task.status == "running" and now_shanghai() < task.created_at + STOP_AFTER)


def stage_model(sessions, task_id, first_order, config):
    if not config.endpoint:
        return "configuration_missing"
    from app.services.agent_trace_statistics import statistics_sample

    with sessions() as db:
        sid = snapshot_id(db.get(Task, task_id))
    scope = {"snapshot_id": sid}
    last_failure = None

    with model_audit.client() as transport:
        after = first_order - 1
        while still_working(sessions, task_id):
            with sessions() as db:
                row = db.scalar(select(TraceSample).where(TraceSample.snapshot_id == sid,
                                                          TraceSample.ingest_order > after)
                                .order_by(TraceSample.ingest_order).limit(1))
                if not row:
                    return last_failure or "model_complete"
                after = row.ingest_order
                events = query.process_records(db, scope, row.trace_id, TraceEvent)
                calls = query.process_records(db, scope, row.trace_id, TraceCall)
            metadata = model_audit.audit(transport, config, row.payload, events, calls,
                                        record=row.result.get("source_record", {}),
                                        should_continue=lambda: still_working(sessions, task_id))
            if metadata.get("reason") == "deadline":
                raise AuditStopped
            if metadata.get("status") != "completed":
                last_failure = metadata.get("reason", "model_unavailable")
            evidence = []
            apply_model_audit(row.payload, row.trace_id, metadata, events, evidence)
            validate_process(row.payload, events, calls)
            with sessions() as db:
                work_task(db, task_id)
                stored = db.get(TraceSample, row.id)
                stored.payload = row.payload
                if metadata.get("risk_detected"):
                    db.merge(statistics_sample(stored))
                for item in evidence:
                    db.add(TraceEvidence(id=stable_id("row_", sid, row.trace_id, item["evidence_id"]),
                                         snapshot_id=sid, trace_id=row.trace_id, event_id=item["event_id"],
                                         projection_version=ingest.VERSION, payload=item))
                snapshot = db.get(TraceSnapshot, sid)
                count_key = ("model_completed_trace_count" if metadata["status"] == "completed"
                             else "model_unavailable_trace_count")
                snapshot.payload = {**snapshot.payload, count_key: snapshot.payload.get(count_key, 0) + 1}
                db.commit()
    raise AuditStopped


def fail_task(sessions, task_id):
    with sessions() as db:
        task = running_task(db, task_id)
        if not task:
            return
        task.status, task.finished_at = "failed", now_shanghai()
        task.result = {**task.result, "error_message": "解析执行失败，原快照保持可用"}
        for file in ingest.bindings(db, task.input_data["batch_id"]):
            if file.payload.get("last_task_id") == task_id:
                file.payload = {**file.payload, "parse_status": "failed"}
        db.commit()


def run_batch(task_id, sessions):
    with sessions() as db:
        claimed = db.execute(update(Task).where(Task.task_id == task_id, Task.status == "pending")
                             .values(status="running", progress=10))
        if claimed.rowcount != 1:
            return
        task = db.get(Task, task_id)
        provider = model_audit.STRATEGIES.get(task.input_data.get("audit_strategy", "default"))
        model_name = task.config.get("audit_model")
        db.commit()
    try:
        count, first = stage_rules(sessions, task_id)
        reason = "rules_complete"
        if provider and count:
            try:
                reason = stage_model(sessions, task_id, first, model_audit.configuration(provider, model=model_name))
            except AuditStopped:
                raise
            except Exception:
                LOGGER.warning("Trace model audit interrupted for task %s", task_id)
                reason = "model_interrupted"
        finish(sessions, task_id, reason)
    except AuditStopped:
        finish(sessions, task_id, "deadline")
    except Exception:
        LOGGER.exception("Trace rule audit or publication failed for task %s", task_id)
        fail_task(sessions, task_id)


def publish_due(sessions):
    with sessions() as db:
        ids = list(db.scalars(select(Task.task_id).where(Task.capability_code == CAPABILITY,
            Task.status.in_(("pending", "running")), Task.created_at <= now_shanghai() - STOP_AFTER)))
    for task_id in ids:
        with sessions() as db:
            db.execute(update(Task).where(Task.task_id == task_id, Task.status == "pending")
                       .values(status="running"))
            db.commit()
        try:
            finish(sessions, task_id, "deadline")
        except Exception:
            LOGGER.warning("Could not publish overdue trajectory audit %s", task_id)


def discard_finished_staging(db):
    sid = db.scalar(select(TraceSnapshot.snapshot_id).join(
        Task, Task.task_id == TraceSnapshot.payload["staging_task_id"].as_string())
        .where(Task.status.in_(("succeeded", "failed"))).limit(1))
    if not sid:
        return
    for table in (TraceSample, TraceEvent, TraceCall, TraceRelation, TraceEvidence, TraceStatisticsSample):
        db.execute(delete(table).where(table.snapshot_id == sid))
    db.execute(delete(TraceSnapshot).where(TraceSnapshot.snapshot_id == sid))
