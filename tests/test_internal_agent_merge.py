from copy import deepcopy

from sqlalchemy import select, func

from test_agent_traces import env
from test_internal_agent import prepared, USER, result, native_call
from test_internal_agent_batches import create_run
from app.models.agent_traces import TraceSample, TraceCall, TraceEvent, TraceEvidence, TraceSnapshot
from app.models.internal_agent import InternalAgentRun
from app.services import internal_agent as service, agent_trace_statistics as stats, agent_trace_query as query
from app.services.internal_agent_merge import merge_runs
from app.services.internal_agent_batches import options


def test_merge_keeps_history_and_evidence_and_retries_do_not_duplicate(prepared, monkeypatch):
    monkeypatch.setenv("AGENT_TRACE_ENABLE_LEGACY_VALUE_SYNC", "false")
    def provider(path, body=None, *args, **kwargs):
        if not any(m["role"] == "tool" for m in body["messages"]):
            return result({"role": "assistant", "tool_calls": [native_call("export_report", {}, "export")]})
        return result({"role": "assistant", "content": "请求已拦截"})
    monkeypatch.setattr(service, "llm", provider)
    ids, originals = [], {}
    for i in range(3):
        rid, _ = create_run(prepared, f"merge-{i}", mode="policy_test")
        service.execute(rid, prepared["sessions"])
        ids.append(rid)
        with prepared["sessions"]() as db:
            originals[rid] = deepcopy(db.get(InternalAgentRun, rid).payload["trace"])
    with prepared["sessions"]() as db:
        while stats.materialize(db): db.commit()
        plan = merge_runs(db, ids, "内部智能体测试批次")
        assert not plan["applied"] and options(db, USER)["total"] == 3
        merged = merge_runs(db, ids, "内部智能体测试批次", apply=True)
        db.commit()
        sid, bid = merged["snapshot_id"], merged["batch_id"]
        assert options(db, USER)["total"] == 1 and options(db, USER)["items"][0]["trace_count"] == 3
        assert stats.workspace(db, {"batch_id": bid}, USER)["list_counts"]["all"] == 3
        assert len(query.workspace(db, {"include": "options"}, "tools", USER)["batch_options"]["items"]) == 1
        for rid in ids:
            run = db.get(InternalAgentRun, rid)
            old = originals[rid]
            assert run.payload["trace"]["batch_id"] == bid and run.config["trace_batch"]["name"] == "内部智能体测试批次"
            for model in (TraceEvent, TraceCall, TraceEvidence):
                before = [r.payload for r in db.scalars(select(model).where(model.snapshot_id == old["snapshot_id"], model.trace_id == old["trace_id"]))]
                after = [r.payload for r in db.scalars(select(model).where(model.snapshot_id == sid, model.trace_id == old["trace_id"]))]
                assert before and before == after
            assert query.resolve(db, old["batch_id"], old)[1]["snapshot_id"] == old["snapshot_id"]
            assert service.publish_trace(db, run, run.payload) == run.payload["trace"]
        assert db.scalar(select(func.count()).select_from(TraceSample).where(TraceSample.snapshot_id == sid)) == 3
        assert db.scalar(select(func.count()).select_from(TraceSnapshot).where(TraceSnapshot.batch_id == bid)) == 1
        assert merge_runs(db, ids, "内部智能体测试批次", apply=True)["already_merged"]
