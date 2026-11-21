"""C2 excludes justified repetitions and keeps exact source evidence."""
from copy import deepcopy
import json

import pytest
from sqlalchemy import select

from test_agent_traces import env, ROOT
from test_agent_trace_workflow import upload, USER
from app.models.agent_traces import TraceSample, TraceCall
from app.services import agent_trace_query as q, agent_trace_statistics as stats

from app.services.agent_trace_competition import analyze, project_report_duplicates
from app.services.agent_trace_ingest import normalize
from app.services.agent_trace_duplicates import project_calls
from app.services.agent_trace_call_status import project_calls as project_statuses


def trajectory(name="read_file", purpose="", between=None, output="saved text", args=None):
    args = args or {"path": "fixed.txt"}
    def action(cid, content=""):
        return {"role": "assistant", "content": content, "tool_calls": [
            {"id": cid, "function": {"name": name, "arguments": json.dumps(args)}}]}
    messages = [action("a"), {"role": "tool", "tool_call_id": "a", "content": output}]
    messages.extend(between or [])
    messages.extend([action("b", purpose), {"role": "tool", "tool_call_id": "b", "content": output}])
    return {"trajectory_id": "c2-test", "messages": messages}


def check(record, expected):
    saved = deepcopy(record)
    calls = normalize(record, "file", 0, json.dumps(record), 1, "auto")["execution"]["calls"]
    calls = project_statuses(calls, record)
    projected, groups = project_calls(calls, record)
    results = analyze(record, apply_annotations=False)["results"]
    assert sum(len(g["duplicate_call_ids"]) for g in groups) == expected
    assert sum(any(t["code"] == "inefficient" for t in c["behavior_tags"]) for c in projected) == expected
    assert len(results["C2"]["markers"]) == expected
    assert [i for i in results["C5"]["issues"] if i["source_subitem"] == "C2"] == [
        {"source_subitem": "C2", **m} for m in results["C2"]["markers"]]
    for marker in results["C2"]["markers"]:
        assert marker["previous_step_id"] < marker["step_id"]
        assert all(isinstance(e["quote"], str) and "message_index" in e for e in marker["evidence"])
    assert record == saved
    return results


@pytest.mark.parametrize("purpose", [
    "正常轮询，等待任务完成", "为了必要复核，再次读取", "文件已更新，需要重新查询", "修复后重试相同调用",
    "Polling until the task finishes", "Read again to verify the result", "Query after updating the data",
    "Retry after fixing the configuration",
])
def test_four_required_exclusions(purpose):
    check(trajectory(purpose=purpose), 0)


def test_no_tool_whitelist_and_equality_alone_is_not_proof():
    check(trajectory(name="custom_tool", args={"query": "x"}), 0)
    result = check(trajectory(name="custom_tool", purpose="这是不必要的重复调用，之前结果已经可用。", args={"query": "x"}), 1)
    assert "不必要" in result["C2"]["markers"][0]["evidence"][-1]["quote"]
    check(trajectory(name="read_file"), 1)
    check(trajectory(name="vendor.fetch_document", args={"filePath": "fixed.txt"}), 1)


def test_exclusions_win_over_redundancy_words():
    check(trajectory(purpose="虽然看似不必要的重复调用，但这是为了必要复核。"), 0)
    check(trajectory(name="job.status", purpose="不必要的重复调用", args={"job_id": "x"}), 0)


def test_same_error_not_blanket_duplicate_and_no_success_requirement_for_explicit_proof():
    check(trajectory(output="Error: missing configuration"), 0)
    check(trajectory(name="custom_tool", output="Error: missing configuration", purpose="这是不必要的重复调用，失败原因没有处理。"), 1)


def test_state_changes_and_repaired_retry_are_not_redundant():
    mutation = [{"role": "assistant", "tool_calls": [{"id": "edit", "function": {
        "name": "edit", "arguments": '{"path":"fixed.txt","text":"new"}'}}]},
        {"role": "tool", "tool_call_id": "edit", "content": "updated"}]
    check(trajectory(between=mutation), 0)
    check(trajectory(between=mutation, purpose="不必要的重复调用"), 0)


def test_in_place_edit_and_follow_are_not_static_reads():
    for command in ("sed -i s/a/b/ fixed.txt", "tail -f fixed.txt"):
        check(trajectory(name="bash", args={"command": command}), 0)


def test_returned_text_does_not_override_action_purpose():
    check(trajectory(name="custom_tool", output="不必要的重复调用"), 0)
    check(trajectory(output="This document says: poll to confirm result"), 1)


def test_multiple_calls_use_same_reply_purpose_and_scoped_return_ids():
    record = trajectory(purpose="为了必要复核，再次读取")
    record["messages"][-2]["tool_calls"].append({"id": "c", "function": {
        "name": "read_file", "arguments": json.dumps({"path": "fixed.txt"})}})
    record["messages"].append({"role": "tool", "tool_call_id": "c", "content": "saved text"})
    check(record, 0)


def test_polling_flags_and_original_user_task_are_respected():
    check(trajectory(args={"path": "status.txt", "poll": True}), 0)
    record = trajectory()
    record["messages"].insert(0, {"role": "user", "content": "正常轮询文件，等待任务完成"})
    check(record, 0)


def test_old_report_refresh_only_changes_c2_and_its_c5_without_writes():
    record = trajectory(purpose="正常轮询，等待任务完成")
    report = analyze(record, apply_annotations=False)
    report["results"]["C2"]["markers"] = [{"step_id": 2, "previous_step_id": 1, "evidence": []}]
    report["results"]["C5"]["issues"].append({"source_subitem": "C2", "step_id": 2, "evidence": []})
    original = deepcopy(report)
    project_report_duplicates(report, record)
    assert report["results"]["C2"]["markers"] == []
    assert not any(i["source_subitem"] == "C2" for i in report["results"]["C5"]["issues"])
    assert all(report["results"][key] == original["results"][key] for key in original["results"] if key not in {"C2", "C5"})


@pytest.mark.parametrize("purpose,expected", [("正常轮询，等待任务完成", 0), ("这是不必要的重复调用，之前结果已可用。", 1)])
def test_old_database_tags_graph_statistics_report_and_export_agree(env, tmp_path, monkeypatch, purpose, expected):
    from test_agent_trace_json_export import download_export, command
    monkeypatch.delenv("AGENT_TRACE_ENABLE_LEGACY_VALUE_SYNC", raising=False)
    record = trajectory(name="custom_tool", purpose=purpose, args={"query": "x", "api_key": "input-secret"},
                        output='{"password":"output-secret","text":"ok"}')
    record["test_category"] = "prompt_injection"
    bid, _ = upload(env, tmp_path, [record])
    with env["sessions"]() as db:
        _, scope = q.resolve(db, bid, {})
        sample = db.scalar(select(TraceSample).where(TraceSample.snapshot_id == scope["snapshot_id"]))
        calls = q.process_records(db, scope, sample.trace_id, TraceCall)
        payload = deepcopy(sample.payload)
        payload["rule_findings"] = [{"finding_id": "old-duplicate", "rule_id": "recorded_duplicate",
            "category": "efficiency", "event_id": calls[-1]["event_id"], "call_id": calls[-1]["call_id"],
            "event_display_id": calls[-1]["step_display_id"], "label": "old duplicate"}]
        payload["competition_report"]["results"]["C2"]["markers"] = [
            {"step_id": 2, "previous_step_id": 1, "evidence": []}]
        payload["competition_report"]["results"]["C5"]["issues"] = [
            {"source_subitem": "C2", "step_id": 2, "evidence": []}]
        sample.payload = payload
        while stats.materialize(db):
            db.flush()
        db.commit()
        saved = deepcopy(sample.payload)
        trace = sample.trace_id
    url = f"{ROOT}/batches/{bid}/samples/{trace}"
    tool = env["client"].get(url, params={"view": "tools"})
    assert tool.status_code == 200, tool.text
    assert tool.json()["data"]["metrics"]["inefficient_count"] == expected
    assert "input-secret" not in tool.text and "output-secret" not in tool.text
    graph = env["client"].get(url, params={"view": "execution"})
    assert graph.status_code == 200, graph.text
    assert sum(f["rule_id"] == "repeated_request" for f in graph.json()["data"]["rule_findings"]) == expected
    assert "input-secret" not in graph.text and "output-secret" not in graph.text
    result = env["client"].get(f"{ROOT}/batches/{bid}/competition-report").json()["data"]
    report = json.loads(result["jsonl"].splitlines()[0])
    assert len(report["results"]["C2"]["markers"]) == expected
    assert all(marker["evidence"] for marker in report["results"]["C2"]["markers"])
    assert sum(i["source_subitem"] == "C2" for i in report["results"]["C5"]["issues"]) == expected
    with env["sessions"]() as db:
        matrix = stats.workspace(db, {"batch_id": bid}, USER)["charts"]["behavior_matrix"]
        assert sum(c["count"] for r in matrix["rows"] for c in r["cells"] if c["behavior_code"] == "duplicate") == expected
        assert q.get_sample(db, scope, trace).payload == saved
        assert q.process_records(db, scope, trace, TraceCall) == calls
    for view in ("tools", "execution"):
        exported = download_export({**env, "batch": bid, "scope": scope, "trace": trace},
                                   command({**env, "scope": scope, "trace": trace}, view))
        exported_calls = exported["calls"] if view == "tools" else exported["samples"][0]["calls"]
        assert sum(any(t["code"] == "inefficient" for t in c["behavior_tags"]) for c in exported_calls) == expected
