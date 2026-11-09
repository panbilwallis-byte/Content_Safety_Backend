"""Read-only tools/execution projections over published relational records."""
import base64
from copy import deepcopy
import hashlib
import hmac
import json
import os
import re
import time

from fastapi import HTTPException
from sqlalchemy import func, select

from app.core.auth import load_secret
from app.core.time import now_shanghai
from app.models.agent_traces import (
    TraceBatch, TraceSnapshot, TraceProjection, TraceSample, TraceEvent, TraceCall,
    TraceEvidence, TraceRelation, TraceIngestCheckpoint,
)
from app.services.agent_trace_generation import VERSION, dumps
from app.services import agent_trace_sync as sync

ROOT = "/compliance/agent-traces"


def fail(status, message):
    raise HTTPException(status, message)


def page(items, number=1, size=20):
    total = len(items)
    return {"items": items[(number - 1) * size:number * size], "total": total, "page": number,
            "page_size": size, "total_pages": (total + size - 1) // size}


def text_preview(value, units):
    """Bound text without cutting a UTF-16 surrogate pair."""
    value = str(value or "")
    encoded = value.encode("utf-16-le")
    text = encoded[:units * 2].decode("utf-16-le", errors="ignore")
    return {"text": text, "truncated": len(encoded) > units * 2,
            "loaded_units": len(text.encode("utf-16-le")) // 2, "total_units": len(encoded) // 2}


def call_preview(call, units):
    value = deepcopy(call)
    truncated = []
    for name in ("input_json", "output_json"):
        preview = text_preview(value.get(name, ""), units)
        value[name] = preview["text"]
        if preview["truncated"]:
            truncated.append(name)
    value["truncated_fields"] = truncated
    return value


def number(params, key, default, maximum=100):
    try:
        value = int(params.get(key, default))
    except (ValueError, TypeError):
        fail(422, f"{key} 必须为整数")
    if value < 1 or value > maximum:
        fail(422, f"{key} 超出允许范围")
    return value


def boolean(params, key):
    v = params.get(key, False)
    if v in (True, "true", "1"):
        return True
    if v in (False, "false", "0", None):
        return False
    fail(422, f"{key} 必须为布尔值")


def permissions(user):
    operator = user.get("tier") != "reader"
    return {"can_query": True, "can_list_samples": True, "can_view_detail": True,
            "can_view_raw": operator, "can_export": operator}


def redact(value, *, preserve_ids=False):
    """Conservative display redaction; never expose stored source payloads verbatim by default."""
    if isinstance(value, dict):
        identity_fields = {"id", "event_id", "call_id", "trace_id", "source_id", "batch_id", "snapshot_id",
            "evidence_id", "edge_id", "from_event_id", "to_event_id", "request_event_id", "return_event_id",
            "decision_event_id", "attempt_id", "parent_call_id", "previous_event_id",
            "next_event_id", "canonical_call_id", "duplicate_call_ids", "related_call_ids"}
        return {k: v if preserve_ids and k in identity_fields else
                "[已脱敏]" if re.search(r"password|secret|api.?key|authorization|access.?token", k, re.I)
                else redact(v, preserve_ids=preserve_ids and k not in {"value_result","source_record","source_output","metadata"}) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, preserve_ids=preserve_ids) for v in value]
    if isinstance(value, str):
        # Strings may themselves contain arbitrary JSON; preserve its keys while redacting values.
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, (dict, list)):
            sanitized = redact(parsed)
            return value if sanitized == parsed else dumps(sanitized)
        value = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[邮箱已脱敏]", value)
        value = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[手机号已脱敏]", value)
        value = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~-]+", r"\1[已脱敏]", value)
        return value
    return value


def sign(payload):
    body = base64.urlsafe_b64encode(dumps(payload).encode()).decode().rstrip("=")
    return body + "." + hmac.new(load_secret(), body.encode(), hashlib.sha256).hexdigest()


def unsign(token):
    try:
        body, sig = token.rsplit(".", 1)
        expected = hmac.new(load_secret(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            fail(422, "游标或票据签名无效")
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if payload["expires"] <= time.time():
            fail(410, "游标或票据已过期")
        return payload
    except (ValueError, KeyError, TypeError):
        fail(422, "游标或票据格式无效")


def cursor(scope, trace, kind, offset, user):
    return sign({"scope": scope, "trace": trace, "kind": kind, "offset": offset,
                 "actor": str(user["uid"]), "expires": time.time() + 900})


def offset(token, scope, trace, kind, user):
    if not token:
        return 0
    data = unsign(token)
    if data.get("scope") != scope or data.get("trace") != trace or data.get("kind") != kind or data.get("actor") != str(user["uid"]):
        fail(409, "游标与查询范围不一致")
    value = data.get("offset")
    if not isinstance(value, int) or value < 0:
        fail(422, "游标偏移无效")
    return value


def llm_access(db, batch):
    if batch.source_system != "llm":
        return
    if batch.source_token_hash != sync.llm_owner():
        fail(403, "批次不属于当前配置的模型服务来源")
    url = os.getenv("AGENT_TRACE_LLM_DATABASE_URL")
    try:
        if url:
            # Avoid caching authorization: revoked tokens must stop subsequent reads/downloads.
            from sqlalchemy import create_engine
            engine = create_engine(url, echo=False)
            try:
                with engine.connect() as connection:
                    sync.check_llm_token(connection, batch.source_token_hash)
            finally:
                engine.dispose()
        else:
            sync.check_llm_token(db, batch.source_token_hash)
    except HTTPException:
        raise
    except Exception:
        fail(503, "模型服务来源授权暂不可验证")


def resolve(db, batch_id, params):
    batch = db.get(TraceBatch, batch_id)
    if not batch:
        fail(404, "审计批次不存在")
    llm_access(db, batch)
    sid = params.get("snapshot_id") or batch.active_snapshot_id
    snapshot = db.get(TraceSnapshot, sid)
    if not snapshot:
        fail(410, "审计快照已过期或不存在")
    if snapshot.batch_id != batch_id:
        fail(409, "快照不属于当前批次")
    version = params.get("projection_version") or VERSION
    projection = db.scalar(select(TraceProjection).where(TraceProjection.snapshot_id == sid,
                                                        TraceProjection.version == version))
    if not projection:
        fail(410, "审计投影已过期或不存在")
    return batch, {"batch_id": batch_id, "snapshot_id": sid, "projection_version": version}


def get_sample(db, scope, trace):
    row = db.scalar(select(TraceSample).where(TraceSample.snapshot_id == scope["snapshot_id"],
                                            TraceSample.trace_id == trace))
    if not row:
        fail(404, "样本不属于当前快照或不存在")
    return row


def sample_summary(row):
    p = row.payload
    return {"trace_id": row.trace_id, "display_id": row.display_id, "language": row.language,
            "event_count": p["event_count"], "call_count": p["call_count"], "duration_ms": p.get("duration_ms"),
            "risk_level": p.get("risk_level", "unknown"), "audit_decision": p.get("audit_decision", "unknown"),
            "risk_types": p.get("risk_types", []), "ingest_order": row.ingest_order,
            "data_origin": p["data_origin"], "sample_id": row.sample_id,
            "source_id": p["source"]["source_id"], "test_category": p.get("test_category")}


def sample_statement(scope, params):
    query = select(TraceSample).where(TraceSample.snapshot_id == scope["snapshot_id"])
    q = str(params.get("q", "")).strip()
    if len(q) > 100:
        fail(422, "搜索文本不能超过 100 字符")
    if q:
        literal = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = query.where(TraceSample.display_id.ilike("%" + literal + "%", escape="\\") |
                            TraceSample.sample_id.ilike("%" + literal + "%", escape="\\"))
    lang = params.get("language", "all")
    if lang != "all":
        if lang not in {"zh", "en", "other", "unknown"}:
            fail(422, "语言筛选无效")
        query = query.where(TraceSample.language == lang)
    for key, allowed in (("risk_level", {"high", "medium", "low", "none", "unknown"}),
                         ("audit_decision", {"risk_found", "no_risk_found", "pending", "error", "unknown"})):
        val = params.get(key, "all")
        if val != "all":
            if val not in allowed:
                fail(422, f"{key} 筛选无效")
            query = query.where(TraceSample.payload[key].as_string() == val)
    return query.order_by(TraceSample.ingest_order, TraceSample.trace_id)


def samples(db, scope, params, view):
    num = number(params, "page", 1, 2_147_483_647)
    size = number(params, "page_size", 50 if view == "tools" else 6)
    query = sample_statement(scope, params)
    total = db.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
    rows = list(db.scalars(query.offset((num - 1) * size).limit(size)))
    result = {"items": [sample_summary(r) for r in rows], "total": total, "page": num,
              "page_size": size, "total_pages": (total + size - 1) // size}
    result["context"] = {**scope, "q": params.get("q", ""), "language": params.get("language", "all"),
        "risk_level": params.get("risk_level", "all"), "audit_decision": params.get("audit_decision", "all"),
        "sort": "ingest_order_asc_trace_id_asc", "page_size": size,
        "filter_fingerprint": hashlib.sha256(dumps([scope, params.get("q", ""), params.get("language", "all"),
                                                   params.get("risk_level", "all"), params.get("audit_decision", "all")]).encode()).hexdigest()}
    return result


def workspace(db, params, view, user):
    include = params.get("include", "options")
    if include not in {"options", "dashboard"}:
        fail(422, "include 无效")
    query = select(TraceBatch).order_by(TraceBatch.created_at.desc(), TraceBatch.batch_id)
    q = str(params.get("option_q", "")).strip()
    if len(q) > 100:
        fail(422, "选项搜索过长")
    candidates = list(db.scalars(query))
    accessible = []
    for b in candidates:
        try:
            llm_access(db, b)
        except HTTPException:
            continue
        if not q or q.casefold() in (b.display_id + b.payload.get("name", "")).casefold():
            accessible.append({"batch_id": b.batch_id, "display_id": b.display_id,
                "name": b.payload.get("name", "数据价值分析"), "batch_version": 1,
                "active_snapshot_id": b.active_snapshot_id, "availability": "available"})
    options = page(accessible, number(params, "batch_option_page", 1, 2_147_483_647),
                   number(params, "option_page_size", 50))
    warnings = [{"code": "source_unavailable", "message": r.message, "panel": "options"}
                for r in db.scalars(select(TraceIngestCheckpoint).where(TraceIngestCheckpoint.status != "succeeded").limit(20))]
    output = {"view": view, "include": include, "batch_options": options,
              "permissions": permissions(user), "warnings": warnings}
    bid = params.get("batch_id")
    if view == "tools":
        if not bid:
            return {**output, "candidate_scope": None, "sample_options": page([], 1, 50)}
        target = db.get(TraceBatch, bid)
        if target and not target.active_snapshot_id and not params.get("snapshot_id"):
            llm_access(db, target)
            return {**output, "candidate_scope": None, "sample_options": page([], 1, 50)}
        _, scope = resolve(db, bid, params)
        return {**output, "candidate_scope": scope, "sample_options": samples(db, scope, params, view)}
    if include == "options":
        if bid:
            resolve(db, bid, params)
        return {**output, "resolved_selection": {"batch_id": bid} if bid else None}
    if not bid:
        fail(422, "查询批次必须提供 batch_id")
    target = db.get(TraceBatch, bid)
    if target and not target.active_snapshot_id and not params.get("snapshot_id"):
        llm_access(db, target)
        return {**output, "scope": None, "batch_summary": {"sample_count": 0, "by_language": []},
            "sample_page": page([], 1, 6), "selected_trace_id": None,
            "queried_at": now_shanghai().isoformat() + "+08:00", "availability": "unavailable", "reason_code": "no_published_snapshot"}
    _, scope = resolve(db, bid, params)
    selection = params.get("trace_id")
    sp = samples(db, scope, params, view)
    if selection:
        ordered = list(db.scalars(sample_statement(scope, params)))
        position = next((i for i, r in enumerate(ordered) if r.trace_id == selection), None)
        if position is None:
            fail(409, "目标样本不在当前筛选范围")
        sp = samples(db, scope, {**params, "page": position // sp["page_size"] + 1}, view)
    count = db.scalar(select(func.count()).select_from(TraceSample).where(TraceSample.snapshot_id == scope["snapshot_id"]))
    langs = db.execute(select(TraceSample.language, func.count()).where(TraceSample.snapshot_id == scope["snapshot_id"])
                      .group_by(TraceSample.language)).all()
    return {**output, "scope": {**scope, "permissions_version": "platform-v1", "schema_version": "1", "policy_version": scope["projection_version"]},
        "batch_summary": {"sample_count": count, "by_language": [{"code": l, "label": l, "count": c} for l, c in langs]},
        "sample_page": sp, "list_context": sp["context"], "selected_trace_id": selection or (sp["items"][0]["trace_id"] if sp["items"] else None),
        "queried_at": now_shanghai().isoformat() + "+08:00"}


def process_records(db, scope, trace, model):
    return [deepcopy(row.payload) for row in db.scalars(select(model).where(model.snapshot_id == scope["snapshot_id"],
        model.trace_id == trace).order_by(model.ordinal))]


def timing(payload, calls):
    executed = [c for c in calls if c["execution_status"] in {"succeeded", "failed"}]
    durations = [c.get("duration_ms") for c in executed]
    intervals = sorted((c["relative_start_ms"], c["relative_end_ms"]) for c in executed
                       if c.get("relative_start_ms") is not None and c.get("relative_end_ms") is not None)
    valid = all(0 <= a <= b and (payload.get("duration_ms") is None or b <= payload["duration_ms"]) for a, b in intervals)
    covered = valid and len(intervals) == len(executed)
    wall, end = 0, 0
    overlap = False
    for a, b in intervals:
        overlap |= a < end
        wall += max(0, b - max(a, end))
        end = max(end, b)
    total = payload.get("duration_ms")
    return {"total_duration_ms": total,
        "tool_duration_sum_ms": sum(d for d in durations if d is not None) if any(d is not None for d in durations) or not executed else None,
        "tool_wall_time_ms": wall if covered else None,
        "other_duration_ms": total - wall if covered and total is not None else None,
        "has_overlap": overlap, "coverage": "complete" if covered and all(d is not None for d in durations) else "partial",
        "availability": "available" if covered else "partial", "duration_source": "explicit" if total is not None else "not_provided"}


def tool_detail(db, batch, scope, trace, params, user):
    sample = get_sample(db, scope, trace)
    section = params.get("section", "overview")
    if section not in {"overview", "calls", "sequence", "selection"}:
        fail(422, "工具交互 section 无效")
    calls = process_records(db, scope, trace, TraceCall)
    calls = redact(calls, preserve_ids=True)
    lookup = {c["call_id"]: c for c in calls}
    cid = params.get("call_id")
    if cid and cid not in lookup:
        fail(404, "调用不属于当前样本")
    if section == "selection" and not cid:
        fail(422, "选中详情必须提供 call_id")
    selected = lookup.get(cid) if cid else next((c for c in calls if c["execution_status"] == "not_executed"),
               next((c for c in calls if c["execution_status"] == "failed"), calls[0] if calls else None))
    if selected:
        selected = call_preview(selected, 65536)
    if boolean(params, "include_raw"):
        if not permissions(user)["can_view_raw"]:
            fail(403, "没有原始事件查看权限")
        if selected:
            ids = {e["event_id"] for e in selected["evidence_chain"]}
            records = process_records(db, scope, trace, TraceEvent)
            selected = deepcopy(selected)
            selected["raw_events"] = page([{"event_id": e["event_id"], "raw_text": text_preview(dumps(redact(e, preserve_ids=True)), 16384)["text"],
                                           "truncated": text_preview(dumps(redact(e, preserve_ids=True)), 16384)["truncated"]} for e in records if e["event_id"] in ids],
                number(params, "raw_page", 1, 2_147_483_647), number(params, "raw_page_size", 20, 50))
    common = {"scope": scope, "trace_id": trace, "view": "tools", "section": section}
    if section == "selection":
        return {**common, "selection": selected}
    call_page = number(params, "call_page", 1, 2_147_483_647)
    call_size = number(params, "call_page_size", 20)
    if section == "overview" and not params.get("call_page") and selected:
        call_page = calls.index(lookup[selected["call_id"]]) // call_size + 1
    visible_calls = [call_preview(c, 4096) for c in calls]
    calls_page = page(visible_calls, call_page, call_size)
    if section == "calls":
        return {**common, "calls": calls_page, "selection_policy": "retain"}
    start = offset(params.get("sequence_cursor"), scope, trace, "sequence", user)
    if section == "overview" and not params.get("sequence_cursor") and selected:
        start = calls.index(lookup[selected["call_id"]]) // 200 * 200
    end = min(start + 200, len(calls))
    participant_ids = list(dict.fromkeys(c.get("to_participant_id", c["tool_name"]) for c in calls[start:end]))
    sequence = {"participants": [{"participant_id": "agent", "label": "智能体"},
                    *[{"participant_id": p, "label": p} for p in participant_ids]],
        "items": visible_calls[start:end], "total_calls": len(calls), "loaded_calls": end, "segment_offset": start,
        "complete": end >= len(calls), "next_cursor": cursor(scope, trace, "sequence", end, user) if end < len(calls) else None}
    if section == "sequence":
        return {**common, "sequence": sequence}
    counts = {s: sum(c["execution_status"] == s for c in calls) for s in ("succeeded", "failed", "not_executed", "unknown")}
    return {**common, "summary": {"batch_display_id": batch.display_id, "trace_display_id": sample.display_id,
            "language": sample.language, "test_category": sample.payload.get("test_category"),
            "risk_level": sample.payload.get("risk_level", "unknown"), "event_count": sample.payload["event_count"],
            "source": sample.payload["source"]},
        "metrics": {"call_count": len(calls), "tool_kind_count": len({c.get("tool_id", c["tool_name"]) for c in calls}),
            "success_count": counts["succeeded"], "error_count": counts["failed"],
            "not_executed_count": counts["not_executed"], "unknown_status_count": counts["unknown"],
            "overreach_count": sum(any(t["code"] == "overreach" for t in c["behavior_tags"]) for c in calls),
            "inefficient_count": sum(any(t["code"] == "inefficient" for t in c["behavior_tags"]) for c in calls)},
        "timing": timing(sample.payload, calls), "sequence": sequence, "calls": calls_page, "selection": selected,
        "sample_analysis": {"inefficiency_groups": sample.payload.get("inefficiency_groups", [])},
        "permissions": permissions(user), "data_origin": sample.payload["data_origin"],
        "warnings": [{"code": "generated_process", "message": "含模拟过程，不是实际风险审计证据"}] if sample.payload["data_origin"] != "actual" else []}


def execution_detail(db, scope, trace, params, user):
    sample = get_sample(db, scope, trace)
    section = params.get("section", "overview")
    if section not in {"overview", "events", "selection", "graph"}:
        fail(422, "执行路径 section 无效")
    mode = params.get("redaction_mode", "redacted")
    if mode not in {"raw", "redacted"}:
        fail(422, "脱敏模式无效")
    if mode == "raw" and not permissions(user)["can_view_raw"]:
        fail(403, "没有原文查看权限")
    events = process_records(db, scope, trace, TraceEvent)
    if mode != "raw":
        events = redact(events, preserve_ids=True)
    evidence = [deepcopy(e.payload) for e in db.scalars(select(TraceEvidence).where(
        TraceEvidence.snapshot_id == scope["snapshot_id"], TraceEvidence.trace_id == trace,
        TraceEvidence.projection_version == scope["projection_version"])) if e.payload.get("review_status") != "dismissed"]
    if mode != "raw":
        evidence = redact(evidence,preserve_ids=True)
    first_risk = next((e["event_id"] for e in events if any(x["event_id"] == e["event_id"] for x in evidence)), None)
    eid = params.get("event_id")
    if eid and not any(e["event_id"] == eid for e in events):
        fail(404, "事件不属于当前样本")
    if section == "selection" and not eid:
        fail(422, "事件详情必须提供 event_id")
    size = number(params, "event_page_size", 50)
    num = number(params, "event_page", 1, 2_147_483_647)
    if eid:
        num = next(i for i, e in enumerate(events) if e["event_id"] == eid) // size + 1
    elif section == "overview" and events:
        eid = first_risk or events[0]["event_id"]
        num = next(i for i, e in enumerate(events) if e["event_id"] == eid) // size + 1
    elif events[(num - 1) * size:num * size]:
        eid = events[(num - 1) * size]["event_id"]
    event_positions={e["event_id"]:i for i,e in enumerate(events)}
    def selection(event, units=16384):
        idx = event_positions[event["event_id"]]
        items = [x for x in evidence if x["event_id"] == event["event_id"]]
        content_version=hashlib.sha256(dumps([scope,trace,event["event_id"],mode,str(user["uid"]),event.get("source_excerpt"),event.get("reasoning_summary")]).encode()).hexdigest()
        texts={}
        for field in ("source_excerpt","reasoning_summary"):
            text=text_preview(event.get(field,""),units)
            text.update(highlights=[],content_version=content_version,permissions_version="platform-v1")
            text["next_cursor"]=cursor(scope,trace,"selection:"+event["event_id"]+":"+content_version+":"+field,text["loaded_units"],user) if text["truncated"] else None
            texts[field]=text
        texts["reasoning_summary"]["provenance"]="derived_summary"
        references=event.get("source_ref") or {}
        lineage_nodes=[]
        if references.get("source_id"):
            lineage_nodes.append({"id":references["source_id"],"kind":"source","label":"来源记录","event_id":None,"source_ref":references})
        lineage_nodes.append({"id":event["event_id"],"kind":"event","event_id":event["event_id"],"label":event["label"]})
        return {"event_id": event["event_id"], "display_id": event["display_id"], "ordinal": event["ordinal"],
            "graph_locator":{"cursor":cursor(scope,trace,"graph",idx//500*500,user) if idx>=500 else None},
            "previous_event_id": events[idx - 1]["event_id"] if idx else None,
            "next_event_id": events[idx + 1]["event_id"] if idx + 1 < len(events) else None,
            "content": {**texts,"source_ref":references,"content_version":content_version,
                        "redaction_mode": mode, "permissions_version": "platform-v1","availability":"available"},
            "risk_evidence": {"items": items, "total": len(items)},
            "lineage": {"nodes":lineage_nodes,"total_nodes":len(lineage_nodes),"complete":True,"unresolved_refs":[],"next_cursor":None,
                        "edges": [{"from_id":references["source_id"],"to_id":event["event_id"],"relation":"source_reference"}] if references.get("source_id") else [], "availability": "available"}}
    rendered = [{**{k: v for k, v in e.items() if k not in {"source_excerpt", "reasoning_summary"}},
                 "selection_preview": selection(e, 4096)} for e in events]
    sp = page(rendered, num, size)
    selected = next((selection(e) for e in events if e["event_id"] == eid), None)
    common = {"view": "execution", "section": section, "scope": {**scope, "permissions_version": "platform-v1"},
              "trace_id": trace, "first_risk_event_id": first_risk}
    if params.get("selection_cursor"):
        if section!="selection" or not selected:fail(422,"长内容游标要求selection和event_id")
        token=unsign(params["selection_cursor"])
        field=token.get("kind","").rsplit(':',1)[-1]
        if field not in {"source_excerpt","reasoning_summary"}:fail(422,"内容游标类别无效")
        version=selected["content"]["content_version"]
        kind="selection:"+eid+":"+version+":"+field
        start=offset(params["selection_cursor"],scope,trace,kind,user)
        event=next(e for e in events if e["event_id"]==eid)
        encoded=str(event.get(field,"")).encode("utf-16-le")
        if start*2>len(encoded):fail(422,"内容偏移越界")
        text=encoded[start*2:(start+16384)*2].decode("utf-16-le",errors="ignore")
        end=start+len(text.encode("utf-16-le"))//2;done=end*2>=len(encoded)
        return {**common,"event_id":eid,"selection_segment":{"kind":field,"text":text,"offset":start,"complete":done,
            "next_cursor":None if done else cursor(scope,trace,kind,end,user),"content_version":version}}
    if section in {"events", "selection"}:
        return {**common, "events": sp, "selection": selected}
    start = offset(params.get("graph_cursor"), scope, trace, "graph", user)
    end = min(start + 500, len(events))
    ids = {e["event_id"] for e in events[:end]}
    previous = {e["event_id"] for e in events[:start]}
    edges = [r.payload for r in db.scalars(select(TraceRelation).where(TraceRelation.snapshot_id == scope["snapshot_id"],
        TraceRelation.trace_id == trace)) if {r.payload["from_event_id"], r.payload["to_event_id"]} <= ids
        and not {r.payload["from_event_id"], r.payload["to_event_id"]} <= previous]
    graph = {"nodes": [{k: e[k] for k in ("event_id", "display_id", "ordinal", "label", "risk_level")} for e in events[start:end]],
             "edges": edges, "total_nodes": len(events), "loaded_nodes": end, "complete": end >= len(events),
             "next_cursor": cursor(scope, trace, "graph", end, user) if end < len(events) else None}
    if section == "graph":
        return {**common, "graph": graph}
    patterns = [{"pattern_id": e["evidence_id"], "label": e["risk_type"]["label"], "event_id": e["event_id"],
        "event_display_id": next(x["display_id"] for x in events if x["event_id"] == e["event_id"]),
        "snippet": e["match_pattern"], "evidence_id": e["evidence_id"]} for e in evidence]
    return {**common, "summary": sample_summary(sample), "graph": graph, "events": sp, "selection": selected,
        "risk_distribution": {"items": [{"code": "simulated_overreach", "label": "模拟越权", "event_count": len({e['event_id'] for e in evidence})}] if evidence else [],
                              "availability": "partial" if evidence else "unavailable"},
        "danger_patterns": page(patterns, number(params, "pattern_page", 1, 2_147_483_647), 20),
        "permissions": permissions(user), "data_origin": sample.payload["data_origin"]}
