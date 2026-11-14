"""Batch selection and append-only publications with isolated data and a fake model provider."""
from copy import deepcopy
import json

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select

from test_agent_traces import env
from test_internal_agent import prepared, USER, ROOT, result, native_call
from app.models.agent_traces import TraceBatch, TraceSample, TraceSnapshot, TraceCall, TraceEvent
from app.models.internal_agent import InternalAgentRun
from app.services import internal_agent as service, internal_agent_batches as batches
from app.services import agent_trace_statistics as statistics


def body(**changes):
    return {"prompt": "检查本次测试", "model_id": "deepseek", "dataset_id": 15, **changes}


def create_run(prepared, key, **changes):
    with prepared["sessions"]() as db:
        row, created = service.create(db, service.CreateRun(**body(**changes)), USER, key)
        assert created
        return row.run_id, deepcopy(row.config["trace_batch"])


def final_answer(*args, **kwargs):
    return result({"role": "assistant", "content": "本次任务完成。"})


def test_http_two_tasks_share_one_batch_and_keep_exact_old_links(prepared, monkeypatch):
    requests = []
    def provider(path, request=None, *args, **kwargs):
        # The runtime passes body positionally; no paid request leaves the process.
        requests.append(deepcopy(request))
        if not any(m["role"] == "tool" for m in request["messages"]):
            tool = "export_report" if request["mode"] == "policy_test" else "query_dataset_info"
            return result({"role": "assistant", "tool_calls": [native_call(tool, {}, "same-provider-id")]})
        return final_answer()
    monkeypatch.setattr(service, "llm", provider)
    execute = service.execute
    monkeypatch.setattr(service, "execute", lambda rid, token=None: execute(rid, prepared["sessions"], token))
    client = prepared["client"]
    one_body = body(batch_mode="new", batch_name="  11月权限测试  ")
    first = client.post(ROOT + "/runs", json=one_body, headers={"X-Request-Id": "first-batch"})
    assert first.status_code == 202, first.text
    first_info = first.json()["data"]
    bid = first_info["batch"]["batch_id"]
    assert first_info["batch"]["name"] == "11月权限测试"
    one = client.get(ROOT + "/runs/" + first_info["run_id"]).json()["data"]
    old_scope = one["trace"]
    assert one["status"] == "succeeded" and old_scope
    with prepared["sessions"]() as db:
        old = db.scalar(select(TraceSample).where(TraceSample.snapshot_id == old_scope["snapshot_id"]))
        old_payload = deepcopy(old.payload)
    available = client.get(ROOT + "/batches").json()["data"]
    assert available["items"] == [{**first_info["batch"], "trace_count": 1}]
    second = client.post(ROOT + "/runs", json=body(batch_mode="existing", batch_id=bid, mode="policy_test"),
                         headers={"X-Request-Id": "second-batch"})
    assert second.status_code == 202, second.text
    two = client.get(ROOT + "/runs/" + second.json()["data"]["run_id"]).json()["data"]
    assert two["status"] == "succeeded", two
    latest = two["trace"]
    assert latest["batch_id"] == bid == old_scope["batch_id"]
    assert latest["trace_id"] != old_scope["trace_id"]
    assert latest["snapshot_id"] != old_scope["snapshot_id"]
    assert two["batch"] == one["batch"]
    with prepared["sessions"]() as db:
        batch = db.get(TraceBatch, bid)
        assert batch.active_snapshot_id == latest["snapshot_id"]
        assert db.scalar(select(func.count()).select_from(TraceBatch).where(TraceBatch.source_system == "internal_agent")) == 1
        rows = list(db.scalars(select(TraceSample).where(TraceSample.snapshot_id == latest["snapshot_id"]).order_by(TraceSample.ingest_order)))
        assert [r.trace_id for r in rows] == [old_scope["trace_id"], latest["trace_id"]]
        assert [r.display_id for r in rows] == ["AUD-0001", "AUD-0002"]
        assert rows[0].payload == old_payload
        assert db.scalar(select(TraceSample).where(TraceSample.snapshot_id == old_scope["snapshot_id"])).payload == old_payload
        for model in (TraceEvent, TraceCall):
            before = [r.payload for r in db.scalars(select(model).where(model.snapshot_id == old_scope["snapshot_id"]))]
            after = [r.payload for r in db.scalars(select(model).where(model.snapshot_id == latest["snapshot_id"], model.trace_id == old_scope["trace_id"]))]
            assert before == after
        while statistics.materialize(db):
            db.commit()
        workspace = statistics.workspace(db, {"batch_id": bid}, USER)
        assert workspace["list_counts"]["all"] == 2
        assert workspace["kpis"]["sample_count"]["value"] == 2
    # Re-publishing an earlier task returns its original snapshot, not the newer task's sample.
    retried = client.post(ROOT + "/runs/" + one["run_id"] + "/trace")
    assert retried.status_code == 200, retried.text
    assert retried.json()["data"]["trace"] == old_scope
    with prepared["sessions"]() as db:
        assert db.get(TraceBatch, bid).active_snapshot_id == latest["snapshot_id"]
        assert db.scalar(select(func.count()).select_from(TraceSnapshot).where(TraceSnapshot.batch_id == bid)) == 2
    replayed = client.post(ROOT + "/runs", json=one_body, headers={"X-Request-Id": "first-batch"})
    assert replayed.status_code == 202 and replayed.json()["data"]["run_id"] == one["run_id"]
    assert len(requests) == 4
    history = client.get(f"/api/v1/compliance/agent-traces/batches/{bid}/samples/{old_scope['trace_id']}",
                         params={"view": "execution", **old_scope})
    assert history.status_code == 200, history.text
    assert history.json()["data"]["trace_id"] == old_scope["trace_id"]


def test_foreign_and_external_batches_are_not_joinable(prepared, monkeypatch):
    with prepared["sessions"]() as db:
        mine = batches.new_batch(db, "mine-run", USER["uid"], "我的批次")
        foreign = batches.new_batch(db, "foreign-run", 2, "其他人的批次")
        upload = TraceBatch(batch_id="upload-batch", source_system="upload", source_task_id="upload-batch", display_id="UPLOAD", active_snapshot_id="", payload={"owner_id": 1, "name": "外部轨迹"})
        db.add(upload)
        db.commit()
        for invalid in (foreign.batch_id, upload.batch_id, "missing"):
            with pytest.raises(HTTPException) as exc:
                service.create(db, service.CreateRun(**body(batch_mode="existing", batch_id=invalid)), USER, invalid)
            assert exc.value.status_code == 404
        assert [b["batch_id"] for b in batches.options(db, USER)["items"]] == [mine.batch_id]
        assert db.scalar(select(func.count()).select_from(InternalAgentRun)) == 0
    monkeypatch.setattr(service, "models", lambda *a, **k: pytest.fail("批次无效时不应访问模型服务"))
    response = prepared["client"].post(ROOT + "/runs", json=body(batch_mode="existing", batch_id=foreign.batch_id), headers={"X-Request-Id": "foreign"})
    assert response.status_code == 404


def test_search_pagination_and_empty_batch_destinations(prepared):
    with prepared["sessions"]() as db:
        for i in range(24):
            batches.new_batch(db, f"page-run-{i}", USER["uid"], f"权限测试 {i:02}")
        literal = batches.new_batch(db, "literal", USER["uid"], "测试_100%")
        db.commit()
        first = batches.options(db, USER)
        second = batches.options(db, USER, page=2)
        assert first["total"] == 25 and len(first["items"]) == 20 and len(second["items"]) == 5
        assert not {b["batch_id"] for b in first["items"]} & {b["batch_id"] for b in second["items"]}
        assert all(b["trace_count"] == 0 for b in first["items"])
        assert batches.options(db, USER, search="_100%")["items"][0]["batch_id"] == literal.batch_id
        assert batches.options(db, USER, search="no-match")["total"] == 0
    assert prepared["client"].get(ROOT + "/batches", params={"page": 0}).status_code == 422
    assert prepared["client"].get(ROOT + "/batches", params={"page_size": 100}).status_code == 422


def test_legacy_batches_remain_joinable_without_rewriting_old_runs(prepared, monkeypatch):
    rid, group = create_run(prepared, "legacy")
    with prepared["sessions"]() as db:
        run = db.get(InternalAgentRun, rid)
        run.config = {k: v for k, v in run.config.items() if k not in {"trace_batch", "batch_mode", "batch_name", "batch_id"}}
        db.delete(db.get(TraceBatch, group["batch_id"]))
        db.commit()
    monkeypatch.setattr(service, "llm", final_answer)
    service.execute(rid, prepared["sessions"])
    with prepared["sessions"]() as db:
        old = db.get(InternalAgentRun, rid)
        scope = deepcopy(old.payload["trace"])
        assert db.get(TraceBatch, scope["batch_id"]).payload.get("owner_id") is None
        assert batches.options(db, USER)["items"][0]["batch_id"] == scope["batch_id"]
    next_id, _ = create_run(prepared, "join-legacy", batch_mode="existing", batch_id=scope["batch_id"])
    service.execute(next_id, prepared["sessions"])
    with prepared["sessions"]() as db:
        old = db.get(InternalAgentRun, rid)
        assert "trace_batch" not in old.config
        assert service.public(old)["batch"]["batch_id"] == scope["batch_id"]
        assert old.payload["trace"] == scope
        assert batches.options(db, USER)["items"][0]["trace_count"] == 2


@pytest.mark.parametrize("ending", ["failed", "cancelled"])
def test_failed_or_stopped_task_still_appends_its_own_trace(prepared, monkeypatch, ending):
    first, group = create_run(prepared, "first", batch_name="失败记录测试")
    monkeypatch.setattr(service, "llm", final_answer)
    service.execute(first, prepared["sessions"])
    second, _ = create_run(prepared, "second", batch_mode="existing", batch_id=group["batch_id"])
    if ending == "cancelled":
        with prepared["sessions"]() as db:
            db.get(InternalAgentRun, second).stop_requested = True
            db.commit()
    else:
        monkeypatch.setattr(service, "llm", lambda *a, **k: (_ for _ in ()).throw(HTTPException(503, "测试失败")))
    service.execute(second, prepared["sessions"])
    with prepared["sessions"]() as db:
        run = db.get(InternalAgentRun, second)
        assert run.status == ending and run.payload["trace"]["batch_id"] == group["batch_id"]
        assert batches.options(db, USER)["items"][0]["trace_count"] == 2


def test_invalid_or_repeated_creation_does_not_leave_extra_batches(prepared):
    with prepared["sessions"]() as db:
        for invalid in (body(dataset_id=16, batch_name="无效"), body(model_id="unknown", batch_name="无效")):
            with pytest.raises(HTTPException):
                service.create(db, service.CreateRun(**invalid), USER, "invalid")
            db.rollback()
        assert batches.options(db, USER)["total"] == 0
        data = service.CreateRun(**body(batch_name="第一批"))
        first, _ = service.create(db, data, USER, "create-once")
        assert service.create(db, data, USER, "create-once")[0].run_id == first.run_id
        with pytest.raises(HTTPException) as exc:
            service.create(db, service.CreateRun(**body(batch_name="第二批")), USER, "different-active")
        assert exc.value.status_code == 409
        db.rollback()
        with pytest.raises(HTTPException) as conflict:
            service.create(db, service.CreateRun(**body(batch_name="第二批")), USER, "create-once")
        assert conflict.value.status_code == 409
        assert batches.options(db, USER)["total"] == 1


@pytest.mark.parametrize("choice", [
    {"batch_mode": "existing"}, {"batch_mode": "existing", "batch_id": "x", "batch_name": "不能同时新建"},
    {"batch_mode": "new", "batch_id": "x"}, {"batch_mode": "invalid"},
    {"batch_name": "  "}, {"batch_id": "  ", "batch_mode": "existing"}, {"batch_name": "x" * 101},
])
def test_invalid_batch_choices_are_rejected(choice):
    with pytest.raises(ValidationError):
        service.CreateRun(**body(**choice))
