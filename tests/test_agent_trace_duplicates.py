"""C2 unnecessary repetitions and four exclusions agree across display and report."""
from copy import deepcopy
import json

import pytest
from sqlalchemy import select

from test_agent_traces import env, ROOT
from test_agent_trace_workflow import upload, USER
from app.models.agent_traces import TraceCall, TraceSample
from app.services import agent_trace_query as q
from app.services import agent_trace_statistics as stats
from app.services.agent_trace_duplicates import duplicate_groups, project_calls


def call(i, **values):
    return {"call_id": str(i), "tool_name": "read_file", "input_json": '{"path":"fixed.txt"}',
            "output_json": '{"status":"success","data":1}', "execution_status": "succeeded",
            "return_event_id": f"return-{i}", "behavior_tags": [], **values}


def test_n_calls_count_n_minus_one_and_do_not_mutate_input():
    calls = [call(i) for i in range(4)]
    saved = deepcopy(calls)
    projected, groups = project_calls(calls)
    assert groups[0]["canonical_call_id"] == "0"
    assert groups[0]["duplicate_call_ids"] == ["1", "2", "3"]
    assert sum(bool(c["behavior_tags"]) for c in projected) == 3
    assert calls == saved


@pytest.mark.parametrize("changes", [
    {"execution_status": "failed"}, {"execution_status": "not_executed"},
    {"execution_status": "unknown"},
])
def test_failed_or_blocked_returns_do_not_prove_unnecessary_repetition(changes):
    if changes["execution_status"] == "unknown":
        assert duplicate_groups([call(0, **changes), call(1)])[0]["duplicate_call_ids"] == ["1"]
    else:
        assert duplicate_groups([call(0, **changes), call(1)]) == []
        assert duplicate_groups([call(0), call(1, **changes)]) == []


@pytest.mark.parametrize("changes", [{"return_event_id": None}, {"output_json": None}, {"input_json": None}])
def test_missing_input_or_output_is_not_an_observed_duplicate(changes):
    assert duplicate_groups([call(0, **changes), call(1)]) == []
    assert duplicate_groups([call(0), call(1, **changes)]) == []


def test_comparison_uses_normalized_json_but_requires_identical_result():
    first = call(0, input_json='{"path":"fixed.txt","b":2,"a":1}')
    second = call(1, input_json=' {"a":1, "b":2,"path":"fixed.txt"} ')
    assert duplicate_groups([first, second])[0]["duplicate_call_ids"] == ["1"]
    assert duplicate_groups([first, {**second, "output_json": '{"data":2}'}]) == []


def test_identical_errors_alone_do_not_prove_unnecessary_repetition():
    first = call(0, execution_status="failed", output_json='{"error":"TIMEOUT","status":"error"}')
    second = call(1, execution_status="failed", output_json=' {"status":"error", "error":"TIMEOUT"} ')
    assert duplicate_groups([first, second]) == []
    assert duplicate_groups([first, call(1)]) == []


@pytest.mark.parametrize("tool_name,args", [
    ("write", "{}"), ("unknown_tool", "{}"),
    ("bash", '{"command":"cat x; rm x"}'),
])
def test_unknown_state_changes_do_not_prove_unnecessary_repetition(tool_name, args):
    assert duplicate_groups([call(0), call(1, tool_name=tool_name, input_json=args), call(2)]) == []


def test_equal_inputs_outputs_are_only_candidates_for_arbitrary_tools():
    calls = [call(0, tool_name="query_database"), call(1, tool_name="query_database"),
             call(2, tool_name="write"), call(3, tool_name="write")]
    assert duplicate_groups(calls) == []


def test_intervening_unrelated_reads_do_not_clear_history():
    calls = [call(0), call(1, input_json='{"path":"other.txt"}'), call(2)]
    assert duplicate_groups(calls)[0]["duplicate_call_ids"] == ["2"]


def test_different_secrets_and_source_duplicate_flags_do_not_prove_duplicates():
    calls = [call(0, input_json='{"token":"secret-a"}'),
             call(1, input_json='{"token":"secret-b"}', behavior_tags=[{"code": "inefficient"}, {"code": "retry"}])]
    projected, groups = project_calls(calls)
    assert groups == []
    assert projected[1]["behavior_tags"] == [{"code": "retry"}]


def test_historical_tool_count_groups_selection_and_report_match_without_db_changes(env, tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_TRACE_ENABLE_LEGACY_VALUE_SYNC", raising=False)
    messages = [{"role": "user", "content": "查询"}]
    for i in range(26):
        messages.extend([
            {"role": "assistant", "content": "不必要地重复调用已完成的工具" if i not in {0, 23} else "首次操作", "tool_calls": [{"id": f"c{i}", "function": {
                "name": "query_database" if i < 23 else "custom_write", "arguments": '{"q":"x"}'}}]},
            {"role": "tool", "tool_call_id": f"c{i}", "content": "plain text result without execution status"},
        ])
    # One separate trace with identical data must not be grouped with the first.
    bid, _ = upload(env, tmp_path, [{"messages": messages, "test_category": "prompt_injection"},
                                 {"messages": messages[:3], "test_category": "prompt_injection"}])
    with env["sessions"]() as db:
        _, scope = q.resolve(db, bid, {})
        samples = list(db.scalars(select(TraceSample).where(TraceSample.snapshot_id == scope["snapshot_id"])))
        trace = next(s.trace_id for s in samples if s.payload["call_count"] == 26)
        saved = [deepcopy(r.payload) for r in db.scalars(select(TraceCall))]
    url = f"{ROOT}/batches/{bid}/samples/{trace}"
    response = env["client"].get(url, params={"view": "tools", "call_page": 2, "call_page_size": 20})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["metrics"]["inefficient_count"] == 24
    assert data["metrics"]["unknown_status_count"] == 0
    assert data["metrics"]["success_count"] == 26
    assert len(data["calls"]["items"]) == 6
    groups = data["sample_analysis"]["inefficiency_groups"]
    ids = {cid for g in groups for cid in g["duplicate_call_ids"]}
    assert len(ids) == 24
    assert sorted(len(g["duplicate_call_ids"]) for g in groups) == [2, 22]
    selected = env["client"].get(url, params={"view": "tools", "section": "selection", "call_id": next(iter(ids))}).json()["data"]["selection"]
    assert any(t["code"] == "inefficient" for t in selected["behavior_tags"])
    report = env["client"].get(f"{ROOT}/batches/{bid}/competition-report")
    assert report.status_code == 200, report.text
    rows = [json.loads(line) for line in report.json()["data"]["jsonl"].splitlines()]
    markers = [m for row in rows[:-1] for m in row["results"]["C2"]["markers"]]
    assert len(markers) == 24
    assert all(len(m["evidence"]) == 5 for m in markers)
    with env["sessions"]() as db:
        while stats.materialize(db):
            db.commit()
        matrix = stats.workspace(db, {"batch_id": bid}, USER)["charts"]["behavior_matrix"]
        assert sum(cell["count"] for row in matrix["rows"] for cell in row["cells"]
                   if cell["behavior_code"] == "duplicate") == 24
        assert [r.payload for r in db.scalars(select(TraceCall))] == saved
