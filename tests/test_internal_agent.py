"""No paid requests: real runtime, real HTTP authorization, isolated SQLite, controlled provider."""
import json
from copy import deepcopy
from time import monotonic

import pytest
from fastapi import HTTPException
from sqlalchemy import select, func
from pydantic import ValidationError

from test_agent_traces import env
from app.models.tables import Dataset, DatasetRecord, Task
from app.models.agent_traces import TraceCall, TraceEvent, TraceBatch, TraceSample, TraceArtifact
from app.models.internal_agent import InternalAgentRun
from app.services import internal_agent as service

USER = {"uid": 1, "tier": "operator"}
ROOT = "/api/v1/internal-agent"


@pytest.fixture
def prepared(env, monkeypatch):
    monkeypatch.delenv("INTERNAL_AGENT_DATASET_IDS", raising=False)
    monkeypatch.setattr(service, "models", lambda token=None: [{"id": "deepseek", "name": "DeepSeek", "upstream_model_id": "deepseek-chat"}])
    with env["sessions"]() as db:
        db.add(Dataset(id=15, name="测试数据集", version="v1"))
        db.add(Dataset(id=16, name="大数据集", version="v1"))
        db.add_all([DatasetRecord(dataset_id=15, task_id="seed", payload={"text": f"测试正文 {i} person@example.com"}) for i in range(30)])
        db.add_all([DatasetRecord(dataset_id=16, task_id="seed", payload={"text": "不应读取"}) for _ in range(101)])
        db.commit()
    yield env


def create(env, **changes):
    with env["sessions"]() as db:
        body = service.CreateRun(prompt="检测隐私并总结", model_id="deepseek", dataset_id=15, **changes)
        row, _ = service.create(db, body, USER, "key-1")
        return row.run_id


def result(message):
    return {"message": message, "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}, "duration_ms": 2}


def calls(*names):
    return {"role": "assistant", "content": None, "tool_calls": [{"id": "id-" + name, "type": "function", "function": {"name": name, "arguments": "{}"}} for name in names]}


def test_limits_freeze_idempotency_and_dataset_allowlist(prepared):
    with prepared["sessions"]() as db:
        assert [r["id"] for r in service.datasets(db)] == [15]
        body = service.CreateRun(prompt="检查", model_id="deepseek", dataset_id=15)
        row, created = service.create(db, body, USER, "same")
        assert created and len(row.config["samples"]) == 10
        assert service.create(db, body, USER, "same")[1] is False
        with pytest.raises(HTTPException) as exc:
            service.create(db, body.model_copy(update={"prompt": "不同内容"}), USER, "same")
        assert exc.value.status_code == 409
        with pytest.raises(HTTPException):
            service.create(db, body.model_copy(update={"dataset_id": 16}), USER, "big")
        # No dynamic reading after creation.
        first = db.scalar(select(DatasetRecord).where(DatasetRecord.dataset_id == 15))
        first.payload = {"text": "后续修改"}
        db.commit()
        assert "后续修改" not in json.dumps(row.config)
    with pytest.raises(ValidationError):
        service.CreateRun(prompt="检查", model_id="deepseek", dataset_id=15, sample_limit=11)


def test_real_runtime_saves_trace_and_one_bounded_privacy_request(prepared, monkeypatch):
    rid = create(prepared)
    observed = []
    turn = 0
    def provider(path, body=None, timeout=40, token=None):
        nonlocal turn
        observed.append((path, deepcopy(body)))
        if path.endswith("/privacy"):
            assert len(body["samples"]) == 10
            return {**result({"role": "assistant", "content": "实际检测响应"}), "request_messages": [],
                "samples": [{"id": s["id"], "status": "risk", "reason": "含邮箱", "quotes": ["person@example.com"]} for s in body["samples"]]}
        turn += 1
        if turn == 1:
            return result(calls("query_dataset_info", "submit_privacy_check"))
        if turn == 2:
            return result({"role": "assistant", "content": None, "tool_calls": [
                {"id": "repeat", "type": "function", "function": {"name": "submit_privacy_check", "arguments": "{}"}},
                {"id": "query", "type": "function", "function": {"name": "query_privacy_check", "arguments": json.dumps({"check_id": "AP-" + rid[3:]})}}]})
        return result({"role": "assistant", "content": "本次检测10条，发现隐私风险；不代表整个数据集。"})
    monkeypatch.setattr(service, "llm", provider)
    service.execute(rid, prepared["sessions"])
    with prepared["sessions"]() as db:
        row = db.get(InternalAgentRun, rid)
        assert row.status == "succeeded", row.payload
        assert row.payload["trace"]
        assert sum(p.endswith("/privacy") for p, _ in observed) == 1
        assert db.scalar(select(func.count()).select_from(Task).where(Task.capability_code == "agent_privacy_check")) == 1
        public = service.public(row, True)
        assert public["token_usage"]["total_tokens"] == 480
        assert "person@example.com" not in json.dumps(public)
        scope = row.payload["trace"]
        audit_calls = list(db.scalars(select(TraceCall).where(TraceCall.snapshot_id == scope["snapshot_id"])))
        assert len(audit_calls) == 4
        assert all(c.payload["data_origin"] == "actual" and c.payload["duration_ms"] is not None for c in audit_calls)
        assert service.publish_trace(db, row, row.payload) == scope
    before = len(observed)
    service.execute(rid, prepared["sessions"])
    assert len(observed) == before
    # Existing audit routes accept the exact published deep link.
    monkeypatch.setenv("AGENT_TRACE_ENABLE_LEGACY_VALUE_SYNC", "false")
    response = prepared["client"].get("/api/v1/compliance/agent-traces/workspace", params={"view": "execution", "include": "dashboard", **scope})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["selected_trace_id"] == scope["trace_id"]


@pytest.mark.parametrize("mode", ["stop", "failure", "limit"])
def test_stop_failure_and_limits_preserve_partial_records(prepared, monkeypatch, mode):
    rid = create(prepared, max_rounds=1)
    if mode == "stop":
        with prepared["sessions"]() as db:
            row = db.get(InternalAgentRun, rid)
            row.stop_requested = True
            db.commit()
    requests = []
    def provider(*args, **kwargs):
        requests.append(args)
        if mode == "failure":
            raise HTTPException(503, "测试上游失败")
        return result(calls("query_dataset_info"))
    monkeypatch.setattr(service, "llm", provider)
    service.execute(rid, prepared["sessions"])
    with prepared["sessions"]() as db:
        row = db.get(InternalAgentRun, rid)
        assert row.status == {"stop": "cancelled", "failure": "failed", "limit": "limited"}[mode]
        assert row.payload["trace"]
        if mode == "stop":
            assert not requests
        assert not row.payload.get("answer")


def test_tool_scope_and_cross_owner_are_enforced(prepared):
    rid = create(prepared)
    with prepared["sessions"]() as db:
        row = db.get(InternalAgentRun, rid)
        with pytest.raises(HTTPException):
            service.tool(db, row, row.payload, "submit_privacy_check", {"dataset_id": 16}, monotonic()+10)
        with pytest.raises(HTTPException):
            service.get_run(db, rid, {"uid": 2})
        with pytest.raises(HTTPException):
            service.tool(db, row, row.payload, "query_privacy_check", {"check_id": "foreign"}, monotonic()+10)


def test_restart_does_not_replay_and_trace_can_be_republished(prepared, monkeypatch):
    rid = create(prepared)
    monkeypatch.setattr(service, "llm", lambda *a, **k: pytest.fail("恢复不应调用模型"))
    with prepared["sessions"]() as db:
        service.recover(db)
        row = db.get(InternalAgentRun, rid)
        assert row.status == "interrupted" and row.payload["trace"]
        before = db.scalar(select(func.count()).select_from(TraceBatch))
    response = prepared["client"].post(f"{ROOT}/runs/{rid}/trace")
    assert response.status_code == 200, response.text
    with prepared["sessions"]() as db:
        assert db.scalar(select(func.count()).select_from(TraceBatch)) == before


def test_http_options_create_and_stop(prepared, monkeypatch):
    monkeypatch.setattr(service, "execute", lambda *a, **k: None)
    client = prepared["client"]
    assert client.get(ROOT + "/options").json()["data"]["datasets"][0]["id"] == 15
    response = client.post(ROOT + "/runs", headers={"X-Request-Id": "http"}, json={"prompt": "检查", "model_id": "deepseek", "dataset_id": 15})
    assert response.status_code == 202, response.text
    rid = response.json()["data"]["run_id"]
    assert client.get(ROOT + "/runs").json()["data"]["items"][0]["run_id"] == rid
    assert client.post(f"{ROOT}/runs/{rid}/stop").json()["data"]["stop_requested"] is True


def test_timeout_and_missing_usage_are_not_success(prepared):
    rid = create(prepared)
    with prepared["sessions"]() as db:
        row = db.get(InternalAgentRun, rid)
        with pytest.raises(service.EndRun) as exc:
            service.check(db, row, monotonic()-1)
        assert exc.value.status == "timed_out"
    assert service.usage([{"usage": None}])["total_tokens"] is None


def native_call(name, arguments, identity):
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def test_policy_mode_records_actual_denials_in_audit_without_export_or_detection(prepared, monkeypatch):
    # A long source sample must not be read/frozen or sent for this mode.
    with prepared["sessions"]() as db:
        record = db.scalar(select(DatasetRecord).where(DatasetRecord.dataset_id == 15))
        record.payload = {"text": "不读取正文" * 1000}
        db.commit()
    rid = create(prepared, mode="policy_test")
    provider_requests = []
    before_files = set(prepared["tmp"].rglob("*"))
    def provider(path, body=None, timeout=40, token=None):
        assert path == "/agent-runtime/turn", "策略演示不能新建付费隐私检测"
        assert body["mode"] == "policy_test"
        assert "不读取正文" not in json.dumps(body, ensure_ascii=False)
        provider_requests.append(deepcopy(body))
        if len(provider_requests) == 1:
            return result({"role": "assistant", "tool_calls": [
                native_call("query_dataset_info", {}, "q1"),
                native_call("query_existing_privacy_checks", {}, "q2"),
                native_call("export_report", {"report_ref": "current_summary", "format": "json"}, "e1"),
                native_call("send_email", {"report_ref": "current_summary", "to": "audit-demo@example.com"}, "m1"),
            ]})
        return result({"role": "assistant", "content": "查询成功，导出和外发均被后端策略拒绝，未实际执行。"})
    monkeypatch.setattr(service, "llm", provider)
    service.execute(rid, prepared["sessions"])
    with prepared["sessions"]() as db:
        row = db.get(InternalAgentRun, rid)
        assert row.status == "succeeded", row.payload
        assert row.config["samples"] == []
        assert row.payload["trace"]
        report = service.public(row, True)
        assert report["mode"] == "policy_test" and report["blocked_count"] == 2
        assert len(row.payload["calls"]) == 4
        blocked = row.payload["calls"][2:]
        for call in blocked:
            assert call["execution_status"] == "not_executed"
            assert call["permission_decision"] == "denied"
            assert call["result"]["status"] == "blocked"
            assert call["result"]["error_code"] == "POLICY_DENIED"
            assert call["result"]["policy_version"] == service.POLICY_VERSION
        assert db.scalar(select(func.count()).select_from(Task).where(Task.capability_code == "agent_privacy_check")) == 0
        assert db.scalar(select(func.count()).select_from(TraceArtifact)) == 0
        scope = row.payload["trace"]
        saved = list(db.scalars(select(TraceCall).where(TraceCall.snapshot_id == scope["snapshot_id"]).order_by(TraceCall.ordinal)))
        for call in saved[2:]:
            assert call.payload["execution_status"] == "not_executed"
            assert call.payload["duration_ms"] is None
            assert call.payload["result_code"] == "POLICY_DENIED"
            assert call.payload["permission_decision"] == "denied"
            assert call.payload["analysis"]["permission_comparison"]["rows"][0]["comparison"] == "mismatch"
        sample = db.scalar(select(TraceSample).where(TraceSample.snapshot_id == scope["snapshot_id"]))
        findings = sample.payload["rule_findings"]
        assert sum(f["rule_id"] == "policy_denied" for f in findings) == 2
        assert sum(f["rule_id"] == "prohibited_request" for f in findings) == 2
        assert not any(f["category"] == "operation" for f in findings)
    assert set(prepared["tmp"].rglob("*")) == before_files
    assert len(provider_requests) == 2
    monkeypatch.setenv("AGENT_TRACE_ENABLE_LEGACY_VALUE_SYNC", "false")
    tools = prepared["client"].get(f"/api/v1/compliance/agent-traces/batches/{scope['batch_id']}/samples/{scope['trace_id']}",
        params={"view": "tools", **scope})
    assert tools.status_code == 200, tools.text
    metrics = tools.json()["data"]["metrics"]
    assert metrics["call_count"] == 4 and metrics["not_executed_count"] == 2
    assert metrics["success_count"] == 2 and metrics["error_count"] == 0


def test_policy_mode_blocks_unoffered_privacy_tool_and_cannot_grant_permissions(prepared):
    rid = create(prepared, mode="policy_test")
    with prepared["sessions"]() as db:
        row = db.get(InternalAgentRun, rid)
        with pytest.raises(service.PolicyDenied) as exc:
            service.tool(db, row, row.payload, "submit_privacy_check", {}, monotonic()+10)
        assert exc.value.decision["policy"] == "privacy:submit"
        assert db.scalar(select(func.count()).select_from(Task).where(Task.capability_code == "agent_privacy_check")) == 0
        # Even a future grant configuration must never enable real export/mail in these test entry points.
        row.config = {**row.config, "policy": {**row.config["policy"], "allowed_scopes": list(service.TOOL_SCOPES.values())}}
        with pytest.raises(service.PolicyDenied):
            service.tool(db, row, row.payload, "export_report", {}, monotonic()+10)
        with pytest.raises(ValidationError):
            service.tool(db, row, row.payload, "send_email", {"to": "someone@real.invalid"}, monotonic()+10)
    with pytest.raises(ValidationError):
        service.CreateRun(prompt="测试", model_id="deepseek", dataset_id=15, mode="policy_test", policy={"allowed_scopes": ["email.send"]})
    with pytest.raises(ValidationError):
        service.CreateRun(prompt="测试", model_id="deepseek", dataset_id=15, mode="unsupported")


def test_policy_mode_never_fabricates_calls_not_requested_by_model(prepared, monkeypatch):
    rid = create(prepared, mode="policy_test")
    monkeypatch.setattr(service, "llm", lambda *a, **kw: result({"role": "assistant", "content": "本次没有提出导出或外发请求，未测试。"}))
    service.execute(rid, prepared["sessions"])
    with prepared["sessions"]() as db:
        row = db.get(InternalAgentRun, rid)
        assert row.status == "succeeded"
        assert service.public(row)["blocked_count"] == 0 and row.payload["calls"] == []
        assert db.scalar(select(func.count()).select_from(TraceCall).where(TraceCall.snapshot_id == row.payload["trace"]["snapshot_id"])) == 0


def test_options_expose_distinct_mode_tools(prepared):
    options = prepared["client"].get(ROOT + "/options").json()["data"]
    modes = {m["code"]: m for m in options["modes"]}
    assert "submit_privacy_check" in modes["privacy"]["tools"]
    assert "export_report" not in modes["privacy"]["tools"]
    assert "submit_privacy_check" not in modes["policy_test"]["tools"]
    assert {"export_report", "send_email"} <= set(modes["policy_test"]["tools"])
    assert "email.send" not in modes["policy_test"]["allowed_scopes"]


def test_budget_stop_is_not_published_as_policy_denial(prepared, monkeypatch):
    rid = create(prepared, mode="policy_test", max_calls=1)
    monkeypatch.setattr(service, "llm", lambda *a, **kw: result({"role": "assistant", "tool_calls": [
        native_call("query_dataset_info", {}, "q"),
        native_call("export_report", {"format": "json"}, "e"),
        native_call("send_email", {"to": "audit-demo@example.com"}, "m"),
    ]}))
    service.execute(rid, prepared["sessions"])
    with prepared["sessions"]() as db:
        row = db.get(InternalAgentRun, rid)
        assert row.status == "limited" and service.public(row)["blocked_count"] == 0
        sample = db.scalar(select(TraceSample).where(TraceSample.snapshot_id == row.payload["trace"]["snapshot_id"]))
        assert not any(f["rule_id"] == "policy_denied" for f in sample.payload["rule_findings"])
