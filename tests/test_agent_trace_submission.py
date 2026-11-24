"""Reference-answer download fields, including opaque source JSON and evidence."""
from copy import deepcopy
import json

import pytest

from test_agent_traces import env, ROOT
from test_agent_trace_workflow import upload
from app.services.agent_trace_competition import NAMES
from app.services.agent_trace_submission import submission_rows


def reference_rows():
    evidence = [{"step_id": 2, "message_index": 5, "quote": "完整原文😀\n" * 2000}]
    results = {
        "A1": {"input_tokens": 0, "output_tokens": "未提供"},
        "A2": {"known_characters": 2, "absent_body_step_ids": [2]},
        "A3": {"tool_calls": 1, "tool_kinds": 1, "tool_counts": {"event_id": 1}},
        "B1": {"segments": [{"step_id": 1, "text": "思考", "step_ids": [1, 2]}], "absent_body_step_ids": [2]},
        "B2": {"judgment": "有风险"},
        "B3": {"risks": [{"step_id": 2, "risk_quote": "风险片段", "evidence": evidence}]},
        "C1": {"calls": [{"step_id": 2, "tool_name": "event_id",
                          "arguments": {"event_id": "原始参数", "rule_id": "用户字段"},
                          "returns": [{"content": {"event_id": "原始返回", "analysis_version": 0}}]}], "unmatched": []},
        "C2": {"markers": [{"step_id": 2, "previous_step_id": 1, "evidence": evidence}]},
        "C3": {"failures": [{"step_id": 2, "error_type": "执行超时", "evidence": evidence}]},
        "C4": {"missing_fields": [], "unmatched": [], "truncations": [
            {"step_id": 2, "message_index": 5, "quote": "...[truncated 11 chars]"}], "completion": None},
        "C5": {"issues": [{"source_subitem": "C3", "step_id": 2, "evidence": evidence},
                          {"source_subitem": "C4", "step_id": 2, "message_index": 5, "evidence": evidence}]},
    }
    for key, value in results.items():
        value["指标"] = NAMES[key]
    return [{"trajectory_id": "原始编号", "results": results},
            {"trajectory_id": "aggregate", "results": {"A4": {"指标": NAMES["A4"],
             "trajectory_ids": ["原始编号"], "tool_calls": 1, "tool_counts": {"event_id": 1}}}}]


def test_reference_fields_and_complete_raw_values_survive_projection():
    expected = reference_rows()
    stored = deepcopy(expected)
    stored[0].update(analysis_version="competition-v5", report_kind="ordinary")
    r = stored[0]["results"]
    r["B1"]["segments"][0].update(message_index=0, event_id="event", source_ref={"path": "private"})
    r["B3"].update(first_risk_step=2)
    r["B3"]["risks"][0].update(rule_id="rule", label="内部标签", severity="high", outcome="recorded")
    for key, collection in (("B3", "risks"), ("C2", "markers"), ("C3", "failures"), ("C5", "issues")):
        for item in r[key][collection]:
            item.update(event_id="event", event_ordinal=2, rule_id="rule")
            for evidence in item["evidence"]:
                evidence.update(source_ref={"path": "private"}, event_id="event", call_id="call")
    call = r["C1"]["calls"][0]
    call.update(message_index=4, tool_call_id="call", call_index=0, call_id="internal")
    call["returns"][0].update(message_index=5, event_id="event")
    r["C4"]["truncations"][0].update(field="content", event_id="event")
    before = deepcopy(stored)
    actual = submission_rows(stored)
    assert actual == expected
    assert stored == before
    assert submission_rows(actual) == expected
    # The two existing browser downloads differ only in serialization.
    jsonl = "\n".join(json.dumps(row, ensure_ascii=False) for row in actual) + "\n"
    readable = json.dumps([json.loads(line) for line in jsonl.splitlines()], ensure_ascii=False, indent=2) + "\n"
    assert json.loads(readable) == expected
    assert len(jsonl.splitlines()) == 2
    assert "完整原文😀" in readable


def test_current_report_endpoint_exports_fields_without_rerunning_analysis(env, tmp_path, monkeypatch):
    original_return = "...[truncated 11 chars]\n" + "完整原文😀" * 2000
    record = {"trajectory_id": "原始编号", "messages": [
        {"role": "assistant", "reasoning_content": "原始思考", "tool_calls": [
            {"id": "call", "function": {"name": "bash", "arguments": '{"command":"cat /etc/shadow"}'}}]},
        {"role": "tool", "tool_call_id": "call", "content": original_return},
        {"role": "assistant", "tool_calls": [{"id": "timeout", "function": {
            "name": "bash", "arguments": '{"command":"timeout 60s python analysis.py"}'}}]},
        {"role": "tool", "tool_call_id": "timeout", "content": "Command exited with code 124"},
    ]}
    batch, _ = upload(env, tmp_path, [record])
    monkeypatch.setattr("app.services.agent_trace_competition.analyze", lambda *a, **k: pytest.fail("re-audited"))
    response = env["client"].get(f"{ROOT}/batches/{batch}/competition-report")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    sample, aggregate = [json.loads(line) for line in data["jsonl"].splitlines()]
    assert data["analysis_version"] == "competition-v5"
    assert set(sample) == set(aggregate) == {"trajectory_id", "results"}
    r = sample["results"]
    assert r["B1"]["segments"] == [{"step_id": 1, "step_ids": [1], "text": "原始思考"}]
    assert r["B3"]["risks"]
    assert all(set(risk) == {"step_id", "risk_quote", "evidence"} for risk in r["B3"]["risks"])
    assert r["C1"]["calls"] == [{"step_id": 1, "tool_name": "bash",
                                    "arguments": '{"command":"cat /etc/shadow"}',
                                    "returns": [{"content": original_return}]},
                                   {"step_id": 2, "tool_name": "bash",
                                    "arguments": '{"command":"timeout 60s python analysis.py"}',
                                    "returns": [{"content": "Command exited with code 124"}]}]
    assert r["C4"]["truncations"] == [{"step_id": 1, "message_index": 1, "quote": "...[truncated 11 chars]"}]
    assert r["C3"]["failures"]
    assert all(set(failure) == {"step_id", "error_type", "evidence"} for failure in r["C3"]["failures"])
    assert all(set(issue) <= {"source_subitem", "step_id", "message_index", "evidence"} for issue in r["C5"]["issues"])
    assert aggregate["results"]["A4"]["trajectory_ids"] == ["原始编号"]
