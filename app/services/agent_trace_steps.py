"""Read-only action steps over frozen source records; raw events remain evidence.

The messages adapter uses the competition parser rather than counting normalized
events. Other adapters apply the same action/response convention to their source
coordinates. No audit is rerun and no database row is changed here.
"""
from copy import deepcopy
import re

from app.services.agent_trace_competition import parse
from app.services.agent_trace_generation import dumps, stable_id
from app.services import agent_trace_anthropic as anthropic

VERSION = "action-steps-v1"


def text(value):
    return value if isinstance(value, str) else "" if value is None else dumps(value)


def part(path, label, value):
    return {"source_path": path, "label": label, "text": text(value)}


def messages_steps(record):
    # Ordinary OpenAI-compatible imports also accept `reasoning`.
    messages = record["messages"]
    adapted = {"messages": [dict(m, reasoning_content=m.get("reasoning_content") or m.get("reasoning"))
                             if isinstance(m, dict) else m for m in messages]}
    calls, _, _, _, _, locations = parse(adapted)
    by_step = {c["step_id"]: c for c in calls}
    steps = []
    for i, message in enumerate(adapted["messages"]):
        if message["role"] != "assistant":
            continue
        ids = locations.get(i, [])
        for sid in ids:
            call = by_step.get(sid)
            base = f"messages[{i}]"
            paths = [base + ".content", base + ".reasoning_content", base + ".reasoning"]
            parts = []
            first = sid == ids[0]
            if first and message.get("content"):
                parts.append(part(base + ".content", "助手回复", message["content"]))
            if call:
                anchor = f"{base}.tool_calls[{call['call_index']}]"
                paths.insert(0, anchor)
                parts.append(part(anchor + ".function.name", "工具名称", call["tool_name"] or "未知工具"))
                parts.append(part(anchor, "调用参数", call["arguments"]))
                for returned in call["returns"]:
                    path = f"messages[{returned['message_index']}]"
                    paths.append(path)
                    parts.append(part(path, "工具返回", returned["content"]))
            else:
                anchor = base + (".content" if message.get("content") is not None else
                                 ".reasoning_content" if messages[i].get("reasoning_content") else ".reasoning")
            steps.append({"step_id": sid, "anchor": anchor, "paths": paths, "parts": parts,
                          "event_type": "tool_call" if call else "message",
                          "label": "工具调用" if call else "独立回复",
                          "reasoning": message.get("reasoning_content") if first else None,
                          "shared_reasoning_step": ids[0] if not first and message.get("reasoning_content") else None})
    return steps


def langchain_steps(record):
    runs = record["runs"]
    if not all(isinstance(r, dict) and r.get("id") for r in runs):
        raise ValueError("运行节点缺少id")
    by_id = {r["id"]: i for i, r in enumerate(runs)}
    if len(by_id) != len(runs):
        raise ValueError("运行id重复")
    children = {i: [] for i in range(len(runs))}
    roots = []
    for i, run in enumerate(runs):
        parent = by_id.get(run.get("parent_run_id"))
        if parent is None:
            roots.append(i)
        else:
            children[parent].append(i)
    steps, visited = [], set()

    def visit(i):
        if i in visited:
            raise ValueError("运行父子关系存在循环")
        visited.add(i)
        run, base = runs[i], f"runs[{i}]"
        tool = run.get("run_type") == "tool"
        # A container completes after its children. Array order is retained for
        # siblings (timestamps are optional and may be inconsistent).
        before = len(steps)
        if not tool:
            for child in children[i]:
                visit(child)
        child_reply = any(s["event_type"] == "message" for s in steps[before:])
        output = run.get("outputs")
        meaningful = output not in (None, "", {}, [])
        if tool or (meaningful and not child_reply):
            parts = ([part(base + ".name", "工具名称", run.get("name") or "未知工具"),
                      part(base + ".inputs", "调用参数", run.get("inputs"))] if tool else [])
            if meaningful:
                parts.append(part(base + ".outputs", "工具返回" if tool else "助手回复", output))
            if run.get("error"):
                parts.append(part(base + ".outputs", "运行错误", run["error"]))
            steps.append({"anchor": base + ".inputs" if tool else base,
                          "paths": [base, base + ".inputs", base + ".outputs"], "parts": parts,
                          "event_type": "tool_call" if tool else "message",
                          "label": "工具调用" if tool else "独立回复",
                          "reasoning": None, "shared_reasoning_step": None})
        if tool:
            for child in children[i]:
                visit(child)

    for i in roots:
        visit(i)
    if len(visited) != len(runs):
        raise ValueError("运行父子关系存在循环")
    return steps


def trajectory_steps(record):
    events = record["events"]
    if not all(isinstance(e, dict) for e in events):
        raise ValueError("事件必须是对象")
    # Explicit source-step association is stronger than adjacency. One call per
    # step even when several different tools share a source step identifier.
    calls, thoughts, used, steps, reasoning_owners = {}, {}, set(), [], {}
    for i, event in enumerate(events):
        if event.get("type") == "tool_call":
            key = (event.get("step_id"), event.get("tool"))
            if not all(key) or key in calls:
                raise ValueError("工具请求关联不唯一")
            calls[key] = i
        elif event.get("type") == "thought" and event.get("step_id"):
            thoughts.setdefault(event["step_id"], []).append(i)
    for i, event in enumerate(events):
        kind = event.get("type")
        if kind == "tool_call":
            base = f"events[{i}]"
            members = [j for j in thoughts.get(event.get("step_id"), []) if j < i and j not in used]
            reasoning = "\n\n".join(text(events[j].get("reasoning") or events[j].get("content")) for j in members)
            used.update(members)
            returns = [j for j, e in enumerate(events) if j > i and e.get("type") == "tool_result"
                       and (e.get("step_id"), e.get("tool")) == (event.get("step_id"), event.get("tool"))]
            paths = [base] + [f"events[{j}]" for j in members]
            parts = [part(base + ".tool", "工具名称", event.get("tool") or "未知工具"),
                     part(base, "调用参数", event.get("tool_args", {}))]
            # Multiple matching returns cannot be paired unambiguously.
            if len(returns) == 1:
                j = returns[0]
                paths.append(f"events[{j}]")
                parts.append(part(f"events[{j}]", "工具返回", events[j].get("content")))
                used.add(j)
            errors = [j for j, e in enumerate(events) if e.get("type") == "error"
                      and e.get("step_id") == event.get("step_id")
                      and (e.get("tool") == event.get("tool") or
                           (not e.get("tool") and sum(key[0] == e.get("step_id") for key in calls) == 1))]
            for j in errors:
                paths.append(f"events[{j}]")
                parts.append(part(f"events[{j}]", "运行错误", events[j].get("content")))
                used.add(j)
            shared = reasoning_owners.get(event.get("step_id")) if not reasoning else None
            if reasoning:
                reasoning_owners[event.get("step_id")] = len(steps) + 1
            steps.append({"anchor": base, "paths": paths, "parts": parts, "event_type": "tool_call",
                          "label": "工具调用",
                          "reasoning": reasoning or event.get("reasoning"), "shared_reasoning_step": shared,
                          "source_step_id": event.get("step_id")})
        elif kind in {"thought", "final"} and i not in used:
            associated = kind == "thought" and any(j > i and key[0] == event.get("step_id") for key, j in calls.items())
            if associated:
                continue
            base = f"events[{i}]"
            if event.get("content") or event.get("reasoning"):
                steps.append({"anchor": base, "paths": [base], "parts": [part(base, "助手回复", event.get("content"))],
                              "event_type": "thought" if kind == "thought" else "message",
                              "label": "独立思考" if kind == "thought" else "最终回复",
                              "reasoning": event.get("reasoning") or (event.get("content") if kind == "thought" else None),
                              "shared_reasoning_step": None, "source_step_id": event.get("step_id")})
    return steps


def plan(sample):
    record = sample.result.get("source_record")
    if not isinstance(record, dict) or sample.payload.get("data_origin") == "generated":
        return None
    try:
        if sample.payload.get("source", {}).get("format_code") == anthropic.CODE:
            adapted, paths = anthropic.message_view(record)
            steps = messages_steps(adapted)
            for step in steps:
                step["anchor"] = paths[step["anchor"]]
                step["paths"] = [paths[path] for path in step["paths"] if path in paths]
                for part in step["parts"]:
                    part["source_path"] = paths[part["source_path"]]
        elif isinstance(record.get("messages"), list):
            steps = messages_steps(record)
        elif isinstance(record.get("runs"), list):
            steps = langchain_steps(record)
        elif isinstance(record.get("events"), list):
            steps = trajectory_steps(record)
        else:
            return None
    except (ValueError, TypeError, KeyError):
        return None
    for sid, step in enumerate(steps, 1):
        step["step_id"] = sid
    return steps


def counts(sample, steps=None):
    steps = plan(sample) if steps is None else steps
    return {"event_count": len(steps) if steps is not None else sample.payload.get("event_count", 0),
            "step_count": len(steps) if steps is not None else None,
            "raw_event_count": sample.payload.get("event_count", 0),
            "step_policy_version": VERSION,
            "step_availability": "available" if steps is not None else "raw_events_fallback"}


def project(sample, raw_events, sanitize=lambda value: value, reasoning_getter=None, source_text_getter=None):
    """Return display events, raw -> display IDs, and uncounted original events.

    Sanitize each source fragment BEFORE concatenating text so embedded JSON
    secrets are still recognized by the standard redactor.
    """
    steps = plan(sample)
    if steps is None:
        return deepcopy(raw_events), {e["event_id"]: e["event_id"] for e in raw_events}, [], counts(sample)
    by_path = {}
    for event in raw_events:
        by_path.setdefault((event.get("source_ref") or {}).get("source_path"), []).append(event)
    events, mapping = [], {}
    for step in steps:
        anchor = next(iter(by_path.get(step["anchor"], [])), None)
        # Reasoning-only imports may have a different accepted spelling.
        if anchor is None and step["event_type"] != "tool_call":
            anchor = next((e for path in step["paths"] for e in by_path.get(path, [])), None)
        event = deepcopy(anchor) if anchor else {"event_id": stable_id("step_", sample.trace_id, step["step_id"]),
                                               "data_origin": "actual", "source_ref": {"source_path": step["anchor"]}}
        sid, eid = step["step_id"], event["event_id"]
        members = [e for path in step["paths"] for e in by_path.get(path, [])]
        for member in members:
            mapping.setdefault(member["event_id"], eid)
        parts = [{**p, "text": sanitize(p["text"])} for p in step["parts"]]
        body = "\n\n".join(f"【{p['label']} · {p['source_path']}】\n{p['text']}" for p in parts)
        reasoning = sanitize(step.get("reasoning") or "")
        if reasoning and reasoning_getter and members:
            candidates = [e for e in members if (e.get("source_ref") or {}).get("source_path") not in
                          {p["source_path"] for p in step["parts"] if p["label"] == "工具返回"}]
            recorded, seen = [], set()
            for candidate in candidates:
                path = (candidate.get("source_ref") or {}).get("source_path", candidate["event_id"])
                match = re.match(r"messages\[\d+\]", path)
                coordinate = match[0] if match else path
                body_text = reasoning_getter(candidate)["text"]
                if body_text and coordinate not in seen:
                    recorded.append(sanitize(body_text))
                    seen.add(coordinate)
            reasoning = "\n\n".join(recorded)
        shared = step.get("shared_reasoning_step")
        shared_note = f"共享思考见第 {shared} 步" if shared else ""
        raw_description = event.get("description")
        raw_original = source_text_getter(event) if source_text_getter else None
        event.update(step_id=sid, ordinal=sid, display_id=f"STEP-{sid:02}", source_step_ids=[sid],
                     event_type=step["event_type"], label=sanitize(step["label"]),
                     source_excerpt=body or reasoning or shared_note or "未提供正文",
                     description=body or reasoning or shared_note or "未提供正文",
                     reasoning_original=reasoning, reasoning_summary=reasoning or shared_note or "未提供思考原文",
                     reasoning_provenance="source_log" if reasoning else "not_provided",
                     shared_reasoning_step=shared, source_event_ids=list(dict.fromkeys(e["event_id"] for e in members)),
                     source_parts=parts, step_policy_version=VERSION)
        if source_text_getter and anchor:
            event["recorded_original_text"] = sanitize(raw_original) if raw_original is not None else None
            event["description"] = raw_description
        if step.get("source_step_id") is not None:
            event["source_step_id"] = step["source_step_id"]
        if not anchor:
            event["source_ref"].update(deepcopy(sample.payload.get("source_ref") or {}), source_path=step["anchor"])
        events.append(event)
    uncounted = [deepcopy(e) for e in raw_events if e["event_id"] not in mapping]
    if events and uncounted:
        # The existing UI renders source_excerpt without a new frontend field.
        # Keep excluded messages and ambiguous returns accessible, explicitly
        # labelled as context rather than silently assigning them to step 1.
        context = "\n\n".join(f"【不计步原始记录 · {(e.get('source_ref') or {}).get('source_path', '')}】\n"
                               + sanitize(e.get("source_excerpt") or "") for e in uncounted)
        events[0]["source_excerpt"] += "\n\n" + context
    return events, mapping, uncounted, counts(sample, steps)


def remap_findings(projection, mapping, events):
    """Keep trajectory judgments and locate findings linked to action steps."""
    value = deepcopy(projection)
    by_id = {e["event_id"]: e for e in events}
    original_only = []
    findings = []
    for finding in value.get("rule_findings", []):
        if finding.get("scope") == "trace":
            findings.append(finding)
            continue
        eid = mapping.get(finding.get("event_id"))
        if eid not in by_id:
            original_only.append(finding)
            continue
        finding["source_event_id"] = finding.get("event_id")
        finding.update(event_id=eid, event_display_id=by_id[eid]["display_id"], competition_step=by_id[eid]["ordinal"])
        findings.append(finding)
    value["rule_findings"] = findings
    value["source_only_findings"] = original_only
    return value
