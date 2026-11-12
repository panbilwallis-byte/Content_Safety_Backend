"""Shared frozen exports for ingestion, statistics and execution views."""
import csv
import hashlib
import html
import io
import json
import os
from pathlib import Path
import zipfile

from sqlalchemy import select

from app.models.agent_traces import TraceSample, TraceBinding, TraceEvidence
from app.services import agent_trace_query as q, agent_trace_statistics as stats, agent_trace_ingest as ingest
from app.services.agent_trace_generation import dumps


def export_rows(db,scope,data):
    spec=data["scope"];view=data["view"]
    if view=="statistics":
        params={"language":spec.get("language",data.get("language","all")),"list_filter":spec.get("list_filter","all")}
        rows=[s for s,_ in stats.filtered(stats.rows(db,scope,params),params["list_filter"])]
    elif spec["type"]=="filtered" and view=="ingest":
        rows=ingest.selected_rows(db,scope,{k:v for k,v in spec.items() if k in {"source_id","issue_type","q"} and v is not None})
    elif spec["type"]=="filtered" and view=="execution":
        rows=list(db.scalars(q.sample_statement(scope,{k:v for k,v in spec.items() if k in {"q","language","risk_level","audit_decision"} and v is not None})))
    else:
        rows=list(db.scalars(select(TraceSample).where(TraceSample.snapshot_id==scope["snapshot_id"]).order_by(TraceSample.ingest_order,TraceSample.trace_id)))
    if spec["type"]=="sample":
        ids=set(spec["trace_ids"])
        found=[s for s in rows if s.trace_id in ids]
        if len(found)!=len(ids):q.fail(404,"导出样本不存在或不在授权范围")
        rows=found
    return rows


def validate(db,data,user):
    if not q.permissions(user)["can_export"]:q.fail(403,"当前账号不能导出")
    view=data.get("view")
    if view not in {"ingest","statistics","execution"}:q.fail(422,"轨迹导出视图无效")
    kind=data.get("export_kind")
    if (kind,data.get("format")) not in {("view","png"),("package","zip"),("sample_json","json")}:
        q.fail(422,"导出类型或格式不支持")
    if kind=="sample_json" and view!="statistics":q.fail(422,"此视图不支持单样本JSON导出")
    for key in ("batch_id","snapshot_id"):
        if not isinstance(data.get(key),str) or not data[key]:q.fail(422,key+"必填")
    spec=data.get("scope")
    if not isinstance(spec,dict) or spec.get("type") not in {"batch","filtered","sample"}:q.fail(422,"导出范围无效")
    ids=spec.get("trace_ids",[])
    if not isinstance(ids,list) or len(ids)>100 or any(not isinstance(i,str) for i in ids):q.fail(422,"样本标识列表无效")
    if spec["type"]=="sample" and len(ids)!=1:q.fail(422,"sample范围要求一个样本")
    if kind=="sample_json" and spec["type"]!="sample":q.fail(422,"完整样本JSON要求sample范围")
    if data.get("redaction_profile","standard")!="standard" or data.get("anonymize_ids",True) is not True:q.fail(422,"要求standard脱敏和匿名化")
    contents=data.get("contents")
    allowed={view+"_png"} if kind=="view" else {"normalized_json"} if kind=="sample_json" else {
        "normalized_json","evidence_index","manifest","offline_html","error_report", "stats_summary","sample_table"}
    if not isinstance(contents,list) or not contents or any(not isinstance(c,str) or c not in allowed for c in contents):q.fail(422,"不支持的导出内容")
    if kind=="package" and "manifest" not in contents:q.fail(422,"ZIP必须包含manifest")
    for key in ("selected_trace_id","selected_event_id"):
        if data.get(key) is not None and not isinstance(data[key],str):q.fail(422,key+"必须为字符串")
    params={**data}
    if view=="statistics":params.setdefault("projection_version",stats.VERSION)
    _,scope=q.resolve(db,data["batch_id"],params)
    frozen={**data,**scope,"redaction_profile":"standard","anonymize_ids":True}
    selected=export_rows(db,scope,frozen)
    if data.get("selected_trace_id"):
        row=q.get_sample(db,scope,data["selected_trace_id"])
        if spec["type"]=="sample" and row.trace_id!=ids[0]:q.fail(409,"选中样本与范围不匹配")
        if data.get("selected_event_id"):
            from app.models.agent_traces import TraceEvent
            if not any(e["event_id"]==data["selected_event_id"] for e in q.process_records(db,scope,row.trace_id,TraceEvent)):q.fail(404,"选中事件不存在")
    state=data.get("ui_state",{})
    if not isinstance(state,dict) or len(dumps(state))>20000:q.fail(422,"ui_state无效或过大")
    return frozen


def png(document,view):
    from PIL import Image,ImageDraw,ImageFont
    candidates=[os.getenv("AGENT_TRACE_FONT",""),"C:/Windows/Fonts/msyh.ttc","/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc","/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"]
    font=next((v for v in candidates if v and Path(v).is_file()),None)
    if not font:raise ValueError("PNG中文字体不可用，请配置AGENT_TRACE_FONT")
    body=ImageFont.truetype(font,20);title=ImageFont.truetype(font,30)
    labels={"ingest":"轨迹接入与校验","statistics":"统计指标","execution":"思维路径"}
    lines=[("智能体轨迹审计 · "+labels[view],True), ("固定快照报告；含默认展示数据时不作为实际风险审计结论",False),
           ("批次样本总数："+str(document.get("context_sample_count",len(document["samples"])))+"；当前明细范围："+str(len(document["samples"])),False)]
    if view=="statistics":
        for key,value in document["statistics"]["kpis"].items():lines.append((f"{key}: {value['value']} {value['unit']}",False))
    for sample in document["samples"][:100]:
        lines.append((sample["summary"]["display_id"]+" / "+sample["summary"]["language"]+" / "+sample["data_origin"],False))
        if view=="execution":
            lines.extend((f"{e['display_id']} {e['label']}  {e.get('relative_time_ms')} ms",False) for e in sample["events"][:100])
        elif view=="ingest":lines.append(("事件："+str(len(sample["events"]))+"；工具调用："+str(len(sample["calls"])),False))
    if len(document["samples"])>100:lines.append(("报告图片仅列前100个样本，完整明细见结果包",False))
    image=Image.new("RGB",(1500,max(300,55*len(lines)+50)),"#f3f7ff");draw=ImageDraw.Draw(image)
    for index,(line,is_title) in enumerate(lines):draw.text((30,25+55*index),line[:110],font=title if is_title else body,fill="#17365e")
    out=io.BytesIO();image.save(out,"PNG");return out.getvalue()


def build(db,task):
    from app.services.agent_trace_export import anonymize
    data=task.input_data;_,scope=q.resolve(db,data["batch_id"],data)
    rows=export_rows(db,scope,data)
    if data["export_kind"]=="view" and data["view"]=="execution" and data.get("selected_trace_id"):
        rows=[s for s in rows if s.trace_id==data["selected_trace_id"]]
    from sqlalchemy import func
    total=db.scalar(select(func.count()).select_from(TraceSample).where(TraceSample.snapshot_id==scope["snapshot_id"]))
    document={"scope":scope,"view":data["view"],"samples":[],"context_sample_count":total,
              "requested_scope":data["scope"],"notice":"含模拟过程或默认展示数据时，不作为真实审计结论"}
    for row in rows:
        value=stats.normalized(db,scope,row)
        document["samples"].append({**value,"summary":q.sample_summary(row),"data_origin":row.payload.get("data_origin","actual"), "rule_audit":row.payload.get("rule_audit"), "rule_findings":row.payload.get("rule_findings",[])})
    if data["view"]=="statistics":document["statistics"]=stats.aggregate(db,scope,{"language":data["scope"].get("language",data.get("language","all"))},{"uid":task.config["owner_id"]})
    document["errors"]=[b.payload.get("error_examples",[]) for b in db.scalars(select(TraceBinding).where(TraceBinding.batch_id==scope["batch_id"]))]
    if rows:document=anonymize(document,task.task_id,rows[0])
    else:document=q.redact(document,preserve_ids=True)
    if data["export_kind"]=="view":return png(document,data["view"]),"agent-trace-"+data["view"]+".png","image/png"
    if data["export_kind"]=="sample_json":return dumps(document["samples"][0]).encode(),"agent-trace-sample.json","application/json"
    files={};contents=set(data["contents"])
    if "normalized_json" in contents:files["normalized.json"]=dumps(document).encode()
    if "stats_summary" in contents:files["stats-summary.json"]=dumps(document.get("statistics",{})).encode()
    if "sample_table" in contents:
        output=io.StringIO(newline="");writer=csv.writer(output);writer.writerow(["trace_id","language","event_count","call_count","duration_ms","data_origin"])
        for sample in document["samples"]:
            row=[sample["summary"].get(k,"") for k in ("trace_id","language","event_count","call_count","duration_ms","data_origin")]
            writer.writerow(["'"+v if isinstance(v,str) and v[:1] in {'=','+','-','@'} else v for v in row])
        files["samples.csv"]=output.getvalue().encode("utf-8-sig")
    if "evidence_index" in contents:
        files["evidence-index.json"]=dumps([{ "trace_id":s["trace_id"],"rule_findings":s.get("rule_findings",[]), "calls":[{"call_id":c["call_id"],"event_id":c["event_id"],"evidence_chain":c["evidence_chain"]} for c in s["calls"]]} for s in document["samples"]]).encode()
    if "offline_html" in contents:files["report.html"]=("<!doctype html><meta charset='utf-8'><title>轨迹审计报告</title><h1>智能体轨迹审计</h1><p>"+html.escape(document["notice"])+"</p><pre>"+html.escape(json.dumps(document,ensure_ascii=False,indent=2))+"</pre>").encode()
    if "error_report" in contents:files["errors.json"]=dumps(document["errors"]).encode()
    manifest={"schema_version":"1",**document["scope"],"view":data["view"],"sample_count":len(rows),
        "context_sample_count":total,"content_scope":document["requested_scope"],"contents":sorted(contents),
        "redaction_profile":"standard","anonymize_ids":True,"notice":document["notice"],"ui_version":data.get("ui_version","1"),
        "files":[{"path":name,"size_bytes":len(body),"sha256":hashlib.sha256(body).hexdigest()} for name,body in files.items()]}
    files["manifest.json"]=dumps(manifest).encode();out=io.BytesIO()
    with zipfile.ZipFile(out,"w",zipfile.ZIP_DEFLATED) as archive:
        for name,body in files.items():archive.writestr(name,body)
    return out.getvalue(),"agent-trace-"+data["view"]+".zip","application/zip"
