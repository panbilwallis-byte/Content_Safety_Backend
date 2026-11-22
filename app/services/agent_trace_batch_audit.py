"""Checkpoint rule and model snapshots before selecting a result to publish."""
from contextlib import nullcontext
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


def snapshot_ids(task):
    bid = task.input_data["batch_id"]
    return stable_id("snap_", bid, task.task_id), stable_id("snap_", bid, task.task_id + ":model")


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


def make_stage(db, task, *, model=False):
    data = task.input_data
    batch = db.get(TraceBatch, data["batch_id"])
    if not batch or batch.payload["batch_version"] != data["expected_version"]:
        raise ValueError("解析输入版本已变化")
    suffix = ":model" if model else ""
    sid = snapshot_ids(task)[int(model)]
    if db.get(TraceSnapshot, sid) is None:
        ingest.publish(db, batch, task.task_id + suffix, [], set(data["file_ids"]),
                       staging_task_id=task.task_id)
    return db.get(TraceSnapshot, sid)


def owned_files(db, task, batch):
    selected = set(task.input_data["file_ids"])
    files = {row.file_id: row for row in ingest.bindings(db, batch.batch_id, lock=True)
             if row.file_id in selected}
    if set(files) != selected or any(f.payload.get("last_task_id") != task.task_id for f in files.values()):
        raise ValueError("解析任务不再拥有文件绑定")
    return files


def finish(sessions, task_id, reason, *, model_complete=False):
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
        rules_id, model_id = snapshot_ids(task)
        rules = db.get(TraceSnapshot, rules_id)
        if rules is None:
            rules = make_stage(db, task)
        expired = now_shanghai() >= task.created_at + STOP_AFTER
        rules_complete = rules.payload.get("rules_complete", False)
        candidate = db.get(TraceSnapshot, model_id) if model_complete and not expired else None
        if candidate is not None and candidate.payload["parsed_count"] != rules.payload["parsed_count"]:
            raise ValueError("模型审计结果未覆盖整批")
        updates = rules.payload["file_updates"]
        parsed = rules.payload["parsed_count"]
        failed = sum(value["failed_record_count"] for value in updates.values())
        if reason == "rules_complete" and not parsed and failed:
            task.status = "failed"
            task.result = {**task.result, "error_message": "所选文件没有可发布的有效轨迹，保留原快照"}
            for key, file in files.items():
                file.payload = {**file.payload, **updates[key]}
        else:
            snapshot = candidate if candidate is not None else rules
            stop_reason = "deadline" if expired else reason
            snapshot.payload = {**snapshot.payload, "audit_completion": {
                "mode": "rules_and_model" if candidate is not None else "rules",
                "reason": stop_reason, "rules_complete": rules_complete,
                "completed_trace_count": parsed,
            }}
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
                "file_count": len(files), "schema_version": "1"}
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


def staged_projection(db, sid, row):
    scope = {"snapshot_id": sid}
    events = query.process_records(db, scope, row.trace_id, TraceEvent)
    calls = query.process_records(db, scope, row.trace_id, TraceCall)
    relations, evidence = ([record.payload for record in db.scalars(select(table).where(
        table.snapshot_id == sid, table.trace_id == row.trace_id).order_by(table.id))]
        for table in (TraceRelation, TraceEvidence))
    return row.trace_id, row.payload, events, calls, relations, evidence


def stage_model(sessions, task_id, first_order, config):
    if not config.endpoint:
        return "configuration_missing"
    if not still_working(sessions, task_id):
        raise AuditStopped
    # Candidate construction does not hold the task lock needed to publish the baseline.
    with sessions() as db:
        task = db.get(Task, task_id)
        rules_id, model_id = snapshot_ids(task)
        make_stage(db, task, model=True)
        db.commit()

    with model_audit.client() as transport:
        after = first_order - 1
        while still_working(sessions, task_id):
            with sessions() as db:
                row = db.scalar(select(TraceSample).where(TraceSample.snapshot_id == rules_id,
                                                          TraceSample.ingest_order > after)
                                .order_by(TraceSample.ingest_order).limit(1))
                if not row:
                    return None
                after = row.ingest_order
                snapshot_sample = {**row.result, "language": row.language}
                projection = staged_projection(db, rules_id, row)
            trace, payload, events, calls, _, evidence = projection
            metadata = model_audit.audit(transport, config, payload, events, calls,
                                        record=snapshot_sample.get("source_record", {}),
                                        should_continue=lambda: still_working(sessions, task_id))
            if metadata.get("status") != "completed":
                return metadata.get("reason", "model_unavailable")
            apply_model_audit(payload, trace, metadata, events, evidence)
            validate_process(payload, events, calls)
            with sessions() as db:
                work_task(db, task_id)
                candidate = db.get(TraceSnapshot, model_id)
                ingest.store_prepared(db, model_id, snapshot_sample, after, projection, stage=True)
                candidate.payload = {**candidate.payload, "sample_count": candidate.payload["sample_count"] + 1,
                                     "parsed_count": candidate.payload["parsed_count"] + 1}
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
        reason, complete = "rules_complete", False
        if provider and count:
            try:
                reason = stage_model(sessions, task_id, first, model_audit.configuration(provider, model=model_name))
                complete = reason is None
                reason = reason or "model_complete"
            except AuditStopped:
                raise
            except Exception:
                LOGGER.warning("Trace model audit interrupted for task %s", task_id)
                reason = "model_interrupted"
        finish(sessions, task_id, reason, model_complete=complete)
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
