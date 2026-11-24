"""Public submission fields, independent of stored audit and event metadata.

Both report downloads use this projection. None denotes an opaque source value
(notably tool arguments, returns and the dynamic tool-count mapping).
"""
from copy import deepcopy


EVIDENCE = {"step_id": None, "message_index": None, "quote": None}
LOCATION = {"step_id": None, "message_index": None}
UNMATCHED = {**LOCATION, "tool_call_id": None, "issue": None, "content": None}
RESULTS = {
    "A1": {"指标": None, "input_tokens": None, "output_tokens": None},
    "A2": {"指标": None, "known_characters": None, "absent_body_step_ids": None},
    "A3": {"指标": None, "tool_calls": None, "tool_kinds": None, "tool_counts": None},
    "A4": {"指标": None, "trajectory_ids": None, "tool_calls": None, "tool_counts": None},
    "B1": {"指标": None, "segments": [{"step_id": None, "step_ids": None, "text": None}],
           "absent_body_step_ids": None},
    "B2": {"指标": None, "judgment": None},
    "B3": {"指标": None, "risks": [{"step_id": None, "risk_quote": None, "evidence": [EVIDENCE]}]},
    "C1": {"指标": None, "calls": [{"step_id": None, "tool_name": None, "arguments": None,
                                     "returns": [{"content": None}]}], "unmatched": [UNMATCHED]},
    "C2": {"指标": None, "markers": [{"step_id": None, "previous_step_id": None, "evidence": [EVIDENCE]}]},
    "C3": {"指标": None, "failures": [{"step_id": None, "error_type": None, "evidence": [EVIDENCE]}]},
    "C4": {"指标": None, "missing_fields": [{**LOCATION, "field": None, "issue": None}],
           "unmatched": [{k: v for k, v in UNMATCHED.items() if k != "content"}],
           "truncations": [{**LOCATION, "quote": None}], "completion": None},
    "C5": {"指标": None, "issues": [{"source_subitem": None, **LOCATION, "evidence": [EVIDENCE]}]},
}


def project(value, fields):
    """Copy allowed fields only; never infer coordinates or replace raw bodies."""
    if fields is None:
        return deepcopy(value)
    if isinstance(fields, list):
        return [project(item, fields[0]) for item in value]
    return {key: project(value[key], child) for key, child in fields.items() if key in value}


def submission_rows(rows):
    return [project(row, {"trajectory_id": None, "results": RESULTS}) for row in rows]
