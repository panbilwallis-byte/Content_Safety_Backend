"""Match repeated tool inputs and outputs over complete, unredacted calls."""
from copy import deepcopy
import json


def canonical(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return value
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def duplicate_groups(calls):
    """Within one sample, N identical tool/input/output tuples count as N-1."""
    history, groups = {}, {}
    for call in calls:
        # A missing return is not an observed output, including the "null"
        # placeholder that ingestion stores for calls without a linked return.
        if not call.get("return_event_id") or call.get("input_json") is None or call.get("output_json") is None:
            continue
        key = (call.get("tool_name"), canonical(call["input_json"]), canonical(call["output_json"]))
        first_id = history.get(key)
        if first_id is not None:
            group = groups.setdefault(first_id, {"canonical_call_id": first_id, "duplicate_call_ids": [],
                "reason": "同一样本内，相同工具的输入参数和输出内容相同；首次调用不计入重复次数"})
            if call["call_id"] != first_id and call["call_id"] not in group["duplicate_call_ids"]:
                group["duplicate_call_ids"].append(call["call_id"])
        else:
            history[key] = call["call_id"]
    return list(groups.values())


def project_calls(calls):
    groups = duplicate_groups(calls)
    duplicates = {cid for group in groups for cid in group["duplicate_call_ids"]}
    output = deepcopy(calls)
    for call in output:
        call["behavior_tags"] = [t for t in call.get("behavior_tags", []) if t.get("code") not in {"duplicate", "inefficient"}]
        if call["call_id"] in duplicates:
            call["behavior_tags"].append({"code": "inefficient", "label": "已核验重复调用"})
    return output, groups
