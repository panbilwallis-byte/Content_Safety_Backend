"""Tool result statuses reflect actual returns, not keyword mentions inside documents."""
from copy import deepcopy

import pytest
from sqlalchemy import select

from test_agent_traces import env, ROOT
from test_agent_trace_workflow import upload, USER
from app.models.agent_traces import TraceCall, TraceSample
from app.services import agent_trace_query as q, agent_trace_statistics as stats
from app.services.agent_trace_call_status import outcome, project_calls
from app.services.agent_trace_ingest import normalize


@pytest.mark.parametrize("output,expected", [
    ("files/\nREADME.md", "succeeded"), ("Successfully wrote 10 bytes to solution.py", "succeeded"),
    ('{"data":{"status":"error","error_code":"EXAMPLE"}}', "succeeded"),
    ('{"execution_status":"succeeded"}', "succeeded"),
    ('{"status":"ok"}', "succeeded"), ('{"success":false}', "failed"),
    ('{"status":"pending"}', "unknown"), ('{"status":"unknown"}', "unknown"),
    ('{"error":"invalid query"}', "failed"), ('{"exit_code":0}', "succeeded"),
    ('{"exit_code":1}', "failed"), ('{"isError":true}', "failed"),
    ('{"permission_decision":"denied"}', "not_executed"),
    ('{"status":"blocked"}', "not_executed"), ('{"error_code":"POLICY_DENIED"}', "not_executed"),
    ("Policy denied: restricted export", "not_executed"),
    ("Process exited with code 0\nOutput:\nHTTP/1.1 403\nerror example", "succeeded"),
    ("HTTP/1.1 404 Not Found", "failed"),
    ("Command exited with code 124", "failed"),
    ("curl: (28) Operation timed out", "failed"), ("NO NET\n", "failed"),
    ("HTTP 000\nNO NET (exit 28)\n", "failed"),
    ("(no output)\n<shell_metadata>\nUser aborted the command\n</shell_metadata>", "failed"),
    ("Traceback (most recent call last):\nFile x\nValueError: invalid", "failed"),
    ("bash: missing: command not found", "failed"),
    ('<path>/example.py</path>\n<type>file</type>\n<content>\nError: quoted example\n</content>', "succeeded"),
    ('Warning: deprecated\nresult=3', "succeeded"),
    (None, "unknown"), ("", "succeeded"), ("null", "unknown"),
])
def test_recorded_outcomes(output, expected):
    assert outcome(output)["execution_status"] == expected


def test_tool_message_metadata_is_consumed_during_import():
    record = {"messages": [
        {"role":"assistant", "tool_calls":[{"id":"a", "function":{"name":"read", "arguments":"{}"}}]},
        {"role":"tool", "tool_call_id":"a", "content":"denied", "is_error":True},
    ]}
    sample = normalize(record, "status-test", 0, "{}", 1, "auto")
    assert sample["execution"]["calls"][0]["execution_status"] == "failed"


def test_projection_preserves_explicit_status_and_leaves_missing_returns_unknown():
    calls = [{"execution_status":"unknown", "output_json":"directory listing", "return_event_id":"r"},
             {"execution_status":"unknown", "output_json":"null", "return_event_id":None},
             {"execution_status":"failed", "output_json":"content", "return_event_id":"r"}]
    original = deepcopy(calls)
    assert [c["execution_status"] for c in project_calls(calls)] == ["succeeded", "unknown", "failed"]
    assert calls == original


def test_historical_unknown_statuses_update_cards_rows_selection_and_statistics_without_reparse(env, tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_TRACE_ENABLE_LEGACY_VALUE_SYNC", raising=False)
    messages = []
    outputs = ["directory listing", "Command exited with code 124", '{"status":"blocked"}', None]
    for i, output in enumerate(outputs):
        messages.append({"role":"assistant", "tool_calls":[{"id":str(i), "function":{"name":"bash", "arguments":"{}"}}]})
        if output is not None:
            messages.append({"role":"tool", "tool_call_id":str(i), "content":output})
    bid, _ = upload(env, tmp_path, [{"messages": messages}])
    with env["sessions"]() as db:
        _, scope = q.resolve(db, bid, {})
        sample = db.scalar(select(TraceSample).where(TraceSample.snapshot_id == scope["snapshot_id"]))
        trace = sample.trace_id
        calls = list(db.scalars(select(TraceCall).where(TraceCall.snapshot_id == scope["snapshot_id"])))
        for call in calls:
            call.payload = {**call.payload, "execution_status":"unknown", "result_code":"NOT_RECORDED", "permission_decision":"unknown"}
        db.commit()
        original = [deepcopy(c.payload) for c in calls]
    url = f"{ROOT}/batches/{bid}/samples/{trace}"
    response = env["client"].get(url, params={"view":"tools"})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert {k:data["metrics"][k] for k in ("success_count", "error_count", "not_executed_count", "unknown_status_count")} == {
        "success_count":1, "error_count":1, "not_executed_count":1, "unknown_status_count":1}
    assert [c["execution_status"] for c in data["calls"]["items"]] == ["succeeded", "failed", "not_executed", "unknown"]
    for call in data["calls"]["items"]:
        selected = env["client"].get(url, params={"view":"tools", "section":"selection", "call_id":call["call_id"]}).json()["data"]["selection"]
        assert selected["execution_status"] == call["execution_status"]
    with env["sessions"]() as db:
        while stats.materialize(db):
            db.commit()
        distribution = stats.workspace(db, {"batch_id":bid}, USER)["charts"]["tool_result_distribution"]
        assert distribution["unknown_count"] == 1
        assert {v["code"]:v["count"] for v in distribution["items"]} == {"succeeded":1,"failed":1,"not_executed":1}
        assert [c.payload for c in db.scalars(select(TraceCall).where(TraceCall.snapshot_id == scope["snapshot_id"]))] == original


def test_zero_error_and_block_counts_remain_zero(env, tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_TRACE_ENABLE_LEGACY_VALUE_SYNC", raising=False)
    bid, _ = upload(env, tmp_path, [{"messages":[
        {"role":"assistant", "tool_calls":[{"id":"a", "function":{"name":"ls", "arguments":"{}"}}]},
        {"role":"tool", "tool_call_id":"a", "content":"README.md"},
    ]}])
    with env["sessions"]() as db:
        _, scope = q.resolve(db, bid, {})
        trace = db.scalar(select(TraceSample).where(TraceSample.snapshot_id == scope["snapshot_id"])).trace_id
    metrics = env["client"].get(f"{ROOT}/batches/{bid}/samples/{trace}", params={"view":"tools"}).json()["data"]["metrics"]
    assert metrics["success_count"] == 1
    assert metrics["error_count"] == metrics["not_executed_count"] == 0
