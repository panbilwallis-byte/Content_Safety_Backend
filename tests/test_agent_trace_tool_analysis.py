from copy import deepcopy

import pytest

from app.services.agent_trace_tool_analysis import explain_calls, outcome_reason, efficiency_groups
from app.models.agent_traces import TraceSample, TraceCall
from sqlalchemy import select
from test_agent_traces import env, detail


def call(cid="call-1", **changes):
    return {"call_id": cid, "tool_name": "search", "input_json": '{"q":"hello"}',
            "execution_status": "unknown", "permission_decision": "unknown", "result_code": "NOT_RECORDED",
            "analysis": {"permission_comparison": {"actual_decision": {"message": "源日志没有记录权限判定"}},
                         "error_attribution": None}, **changes}


def test_denial_preserves_runtime_reason_without_mutating_record():
    c = call(execution_status="not_executed", permission_decision="denied", result_code="POLICY_DENIED")
    c["analysis"]["permission_comparison"]["actual_decision"]["message"] = "未授权 restricted.export，未生成导出文件"
    before = deepcopy(c)
    result = explain_calls([c])[0]["analysis"]["outcome_reason"]
    assert result["kind"] == "policy_denied"
    assert "restricted.export" in result["message"] and "本次工具未执行" in result["message"]
    assert c == before


@pytest.mark.parametrize("status,code,expected", [
    ("succeeded", "OK", "调用执行成功"),
    ("failed", "TIMEOUT", "源日志未提供原因（错误码：TIMEOUT）"),
    ("unknown", "NOT_RECORDED", "源日志未提供原因"),
    ("not_executed", "RUN_LIMIT_OR_STOP", "工具未执行，源日志未提供原因"),
])
def test_statuses(status, code, expected):
    assert outcome_reason(call(execution_status=status, result_code=code))["message"] == expected


def test_denial_never_implies_action_did_not_run_when_status_is_unknown():
    result = outcome_reason(call(permission_decision="denied", granted_scopes=["dataset:read"],
                                 requested_scopes=["restricted.export"]))
    assert "restricted.export" in result["message"]
    assert "未执行" not in result["message"]


def finding(cid="call-2", rule="repeated_request"):
    return {"category": "efficiency", "rule_id": rule, "call_id": cid, "evidence_text": "参数相同"}


def test_repeated_parameters_and_retry_are_distinct_and_deduplicated():
    calls = [call(), call("call-2"), call("call-3", parent_call_id="call-2")]
    payload = {"rule_findings": [finding(), finding(), finding(rule="recorded_duplicate"),
                                finding("call-3"), finding("call-3", "recorded_retry")]}
    before = deepcopy(payload)
    groups = efficiency_groups(payload, calls)
    assert len(groups) == 2
    assert groups[0]["canonical_call_id"] == "call-1"
    assert groups[0]["duplicate_call_ids"] == ["call-2"]
    assert groups[0]["kind"] == "duplicate" and "不直接判定低效" in groups[0]["reason"]
    assert groups[1]["canonical_call_id"] == "call-2" and groups[1]["kind"] == "retry"
    assert payload == before


def test_legacy_groups_do_not_duplicate_findings_or_invent_relations():
    payload = {"inefficiency_groups": [{"canonical_call_id": "call-1", "duplicate_call_ids": ["call-2", "call-2"],
                                       "reason": "已有明确低效依据"}],
               "rule_findings": [finding(), finding("call-3", "recorded_retry"), finding("absent")]}
    groups = efficiency_groups(payload, [call(), call("call-2"), call("call-3")])
    assert len(groups) == 2 and groups[0]["kind"] == "inefficient"
    assert groups[0]["duplicate_call_ids"] == ["call-2"]
    assert groups[1]["canonical_call_id"] == "call-3" and groups[1]["duplicate_call_ids"] == []
    assert efficiency_groups({}, [call(permission_decision="denied")]) == []


def test_unified_explanations_reach_overview_selection_and_cached_call_rows(env):
    with env["sessions"]() as db:
        sample = db.scalar(select(TraceSample).where(TraceSample.trace_id == env["trace"]))
        rows = list(db.scalars(select(TraceCall).where(TraceCall.trace_id == env["trace"]).order_by(TraceCall.ordinal)))
        first, second = rows[:2]
        second.payload = {**second.payload, "tool_name": first.payload["tool_name"], "input_json": first.payload["input_json"]}
        blocked = next(row for row in rows if row.payload["execution_status"] == "not_executed")
        data = deepcopy(blocked.payload)
        data["analysis"]["error_attribution"] = None
        data["analysis"]["permission_comparison"]["actual_decision"]["message"] = "当前任务未授权 restricted.export"
        blocked.payload = data
        sample.payload = {**sample.payload, "inefficiency_groups": [], "rule_findings": [finding(second.call_id)]}
        db.commit()
        cid = blocked.call_id
    response = detail(env).json()["data"]
    assert response["selection"]["analysis"]["outcome_reason"]["kind"] == "policy_denied"
    assert response["sample_analysis"]["inefficiency_groups"][0]["duplicate_call_ids"] == [second.call_id]
    for section in ("selection", "calls", "sequence"):
        d = detail(env, section=section, call_id=cid).json()["data"]
        selected = d["selection"] if section == "selection" else next(c for c in d[section]["items"] if c["call_id"] == cid)
        assert "restricted.export" in selected["analysis"]["outcome_reason"]["message"]
