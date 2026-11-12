"""Server-side recording seam for future internal model/agent execution.

Call after a real invocation, using the captured messages and provider usage.
This module never invokes a model or fabricates tool results. The caller owns
the transaction. There is deliberately no client-writable 'internal' endpoint.
"""
from copy import deepcopy
from sqlalchemy import select

from app.models.agent_traces import TraceBatch, TraceSample
from app.services.agent_trace_generation import stable_id, dumps
from app.services.agent_trace_ingest import normalize, publish


def record_model_call(db, *, invocation_id, model, messages, response=None,
                      usage=None, duration_ms=None, error=None, language="unknown"):
    if not isinstance(invocation_id, str) or not invocation_id:
        raise ValueError("真实调用编号必填")
    if not isinstance(messages, list) or not messages:
        raise ValueError("需要真实请求消息")
    bid = stable_id("batch_", "internal_model", invocation_id)
    batch = db.get(TraceBatch, bid)
    if batch:
        row = db.scalar(select(TraceSample).where(TraceSample.snapshot_id == batch.active_snapshot_id))
        return {"batch_id": bid, "snapshot_id": batch.active_snapshot_id, "trace_id": row.trace_id}
    captured = deepcopy(messages)
    if response is not None:
        if not isinstance(response, dict) or response.get("role") != "assistant":
            raise ValueError("响应必须是实际 assistant 消息")
        captured.append(deepcopy(response))
    if response is None and error is None:
        raise ValueError("需要实际响应或错误记录")
    record = {"messages": captured, "model": model, "language": language,
              "duration_ms": duration_ms, "usage": deepcopy(usage or {}),
              "task_status": "failed" if error is not None else "completed"}
    sample = normalize(record, stable_id("src_", invocation_id), 0, dumps(record), 1, "openai_messages")
    sample["execution"].update(origin_type="platform", model=model, invocation_id=invocation_id,
                               error=str(error) if error is not None else None)
    sample["execution"]["source"].update(format_label="内部模型调用 · 实际记录", filename="内部模型调用记录")
    batch = TraceBatch(batch_id=bid, source_system="internal_model", source_task_id=stable_id("inv_", invocation_id),
        display_id="MODEL-" + bid[-10:].upper(), active_snapshot_id="",
        payload={"name": "内部模型调用 · " + str(model), "batch_version": 1, "data_origin": "actual"})
    db.add(batch)
    db.flush()
    sid = publish(db, batch, stable_id("capture_", invocation_id), [sample], set())
    row = db.scalar(select(TraceSample).where(TraceSample.snapshot_id == sid))
    return {"batch_id": bid, "snapshot_id": sid, "trace_id": row.trace_id}
