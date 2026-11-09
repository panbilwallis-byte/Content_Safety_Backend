"""Statistics projections, list contexts and bounded UTF-8 content over frozen snapshots."""
from collections import Counter
from copy import deepcopy
import hashlib
import math

from sqlalchemy import select, func

from app.models.agent_traces import TraceBatch, TraceSample, TraceProjection, TraceStatisticsSample, TraceCall, TraceEvent
from app.services.agent_trace_generation import stable_id, dumps
from app.services import agent_trace_query as q

VERSION = "statistics_v1"
CATEGORIES = [("prompt_injection", "提示注入"), ("unauthorized_access", "越权访问"),
              ("privacy_leak", "隐私泄露"), ("harmful_content", "有害内容"), ("tool_misuse", "工具滥用"), ("other", "其他")]


def materialize(db):
    """Background migration: add an independent display projection, not new source facts."""
    pending = (select(TraceSample).outerjoin(TraceStatisticsSample,
        (TraceStatisticsSample.snapshot_id == TraceSample.snapshot_id) & (TraceStatisticsSample.trace_id == TraceSample.trace_id))
        .where(TraceStatisticsSample.id.is_(None)))
    first = db.scalar(pending.limit(1))
    missing = list(db.scalars(pending.where(TraceSample.snapshot_id==first.snapshot_id).limit(500))) if first else []
    snapshots = {r.snapshot_id for r in missing}
    for sample in missing:
        p = sample.payload
        generated = p.get("field_origins", {}).get("calls") == "generated" or p.get("data_origin") == "generated"
        # Persist generated test labels separately; never treat a value score as risk evidence.
        category = p.get("test_category")
        if isinstance(category, dict):
            category = category.get("code")
        category = category or (CATEGORIES[int(sample.trace_id[-4:], 16) % 6][0] if generated else "unknown")
        usage = p.get("token_usage") or {}
        values = {"input": usage.get("input_tokens"), "reasoning_recorded": usage.get("reasoning_tokens"),
                  "final_output": usage.get("final_output_tokens", usage.get("output_tokens"))}
        if usage.get("output_includes_reasoning") and values["final_output"] is not None:
            values["final_output"] = (values["final_output"] - values["reasoning_recorded"]
                if values["reasoning_recorded"] is not None and values["final_output"] >= values["reasoning_recorded"] else None)
        for key, value in values.items():
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                values[key] = None
        missing_fields = []
        availability = {"source": "available" if p.get("source") else "not_provided",
            "time": "available" if p.get("duration_ms") is not None else "not_provided",
            "reasoning": "available" if values["reasoning_recorded"] is not None else "not_provided"}
        missing_fields = [k for k, v in availability.items() if v != "available"]
        complete = all(v is not None for v in values.values())
        values.update(total_recorded=sum(v for v in values.values() if isinstance(v, int)),
            normalization_policy="mutually_exclusive_v1", availability="available" if complete else "partial")
        actual_decision = p.get("audit_decision", "unknown")
        display_decision = actual_decision
        if generated and actual_decision == "unknown":
            display_decision = "risk_found" if p.get("scenario_code") != "tools_1" else "no_risk_found"
        task_status = p.get("task_status") or ("failed" if sample.result.get("error_code") else "completed" if generated else "unknown")
        db.add(TraceStatisticsSample(id=stable_id("sts_", sample.snapshot_id, sample.trace_id), snapshot_id=sample.snapshot_id,
            trace_id=sample.trace_id, payload={"test_category": category, "token_usage": values,
                "missing_fields": missing_fields, "availability": availability, "task_status": task_status,
                "audit_decision": display_decision, "actual_audit_decision": actual_decision,
                "data_origin": "mixed" if generated else p.get("data_origin", "actual")}))
    db.flush()
    for sid in snapshots:
        remaining=db.scalar(select(func.count()).select_from(pending.where(TraceSample.snapshot_id==sid).subquery()))
        if not remaining and not db.scalar(select(TraceProjection).where(TraceProjection.snapshot_id==sid,TraceProjection.version==VERSION)):
            db.add(TraceProjection(projection_id=stable_id("proj_",sid,VERSION),snapshot_id=sid,version=VERSION,
                payload={"policy_version":VERSION,"data_origin":"mixed","notice":"含默认展示数据，非真实审计结论"}))
    db.flush()
    return len(missing)


def context(scope, params):
    language = params.get("language", "all")
    if language not in {"all", "zh", "en", "other", "unknown"}:
        q.fail(422, "语言筛选无效")
    selected = params.get("list_filter", "all")
    if selected not in {"all", "risk", "missing_fields"}:
        q.fail(422, "列表筛选无效")
    value = {**{k:scope[k] for k in ("batch_id","snapshot_id","projection_version")}, "language": language, "list_filter": selected,
             "page_size": q.number(params, "page_size", 6), "sort": "ingest_order_asc_trace_id_asc"}
    return {**value, "filter_fingerprint": hashlib.sha256(dumps(value).encode()).hexdigest()}


def rows(db, scope, params):
    ctx = context(scope, params)
    stmt = select(TraceSample, TraceStatisticsSample).join(TraceStatisticsSample,
        (TraceSample.snapshot_id == TraceStatisticsSample.snapshot_id) & (TraceSample.trace_id == TraceStatisticsSample.trace_id))
    stmt = stmt.where(TraceSample.snapshot_id == scope["snapshot_id"])
    if ctx["language"] != "all":
        stmt = stmt.where(TraceSample.language == ctx["language"])
    return list(db.execute(stmt.order_by(TraceSample.ingest_order, TraceSample.trace_id)))


def filtered(data, kind):
    return [(s, st) for s, st in data if kind == "all" or
            (kind == "risk" and st.payload["audit_decision"] == "risk_found") or
            (kind == "missing_fields" and st.payload["missing_fields"])]


def summary(s, st, scope, user):
    p, v = s.payload, st.payload
    return {**scope, "language": s.language, "trace_id": s.trace_id, "display_id": s.display_id,
        "source_id": p["source"]["source_id"], "filename": p["source"].get("filename", "价值分析记录"),
        "source_format": p["source"].get("format_code", "value_analysis"), "test_category": v["test_category"],
        "test_category_label": dict(CATEGORIES).get(v["test_category"], "未知"), "token_usage": v["token_usage"],
        "event_count": p["event_count"], "call_count": p["call_count"], "retry_count": p.get("retry_count", 0),
        "duration_ms": p.get("duration_ms"), "task_status": v["task_status"], "audit_decision": v["audit_decision"],
        "missing_fields": v["missing_fields"], "availability": v["availability"], "ingest_order": s.ingest_order,
        "data_origin": v["data_origin"], "actual_audit_decision": v["actual_audit_decision"],
        "allowed_actions": {"view_detail": True, "copy_json": True, "export_json": q.permissions(user)["can_export"]}}


def samples(db, scope, params, user):
    ctx = context(scope, params)
    data = filtered(rows(db, scope, params), ctx["list_filter"])
    result = q.page([summary(s, st, scope, user) for s, st in data], q.number(params, "page", 1, 2147483647), ctx["page_size"])
    return {**result, "context": ctx}


def distribution(counts, denominator=None):
    total = sum(counts.values()) if denominator is None else denominator
    return {"availability": "available" if total else "not_applicable", "denominator": total,
        "items": [{"code": k, "label": {**dict(CATEGORIES),"zh":"中文","en":"English","unknown":"未知","platform":"平台调用","scenario":"场景应用","external_import":"外部导入",
                   "succeeded":"成功","failed":"错误","not_executed":"拦截"}.get(k, k), "count": v,
                   "ratio_percent": 100 * v / total if total else None} for k, v in sorted(counts.items())]}


def aggregate(db, scope, params, user):
    data = rows(db, scope, params)
    n = len(data)
    calls = [{**r.payload,"_trace_id":r.trace_id} for r in db.scalars(select(TraceCall).where(TraceCall.snapshot_id == scope["snapshot_id"],
        TraceCall.trace_id.in_([s.trace_id for s, _ in data])))] if data else []
    risk = len(filtered(data, "risk"))
    missing = len(filtered(data, "missing_fields"))
    def metric(value, unit="count", valid=None):
        state="not_applicable" if not n else "unavailable" if value is None else "partial" if valid is not None and valid<n else "available"
        return {"value": value, "unit": unit, "availability": state,
                "valid_sample_count": n if valid is None else valid, "eligible_sample_count": n}
    compatible = [st.payload["token_usage"] for _, st in data if st.payload["token_usage"]["reasoning_recorded"] is not None
                  and st.payload["token_usage"]["final_output"] is not None]
    denom = sum(v["final_output"] for v in compatible)
    totals = {k: sum(st.payload["token_usage"][k] or 0 for _, st in data) for k in ("input", "reasoning_recorded", "final_output")}
    steps = [s.payload["event_count"] for s, _ in data if s.payload.get("event_count") is not None]
    audit_valid=sum(st.payload["audit_decision"] in {"risk_found","no_risk_found"} for _,st in data)
    token_valid=sum(any(st.payload["token_usage"][key] is not None for key in ("input","reasoning_recorded","final_output")) for _,st in data)
    kpis = {"sample_count": metric(n), "risk_sample_count": metric(risk if audit_valid else None,valid=audit_valid),
        "recorded_tokens": metric(sum(totals.values()) if token_valid else None, "token",token_valid),
        "reasoning_output_ratio": metric(sum(v["reasoning_recorded"] for v in compatible) / denom if denom else None, "ratio", len(compatible)),
        "tool_call_count": metric(len(calls)), "tool_type_count": metric(len({c.get("tool_id", c["tool_name"]) for c in calls})),
        "average_steps": metric(sum(steps) / len(steps) if steps else None, "step", len(steps)),
        "task_success_rate": metric(100 * sum(st.payload["task_status"] == "completed" for _, st in data) / n if n else None, "percent")}
    times = sorted(s.payload["duration_ms"] for s, _ in data if isinstance(s.payload.get("duration_ms"), (int, float)) and s.payload["duration_ms"] >= 0)
    limits = [(0,1000), (1000,2000), (2000,4000), (4000,6000), (6000,8000), (8000,10000), (10000,None)]
    durations = {"availability": "available" if times else "unavailable", "valid_count": len(times),
        "missing_count": sum(s.payload.get("duration_ms") is None for s, _ in data), "invalid_count": n - len(times) - sum(s.payload.get("duration_ms") is None for s, _ in data),
        "total_count": n, "unit": "ms", "quantile_method": "nearest_rank",
        "p50_ms": times[math.ceil(.5 * len(times))-1] if times else None, "p95_ms": times[math.ceil(.95 * len(times))-1] if times else None,
        "buckets": [{"code": str(i), "label": f"{lo//1000}–{hi//1000}" if hi else ">10", "lower_ms": lo, "upper_ms": hi,
                     "lower_inclusive": i == 0, "upper_inclusive": True,
                     "count": sum((t >= lo if i == 0 else t > lo) and (hi is None or t <= hi) for t in times)} for i,(lo,hi) in enumerate(limits)]}
    result_counts = Counter(c["execution_status"] for c in calls)
    terminal = sum(result_counts[k] for k in ("succeeded", "failed", "not_executed"))
    results = {**distribution(Counter({k: result_counts[k] for k in ("succeeded", "failed", "not_executed")}), terminal),
               "total_call_count": len(calls), "terminal_count": terminal, "non_terminal_count": 0, "unknown_count": result_counts["unknown"]}
    results["items"].sort(key=lambda item:("succeeded","failed","not_executed").index(item["code"]))
    names = Counter(c["tool_name"] for c in calls)
    token_components = [{"code": k, "label": label, "count": totals[k], "ratio_percent": 100 * totals[k] / sum(totals.values()) if sum(totals.values()) else 0,
                         "valid_sample_count": sum(st.payload["token_usage"][k] is not None for _, st in data), "eligible_sample_count": n}
                        for k, label in (("input","输入 Token"),("reasoning_recorded","已记录推理 Token"),("final_output","最终输出 Token"))]
    by_category = Counter(st.payload["test_category"] for _, st in data)
    category_for = {s.trace_id: st.payload["test_category"] for s, st in data}
    matrix = Counter()
    for c in calls:
        for code in { {"overreach":"unauthorized","inefficient":"duplicate"}.get(t["code"],t["code"]) for t in c.get("behavior_tags", []) }:
            matrix[(category_for[c["_trace_id"]], code, c["_trace_id"], c["event_id"])] = 1
    columns = [("unauthorized","越权"),("argument_error","参数错误"),("timeout","超时"),("duplicate","重复"),("retry","重试")]
    charts = {"test_category_distribution": distribution(by_category),
        "token_composition": {"availability": "available" if n else "not_applicable", "mutually_exclusive": True,
            "normalized_policy": "mutually_exclusive_v1", "compatible_sample_count": len(compatible),
            "incompatible_sample_count": n-len(compatible), "total_recorded": sum(totals.values()), "components": token_components},
        "tool_ranking": {"availability": "available" if calls else "not_applicable", "total_call_count": len(calls), "tool_type_count": len(names),
            "ranking_order": "count_desc_name_asc", "items": [{"tool_code": k, "label": k, "count": v, "is_other": False} for k,v in sorted(names.items(), key=lambda x:(-x[1],x[0]))]},
        "duration_distribution": durations, "tool_result_distribution": results,
        "behavior_matrix": {"availability": "available" if n else "not_applicable", "row_dimension": "test_category", "unit": "event_occurrence", "overlap_allowed": True,
            "columns": [{"code": c, "label": l} for c,l in columns], "rows": [{"category_code": cat, "label": dict(CATEGORIES).get(cat,cat),
                "cells": [{"behavior_code": c, "count": sum(v for (ca,co,_,_),v in matrix.items() if ca == cat and co == c)} for c,_ in columns]} for cat in sorted(by_category)]},
        "source_distribution": distribution(Counter("external_import" if s.payload["source"].get("format_code") != "value_analysis" else "platform" for s,_ in data)),
        "language_distribution": distribution(Counter(s.language for s,_ in data))}
    completeness = {}
    for field in ("source", "time", "reasoning"):
        available = sum(st.payload["availability"][field] == "available" for _,st in data)
        completeness[field] = {"available_count": available, "missing_count": n-available, "invalid_count": 0, "not_applicable_count": 0,
            "applicable_count": n, "total_count": n, "rate_percent": available * 100/n if n else None, "availability": "available" if available == n and n else "partial" if n else "not_applicable"}
    completeness.update(required_fields=["source","time","reasoning"], missing_union_count=missing,
        overlap_summary={"time_missing_count": completeness["time"]["missing_count"], "reasoning_missing_count": completeness["reasoning"]["missing_count"],
                         "source_missing_count": completeness["source"]["missing_count"], "time_and_reasoning_count": sum({"time","reasoning"} <= set(st.payload["missing_fields"]) for _,st in data)})
    return {"kpis": kpis, "charts": charts, "completeness": completeness, "list_counts": {"all":n,"risk":risk,"missing_fields":missing}}


def workspace(db, params, user):
    options = q.workspace(db, {"include":"options", "option_q":params.get("option_q",""),
        "batch_option_page":params.get("option_page",1), "option_page_size":params.get("option_page_size",50)}, "tools", user)["batch_options"]
    for b in options["items"]:
        sid = b["active_snapshot_id"]
        b["sample_count"] = db.scalar(select(func.count()).select_from(TraceSample).where(TraceSample.snapshot_id == sid))
        b["languages"] = [{"code": l, "count": n} for l,n in db.execute(select(TraceSample.language,func.count()).where(TraceSample.snapshot_id==sid).group_by(TraceSample.language))]
    bid = params.get("batch_id") or next((b["batch_id"] for b in options["items"] if b["active_snapshot_id"]), None)
    default = {"scope":None,"resolved_selection":None,"resolution":"empty","batch_options":options,
               "language_options":[],"sample_page":{**q.page([],1,q.number(params,"page_size",6)),"context":{}}, "warnings":[]}
    if not bid:
        empty_scope={"batch_id":"","snapshot_id":"","projection_version":VERSION}
        return {**default,**aggregate(db,empty_scope,{},user),"list_context":context(empty_scope,{}),
            "permissions":{"can_view_statistics":True,"can_list_samples":True,"can_view_detail":True,"can_view_raw":q.permissions(user)["can_view_raw"],"can_copy_json":True,"can_export":False},
            "export_options":{"kinds":[],"contents":[],"required_contents":[],"redaction_profiles":["standard"],"default_scope":"filtered","max_selected_trace_ids":100},
            "module_availability":{v:{"availability":"unavailable","reason":"暂无已发布批次"} for v in ("ingest","statistics","execution","tools")}}
    batch = db.get(TraceBatch,bid)
    if not batch:
        q.fail(404,"批次不存在")
    if not batch.active_snapshot_id:
        q.llm_access(db,batch)
        return default
    _,scope=q.resolve(db,bid,{**params,"projection_version":params.get("projection_version") or VERSION})
    scope={**scope,"language":params.get("language","all"),"schema_version":"1","policy_version":VERSION}
    result=aggregate(db,scope,params,user)
    sample_page=samples(db,scope,params,user)
    perms=q.permissions(user)
    return {**default,**result,"scope":scope,"resolved_selection":scope,"resolution":"resolved" if params.get("batch_id") else "defaulted",
        "language_options":[{"code":"all","label":"全部语言","count":sum(x["count"] for x in next((b["languages"] for b in options["items"] if b["batch_id"]==bid),[]))},
            *[{"code":l,"label":l,"count":n} for l,n in db.execute(select(TraceSample.language,func.count()).where(TraceSample.snapshot_id==scope["snapshot_id"]).group_by(TraceSample.language))]],
        "sample_page":sample_page,"list_context":sample_page["context"],
        "permissions":{"can_view_statistics":True,"can_list_samples":True,"can_view_detail":True,"can_view_raw":perms["can_view_raw"],"can_copy_json":True,"can_export":perms["can_export"]},
        "export_options":{"kinds":[{"export_kind":k,"formats":[f],"availability":"available"} for k,f in (("view","png"),("package","zip"),("sample_json","json"))],
            "contents":[{"code":c,"label":c,"availability":"available"} for c in ("statistics_png","stats_summary","sample_table","normalized_json","manifest")],
            "required_contents":["manifest"],"redaction_profiles":["standard"],"default_scope":"filtered","max_selected_trace_ids":100},
        "module_availability":{v:{"availability":"available"} for v in ("ingest","statistics","execution","tools")},
        "warnings":[{"code":"mixed_display_data","message":"含模拟过程及默认展示指标；并非实际风险检测结论。","panel":"all"}]}


def normalized(db,scope,sample):
    return {"trace_id":sample.trace_id,"display_id":sample.display_id,"sample_id":sample.sample_id,
            "value_result":sample.result,"data_origin":sample.payload.get("data_origin"),
            "events":q.process_records(db,scope,sample.trace_id,TraceEvent),"calls":q.process_records(db,scope,sample.trace_id,TraceCall)}


def detail(db,scope,trace,params,user):
    scope={**scope,"language":params.get("language","all")}
    ctx=context(scope,params)
    all_rows=filtered(rows(db,scope,params),ctx["list_filter"])
    index=next((i for i,(s,_) in enumerate(all_rows) if s.trace_id==trace),None)
    if index is None:
        q.get_sample(db,scope,trace)
        q.fail(409,"样本不在当前筛选范围")
    sample,st=all_rows[index]
    value=q.redact(normalized(db,scope,sample),preserve_ids=True)
    text=dumps(value)
    raw=(sample.payload.get("raw_text") or dumps(sample.result)) if q.permissions(user)["can_view_raw"] else "无原文查看权限"
    version=hashlib.sha256(dumps([scope,trace,str(user["uid"]),"standard",text,raw]).encode()).hexdigest()
    def block(content,start,limit):
        remaining=content.encode()[start:start+limit]
        decoded=remaining.decode("utf-8",errors="ignore")
        end=start+len(decoded.encode())
        return decoded,end,end>=len(content.encode())
    preview,loaded,complete=block(text,0,262144)
    raw_preview,raw_loaded,raw_complete=block(raw,0,65536)
    def next_cursor(kind,end,done):
        return None if done else q.cursor(scope,trace,kind+":"+version,end,user)
    def neighbor(i):
        return None if i<0 or i>=len(all_rows) else {"trace_id":all_rows[i][0].trace_id,"display_id":all_rows[i][0].display_id,"page":i//ctx["page_size"]+1}
    try:
        import json
        json.loads(preview)
        valid=True
    except ValueError:
        valid=False
    response={**scope,"trace_id":trace,"display_id":sample.display_id,"summary":summary(sample,st,scope,user),
        "audit":{"decision":st.payload["audit_decision"],"risk_types":[],"evidence_refs":[],"policy_version":VERSION,"availability":"partial"},
        "sample_json":{"availability":"available","json_text":preview,"is_valid_json":valid,"complete":complete,"truncated":not complete,
            "loaded_bytes":loaded,"total_bytes":len(text.encode()),"content_version":version,"content_sha256":hashlib.sha256(text.encode()).hexdigest(),
            "next_cursor":next_cursor("sample",loaded,complete),"preview_scope":{"is_complete":complete,"messages_shown":0,"messages_total":0,
                "events_shown":sample.payload["event_count"] if complete else 0,"events_total":sample.payload["event_count"]}},
        "raw_preview":{"availability":"available" if q.permissions(user)["can_view_raw"] else "unavailable","raw_text":raw_preview,"kind":"record","line_start":1,"line_end":raw_preview.count('\n')+1,
            "total_lines":raw.count('\n')+1,"truncated":not raw_complete,"next_cursor":next_cursor("raw",raw_loaded,raw_complete)},
        "mapping":{"format_code":sample.payload["source"].get("format_code","value_analysis"),"format_label":sample.payload["source"]["format_label"],
            "mapping_version":"1","rows":[{"source_path":"result","target_path":"value_result","description":"保存的价值结果或源记录","state":"mapped"}]},
        "source_ref":{"source_id":sample.payload["source"]["source_id"],"record_index":sample.ingest_order+1,"source_path":"value_result"},
        "navigation":{"previous":neighbor(index-1),"next":neighbor(index+1),"position":index+1,"total":len(all_rows),"page":index//ctx["page_size"]+1,
            "page_size":ctx["page_size"],"context":ctx},
        "permissions":{"view_detail":True,"view_raw":q.permissions(user)["can_view_raw"],"copy_json":True,"export_json":q.permissions(user)["can_export"],"redaction_profile":"standard"},"content_chunk":None}
    if params.get("content_cursor"):
        kind=params.get("representation")
        if kind not in {"sample","raw"}:
            q.fail(422,"内容表示类型无效")
        if kind=="raw" and not q.permissions(user)["can_view_raw"]:q.fail(403,"没有原文查看权限")
        start=q.offset(params["content_cursor"],scope,trace,kind+":"+version,user)
        chunk,end,done=block(text if kind=="sample" else raw,start,262144 if kind=="sample" else 65536)
        response["content_chunk"]={"text":chunk,"offset_bytes":start,"next_cursor":next_cursor(kind,end,done),"content_version":version,"complete":done}
    return response
