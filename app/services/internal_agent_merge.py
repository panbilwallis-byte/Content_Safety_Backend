"""Explicit maintenance merge of completed one-run batches, preserving all historical rows."""
from copy import deepcopy

from fastapi import HTTPException
from sqlalchemy import select

from app.models.internal_agent import InternalAgentRun
from app.models.agent_traces import (TraceBatch, TraceSnapshot, TraceProjection, TraceSample,
    TraceEvent, TraceCall, TraceRelation, TraceEvidence, TraceStatisticsSample)
from app.services.agent_trace_generation import stable_id
from app.services.internal_agent_batches import batch_info


def merge_runs(db, run_ids, name, *, apply=False):
    ids = sorted(set(run_ids))
    if len(ids) != len(run_ids) or len(ids) < 2 or not name.strip() or len(name.strip()) > 100:
        raise HTTPException(422, "请指定至少两个不同任务和有效的批次名称")
    name = name.strip()
    runs = list(db.scalars(select(InternalAgentRun).where(InternalAgentRun.run_id.in_(ids))
                          .order_by(InternalAgentRun.run_id).with_for_update()))
    if len(runs) != len(ids) or len({r.owner_id for r in runs}) != 1:
        raise HTTPException(409, "任务不存在或不属于同一账号")
    if any(r.status in {"pending", "running"} or not r.payload.get("trace") for r in runs):
        raise HTTPException(409, "请等待任务结束并成功发布轨迹后再合并")
    bid = stable_id("batch_", "internal_agent_merge", name, *ids)
    sid = stable_id("snap_", bid, "merge")
    existing = db.get(TraceBatch, bid)
    if existing:
        if all(r.payload["trace"]["batch_id"] == bid for r in runs):
            return {"applied": True, "already_merged": True, **batch_info(existing), "trace_count": len(runs)}
        raise HTTPException(409, "目标批次已经存在，但任务归属不一致")
    source_ids = {r.payload["trace"]["batch_id"] for r in runs}
    if len(source_ids) != len(ids):
        raise HTTPException(409, "本操作仅合并各自独立的任务批次")
    batches = list(db.scalars(select(TraceBatch).where(TraceBatch.batch_id.in_(source_ids))
                             .order_by(TraceBatch.batch_id).with_for_update()))
    if len(batches) != len(ids) or any(b.source_system != "internal_agent" or b.payload.get("merged_into") for b in batches):
        raise HTTPException(409, "原批次不存在、已合并或不是内部智能体批次")
    active = db.scalar(select(InternalAgentRun.run_id).where(InternalAgentRun.status.in_(["pending", "running"]),
        InternalAgentRun.config["trace_batch"]["batch_id"].as_string().in_(source_ids)))
    if active:
        raise HTTPException(409, "还有任务准备加入原批次，请等待完成后再合并")
    by_id = {b.batch_id: b for b in batches}
    samples = []
    for run in runs:
        trace = run.payload["trace"]
        source = by_id[trace["batch_id"]]
        rows = list(db.scalars(select(TraceSample).where(TraceSample.snapshot_id == source.active_snapshot_id)))
        if len(rows) != 1 or rows[0].trace_id != trace["trace_id"]:
            raise HTTPException(409, "原批次不是单条指定任务轨迹，已停止合并")
        samples.append(rows[0])
    if len({s.trace_id for s in samples}) != len(samples):
        raise HTTPException(409, "任务轨迹重复，已停止合并")
    plan = {"applied": apply, "name": name, "batch_id": bid, "snapshot_id": sid,
            "owner_id": runs[0].owner_id, "trace_count": len(samples), "run_ids": ids,
            "original_batches": [{**batch_info(b), "snapshot_id": b.active_snapshot_id,
                                  "payload": deepcopy(b.payload)} for b in batches],
            "original_runs": [{"run_id": r.run_id, "trace": deepcopy(r.payload["trace"]),
                               "trace_batch": deepcopy(r.config.get("trace_batch"))} for r in runs]}
    if not apply:
        return plan
    target = TraceBatch(batch_id=bid, source_system="internal_agent", source_task_id=stable_id("merge_", *ids),
        display_id="AGT-" + bid[-8:].upper(), active_snapshot_id=sid,
        payload={"name": name, "owner_id": runs[0].owner_id, "batch_version": 1,
                 "data_origin": "actual", "merged_from": sorted(source_ids)})
    db.add(target)
    rule_versions = {db.get(TraceSnapshot, s.snapshot_id).payload.get("rule_version") for s in samples}
    db.add(TraceSnapshot(snapshot_id=sid, batch_id=bid,
        payload={"schema_version": "1", "batch_version_at_start": 1, "sample_count": len(samples),
                 "data_origin": "actual", "merge_sources": sorted(source_ids),
                 "rule_version": next(iter(rule_versions)) if len(rule_versions) == 1 else None}))
    projections = {}
    for order, sample in enumerate(samples):
        db.add(TraceSample(id=stable_id("sample_", sid, sample.trace_id), snapshot_id=sid,
            trace_id=sample.trace_id, sample_id=sample.sample_id, display_id=f"AUD-{order+1:04}",
            ingest_order=order, language=sample.language, result=deepcopy(sample.result), payload=deepcopy(sample.payload)))
        for model in (TraceEvent, TraceCall, TraceRelation, TraceEvidence, TraceStatisticsSample):
            for row in db.scalars(select(model).where(model.snapshot_id == sample.snapshot_id, model.trace_id == sample.trace_id)):
                values = {"id": stable_id("merge_row_", sid, row.id), "snapshot_id": sid,
                          "trace_id": row.trace_id, "payload": deepcopy(row.payload)}
                if model == TraceEvent: values.update(event_id=row.event_id, ordinal=row.ordinal)
                if model == TraceCall: values.update(call_id=row.call_id, ordinal=row.ordinal)
                if model == TraceEvidence: values.update(event_id=row.event_id, projection_version=row.projection_version)
                db.add(model(**values))
        for p in db.scalars(select(TraceProjection).where(TraceProjection.snapshot_id == sample.snapshot_id)):
            projections[p.version] = deepcopy(p.payload)
    for version, payload in projections.items():
        db.add(TraceProjection(projection_id=stable_id("proj_", sid, version), snapshot_id=sid, version=version, payload=payload))
    for b in batches:
        b.payload = {**b.payload, "merged_into": bid}
    for r in runs:
        r.config = {**r.config, "trace_batch": batch_info(target)}
        r.payload = {**r.payload, "trace_before_batch_merge": deepcopy(r.payload["trace"]),
                     "trace": {"batch_id": bid, "snapshot_id": sid, "trace_id": r.payload["trace"]["trace_id"]}}
    db.flush()
    return plan
