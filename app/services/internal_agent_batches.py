"""Owner-scoped destinations for internal agent runs; audit publications stay immutable."""
from fastapi import HTTPException
from sqlalchemy import func, or_, select

from app.models.agent_traces import TraceBatch, TraceSample
from app.models.internal_agent import InternalAgentRun
from app.services.agent_trace_generation import stable_id


def batch_info(batch):
    return {"batch_id": batch.batch_id, "display_id": batch.display_id,
            "name": batch.payload.get("name") or "内部智能体任务"}


def owned_batch(db, batch_id, owner_id, *, lock=False):
    statement = select(TraceBatch).where(TraceBatch.batch_id == batch_id)
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    batch = db.scalar(statement)
    if not batch or batch.source_system != "internal_agent" or batch.payload.get("merged_into"):
        raise HTTPException(404, "批次不存在，或不能加入该批次")
    owner = batch.payload.get("owner_id")
    # Legacy one-run batches did not store an owner; their originating run is authoritative.
    if owner is None:
        run = db.get(InternalAgentRun, batch.source_task_id)
        owner = run.owner_id if run else None
    if str(owner) != str(owner_id):
        raise HTTPException(404, "批次不存在，或不能加入该批次")
    return batch


def new_batch(db, run_id, owner_id, name):
    bid = stable_id("batch_", "internal_agent", run_id)
    batch = TraceBatch(batch_id=bid, source_system="internal_agent", source_task_id=run_id,
        display_id="AGT-" + bid[-8:].upper(), active_snapshot_id="",
        payload={"name": name, "owner_id": int(owner_id), "batch_version": 1, "data_origin": "actual"})
    db.add(batch)
    db.flush()
    return batch


def options(db, user, search="", page=1, page_size=20):
    uid = int(user["uid"])
    owner = TraceBatch.payload["owner_id"].as_integer()
    # The outer join includes old batches without changing any historical records.
    statement = (select(TraceBatch).outerjoin(InternalAgentRun,
        InternalAgentRun.run_id == TraceBatch.source_task_id)
        .where(TraceBatch.source_system == "internal_agent", TraceBatch.payload["merged_into"].as_string().is_(None),
            or_(owner == uid, (owner.is_(None)) & (InternalAgentRun.owner_id == uid))))
    search = search.strip()
    if search:
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = "%" + escaped + "%"
        statement = statement.where(or_(TraceBatch.payload["name"].as_string().ilike(pattern, escape="\\"),
                                        TraceBatch.display_id.ilike(pattern, escape="\\")))
    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    rows = list(db.scalars(statement.order_by(TraceBatch.created_at.desc(), TraceBatch.batch_id)
                           .offset((page - 1) * page_size).limit(page_size)))
    snapshot_ids = [b.active_snapshot_id for b in rows if b.active_snapshot_id]
    counts = dict(db.execute(select(TraceSample.snapshot_id, func.count()).where(
        TraceSample.snapshot_id.in_(snapshot_ids)).group_by(TraceSample.snapshot_id)).all()) if snapshot_ids else {}
    return {"items": [{**batch_info(b), "trace_count": counts.get(b.active_snapshot_id, 0)} for b in rows],
            "total": total, "page": page, "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size}
