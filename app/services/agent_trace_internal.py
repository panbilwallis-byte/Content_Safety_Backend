"""Server-side recording seam for future internal model/agent execution.

Call after a real invocation, using the captured messages and provider usage.
This module never invokes a model or fabricates tool results. The caller owns
the transaction. There is deliberately no client-writable 'internal' endpoint.
"""
from copy import deepcopy
from sqlalchemy import select

from app.models.agent_traces import TraceBatch, TraceSample, TraceSnapshot
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


def record_agent_run(db, run_id, config, payload, status):
    """Publish once from persisted actual messages; safe to retry publication without execution."""
    from app.services.internal_agent import usage
    destination = config.get("trace_batch")
    bid = destination["batch_id"] if destination else stable_id("batch_", "internal_agent", run_id)
    # Serialize append against the latest publication, including retries from multiple requests.
    batch = db.scalar(select(TraceBatch).where(TraceBatch.batch_id == bid)
                      .with_for_update().execution_options(populate_existing=True))
    if destination and (not batch or batch.source_system != "internal_agent"):
        raise ValueError("任务所属批次不存在，轨迹暂未发布")
    recorded = payload.get("trace") or {}
    if recorded.get("batch_id") == bid:
        snapshot = db.get(TraceSnapshot, recorded.get("snapshot_id"))
        sample = db.scalar(select(TraceSample).where(TraceSample.snapshot_id == recorded.get("snapshot_id"),
                                                    TraceSample.trace_id == recorded.get("trace_id")))
        if snapshot and snapshot.batch_id == bid and sample and sample.payload.get("task_id") == run_id:
            return deepcopy(recorded)
    publication = stable_id("capture_", run_id)
    published_sid = stable_id("snap_", bid, publication)
    source_id = stable_id("src_", run_id)
    trace_id = stable_id("trc_", "upload", source_id + ":0")
    if db.scalar(select(TraceSnapshot).where(TraceSnapshot.snapshot_id == published_sid).with_for_update()):
        row = db.scalar(select(TraceSample).where(TraceSample.snapshot_id == published_sid,
                                                 TraceSample.trace_id == trace_id).with_for_update())
        if not row:
            raise ValueError("已发布版本中缺少任务轨迹")
        return {"batch_id": bid, "snapshot_id": published_sid, "trace_id": row.trace_id}
    totals = usage(payload.get("invocations", []))
    invocations = payload.get("invocations", [])
    # Reasoning tokens, when supplied, are a subset of provider completion tokens.
    reasoning = [i.get("usage", {}).get("completion_tokens_details", {}).get("reasoning_tokens")
        if isinstance(i.get("usage"), dict) and isinstance(i["usage"].get("completion_tokens_details"), dict) else None for i in invocations]
    source = {"messages": deepcopy(payload["messages"]), "model": config["model_id"], "language": "zh",
        "duration_ms": payload.get("duration_ms"), "task_status": {"succeeded": "completed", "cancelled": "terminated"}.get(status, "failed"),
        "usage": {"input_tokens": totals["input_tokens"], "output_tokens": totals["output_tokens"],
                  "completion_tokens_details": {"reasoning_tokens": sum(reasoning) if reasoning and all(isinstance(n, int) for n in reasoning) else None}},
        "metadata": {"task_id": run_id, "dataset_id": config["dataset_id"], "dataset_version": config["dataset"]["version"],
                     "sample_count": len(config["samples"]), "error": payload.get("error"), "model_invocations": deepcopy(invocations),
                     "mode": config.get("mode", "privacy"), "policy": deepcopy(config.get("policy"))}}
    sample = normalize(source, stable_id("src_", run_id), 0, dumps(source), 1, "openai_messages")
    execution = sample["execution"]
    runtime_calls = {c["call_id"]: c for c in payload.get("calls", [])}
    for call in execution["calls"]:
        actual = runtime_calls.get(call["source_call_id"])
        if not actual:
            # The model requested this action, but the runtime never reached its permission check.
            # Do not turn stopping/budget exhaustion into a fabricated policy refusal.
            call.update(execution_status="unknown", result_code="RUN_LIMIT_OR_STOP", permission_decision="unknown",
                        non_execution_reason="任务停止或达到限额，未进入工具权限检查")
            continue
        start, end = actual.get("started_at_ms"), actual.get("ended_at_ms")
        blocked = actual.get("execution_status") == "not_executed"
        granted = actual.get("granted_scopes", ["dataset:bound", "privacy:sample<=10"])
        requested = actual.get("requested_scopes", ["dataset:bound"])
        decision = actual.get("permission_decision", "unknown")
        call.update(relative_start_ms=start, relative_end_ms=end,
            duration_ms=None if blocked else end-start if end is not None and start is not None else None,
            policy_check_duration_ms=end-start if blocked and end is not None and start is not None else None,
            execution_status=actual["execution_status"] if actual["execution_status"] in {"succeeded", "failed", "not_executed"} else "unknown",
            permission_decision=decision, granted_scopes=granted, requested_scopes=requested)
        if actual.get("result_code"):
            call["result_code"] = actual["result_code"]
        call["analysis"]["permission_comparison"] = {"rows": [{"label": "本次工具权限", "granted": granted, "requested": requested,
            "comparison": "mismatch" if blocked else "match" if decision == "allowed" else "unknown"}],
            "actual_decision": {"message": (actual.get("result") or {}).get("message") or ("后端校验通过，允许执行绑定数据集工具" if decision == "allowed" else "权限判定未完成"),
                "policy_version": actual.get("policy_version"), "rule_id": actual.get("policy_rule_id")}}
    execution.update(origin_type="platform", model=config["model_id"], invocation_id=run_id, task_id=run_id, data_origin="actual")
    execution["source"].update(format_label="内部智能体任务 · 实际记录", filename=run_id + ".json")
    if not batch:  # Backward compatibility for runs created before batch selection was introduced.
        batch = TraceBatch(batch_id=bid, source_system="internal_agent", source_task_id=run_id,
            display_id=run_id, active_snapshot_id="", payload={"name": "内部智能体任务", "batch_version": 1, "data_origin": "actual"})
        db.add(batch)
        db.flush()
    sid = publish(db, batch, publication, [sample], set(), current_read=True)
    batch.payload = {**batch.payload, "batch_version": batch.payload.get("batch_version", 1) + 1}
    row = db.scalar(select(TraceSample).where(TraceSample.snapshot_id == sid, TraceSample.trace_id == trace_id))
    return {"batch_id": bid, "snapshot_id": sid, "trace_id": row.trace_id}
