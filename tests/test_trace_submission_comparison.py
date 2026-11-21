"""The imported comparison utility accepts current saved report responses."""
from copy import deepcopy
import json

import pytest

from scripts.compare_trace_submission import compare, read_rows
from app.services.agent_trace_competition import analyze


@pytest.mark.parametrize("representation", ["jsonl", "report", "response", "array", "single"])
def test_report_formats_preserve_current_scoring_structure(tmp_path, representation):
    row = analyze({"trajectory_id": "sample", "messages": [
        {"role": "assistant", "reasoning_content": "思考😀", "tool_calls": [
            {"id": "a", "function": {"name": "Read", "arguments": '{ "path": "a" }'}}]},
        {"role": "tool", "tool_call_id": "a", "content": "\noriginal\n"}]})
    jsonl = json.dumps(row, ensure_ascii=False) + "\n"
    value = {"jsonl": jsonl, "report": {"jsonl": jsonl, "analysis_version": "current"},
             "response": {"code": 0, "data": {"jsonl": jsonl}}, "array": [row], "single": row}[representation]
    path = tmp_path / "submission.json"
    path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8-sig")
    rows = read_rows(path)
    assert rows == {"sample": row["results"]}
    assert compare(rows, rows) == []
    changed = deepcopy(rows)
    changed["sample"]["C1"]["calls"][0]["tool_name"] = "read"
    differences = compare(changed, rows)
    assert len(differences) == 1 and "sample C1" in differences[0]


def test_duplicate_or_malformed_rows_are_rejected(tmp_path):
    path = tmp_path / "submission.jsonl"
    row = {"trajectory_id": "same", "results": {}}
    path.write_text(json.dumps([row, row]), encoding="utf-8")
    with pytest.raises(ValueError, match="重复轨迹"):
        read_rows(path)
    for malformed in [[], {"results": {}}, {"trajectory_id": "sample", "results": []}]:
        path.write_text(json.dumps([malformed]), encoding="utf-8")
        with pytest.raises(ValueError):
            read_rows(path)
