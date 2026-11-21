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
    ("curl: (28) Operation timed out", "failed"), ("NO NET\n", "succeeded"),
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


@pytest.mark.parametrize("output,kwargs,expected,error_type", [
    ('{"status":"success","stdout":"main.c:12:3: error: missing symbol"}', {}, "failed", "编译失败"),
    ('{"status":"success","exit_code":127}', {}, "failed", "命令不存在"),
    ('{"execution_status":"succeeded","status":"failed"}', {}, "failed", "工具执行错误"),
    ("Process exited with code 0\nOutput:\ncurl: (23) Failure writing output to destination", {}, "failed", "输出写入失败"),
    ("NO NET", {"command":"curl -I https://example.com || echo 'NO NET'"}, "succeeded", None),
    ('{"exit_code":1}', {"command":"grep missing README.md"}, "succeeded", None),
    ('{"exit_code":2}', {"command":"grep missing README.md"}, "failed", "进程退出异常"),
    ('{"http_status":404}', {"command":"curl -I https://example.com"}, "succeeded", None),
    ('{"exit_code":1,"output":"AddressSanitizer: heap-use-after-free"}',
     {"instructions":"Reproduce this vulnerability and trigger the target crash."}, "succeeded", None),
    ('{"exit_code":1,"output":"AddressSanitizer: heap-use-after-free"}', {}, "failed", "进程退出异常"),
    ("cat: missing: No such file or directory", {"command":"cat missing"}, "failed", "文件读取失败"),
    ('{"data":{"stderr":"Traceback (most recent call last):"}}', {}, "succeeded", None),
    ("Traceback (most recent call last):\nImportError: blocked for fallback test", {}, "failed", "运行异常"),
])
def test_c3_document_exclusions_and_internal_failures(output, kwargs, expected, error_type):
    result = outcome(output, **kwargs)
    assert result["execution_status"] == expected
    assert [f["error_type"] for f in result["execution_failures"]] == ([error_type] if error_type else [])
    assert all(f["quote"] in output for f in result["execution_failures"])


def test_expected_exception_is_contextual_and_does_not_hide_later_failure():
    command = "# expected exception for fallback test\nraise ValueError('deliberate test')"
    expected = "Traceback (most recent call last):\nValueError: deliberate test"
    assert outcome(expected, command=command)["execution_status"] == "succeeded"
    output = expected + "\n---\nTraceback (most recent call last):\nRuntimeError: actual bug"
    assert outcome(output, command=command)["execution_status"] == "failed"


def test_target_crash_exclusion_does_not_hide_timeout_or_command_error():
    output = "AddressSanitizer: heap-use-after-free\nCommand exited with code 124"
    assert outcome(output, instructions="Reproduce vulnerability and trigger a crash.")["execution_status"] == "failed"
    assert outcome('{"status":"timeout","output":"AddressSanitizer: heap-use-after-free"}',
                   instructions="Reproduce vulnerability and trigger a crash.")["execution_status"] == "failed"


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


def test_old_success_and_failure_are_reconciled_with_c3_report_and_audit(env, tmp_path, monkeypatch):
    import json
    monkeypatch.delenv("AGENT_TRACE_ENABLE_LEGACY_VALUE_SYNC", raising=False)
    messages = [{"role":"user", "content":"Reproduce the vulnerability and trigger a target crash."}]
    records = [
        ("curl -I https://example.com || echo 'NO NET'", "NO NET"),
        ("gcc broken.c", '{"status":"success","stderr":"broken.c:2:1: error: unknown type"}'),
        ("./target poc", '{"exit_code":1,"output":"AddressSanitizer: heap-use-after-free"}'),
    ]
    for i, (command, output) in enumerate(records):
        messages.extend([
            {"role":"assistant", "tool_calls":[{"id":str(i),"function":{"name":"bash","arguments":json.dumps({"command":command})}}]},
            {"role":"tool", "tool_call_id":str(i), "content":output},
        ])
    bid, _ = upload(env, tmp_path, [{"trajectory_id":"c3-contract", "messages":messages}])
    with env["sessions"]() as db:
        _, scope = q.resolve(db, bid, {})
        sample = db.scalar(select(TraceSample).where(TraceSample.snapshot_id == scope["snapshot_id"]))
        trace = sample.trace_id
        calls = list(db.scalars(select(TraceCall).where(TraceCall.snapshot_id == scope["snapshot_id"]).order_by(TraceCall.ordinal)))
        for call, status in zip(calls, ["failed", "succeeded", "failed"]):
            call.payload = {**call.payload,"execution_status":status,"result_code":"TIMEOUT" if status == "failed" else "OK"}
        payload = deepcopy(sample.payload)
        payload["rule_findings"].append({"finding_id":"stale", "rule_id":"tool_timeout", "category":"operation",
            "call_id":calls[0].payload["call_id"], "event_id":calls[0].payload["event_id"], "label":"工具超时", "evidence_text":"NO NET"})
        payload["competition_report"]["results"]["C3"]["failures"] = [{"step_id":1,"error_type":"工具超时","evidence":[]}]
        sample.payload = payload
        db.commit()
        saved_calls = [deepcopy(c.payload) for c in calls]
        saved_sample = deepcopy(sample.payload)
    url = f"{ROOT}/batches/{bid}/samples/{trace}"
    data = env["client"].get(url, params={"view":"tools"}).json()["data"]
    assert data["metrics"]["success_count"] == 2
    assert data["metrics"]["error_count"] == 1
    operations = [f for f in data["rule_findings"] if f["rule_id"] in {"tool_error", "tool_timeout"}]
    assert len(operations) == 1 and operations[0]["label"] == "编译失败"
    response = env["client"].get(f"{ROOT}/batches/{bid}/competition-report")
    assert response.status_code == 200, response.text
    result = json.loads(response.json()["data"]["jsonl"].splitlines()[0])["results"]
    assert [(f["step_id"],f["error_type"]) for f in result["C3"]["failures"]] == [(2,"编译失败")]
    issues = [f for f in result["C5"]["issues"] if f["source_subitem"] == "C3"]
    assert issues == [{"source_subitem":"C3", **result["C3"]["failures"][0]}]
    assert all(e["quote"] in records[1][1] for e in issues[0]["evidence"])
    with env["sessions"]() as db:
        while stats.materialize(db):
            db.commit()
        distribution = stats.workspace(db,{"batch_id":bid},USER)["charts"]["tool_result_distribution"]
        assert {v["code"]:v["count"] for v in distribution["items"]} == {"succeeded":2,"failed":1,"not_executed":0}
        assert [c.payload for c in db.scalars(select(TraceCall).where(TraceCall.snapshot_id == scope["snapshot_id"]).order_by(TraceCall.ordinal))] == saved_calls
        assert db.scalar(select(TraceSample).where(TraceSample.trace_id == trace)).payload == saved_sample
