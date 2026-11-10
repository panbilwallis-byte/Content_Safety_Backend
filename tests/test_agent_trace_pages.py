"""The remaining three pages: real adapters, statistics contexts and frozen exports."""
from copy import deepcopy
import hashlib
import io
import json
from uuid import uuid4
import zipfile

import pytest
from sqlalchemy import select,func

from test_agent_traces import env, ROOT
from app.services import agent_trace_ingest as ingest, agent_trace_statistics as stats, agent_trace_query as q
from app.models.agent_traces import TraceSample,TraceAsset,TraceBinding,TraceProjection,TraceEvent,TraceCall
from app.routers import files
from app.core.auth import create_token


@pytest.fixture
def pages(env,monkeypatch,tmp_path):
    monkeypatch.setattr(files,"SessionLocal",env["sessions"])
    target=tmp_path/"uploads";target.mkdir()
    monkeypatch.setattr(files,"UPLOAD_DIR",target)
    original=ingest.run
    monkeypatch.setattr(ingest,"run",lambda tid,*args:original(tid,env["sessions"]))
    with env["sessions"]() as db:
        stats.materialize(db);db.commit()
    return env


def post(pages,path,data=None,key=None,**kwargs):
    return pages["client"].post(path,json=data,headers={"X-Request-Id":key or uuid4().hex},**kwargs)


def create(pages):
    r=post(pages,ROOT+"/batches",{"name":"上传测试批次"})
    assert r.status_code==200,r.text
    return r.json()["data"]


def upload(pages,text,name="test.jsonl",key=None):
    r=pages["client"].post("/api/v1/files",files={"file":(name,text.encode(),"application/json")},headers={"X-Request-Id":key or uuid4().hex})
    assert r.status_code==200,r.text
    return r.json()["data"]["file_id"]


OPENAI={"messages":[{"role":"user","content":"Keep User_ID unchanged"},{"role":"assistant","tool_calls":[{"id":"src_1","function":{"name":"web.search","arguments":'{"User_ID":42,"tool_call_id":"unchanged"}'}}]},
                    {"role":"tool","tool_call_id":"src_1","content":"public result"}]}
from app.services.agent_trace_trajectory import EXAMPLE as TRAJECTORY
LANGCHAIN={"runs":[{"id":"root_1","parent_run_id":None,"run_type":"chain","inputs":{"q":"one"},"outputs":{"answer":"one"}},
                    {"id":"tool","parent_run_id":"root_1","run_type":"tool","name":"web.search","inputs":{"q":"one"},"outputs":{"text":"one"}},
                    {"id":"root_2","parent_run_id":None,"run_type":"chain","inputs":{"q":"two"},"outputs":{"answer":"two"}}]}


def parse(pages,batch,file_ids,explicit=None):
    bid=batch["batch_id"]
    r=post(pages,ROOT+f"/batches/{bid}/files",{"expected_version":batch["batch_version"],"files":[{"file_id":fid,"format_override":"auto"} for fid in file_ids]})
    assert r.status_code==200,r.text
    b=r.json()["data"]["batch"]
    inputs={"batch_id":bid,"expected_version":b["batch_version"],"mode":"parse_pending"}
    if explicit is not None:inputs["file_ids"]=explicit
    r=post(pages,"/api/v1/tasks/execute",{"capability_code":"agent_trace_ingest","name":"接入解析", "input":inputs})
    assert r.status_code==200,r.text
    task=pages["client"].get("/api/v1/tasks/"+r.json()["data"]["task_id"])
    assert task.status_code==200,task.text
    return task.json()["data"]


def test_statistics_failure_does_not_relabel_published_ingest(pages, monkeypatch):
    batch = create(pages)
    fid = upload(pages, json.dumps(OPENAI))
    def unavailable(db):
        raise RuntimeError("statistics temporarily unavailable")
    monkeypatch.setattr(stats, "materialize", unavailable)
    task = parse(pages, batch, [fid])
    assert task["status"] == "succeeded", task
    workspace = pages["client"].get(ROOT + "/workspace", params={"batch_id": batch["batch_id"]})
    assert workspace.status_code == 200
    assert workspace.json()["data"]["summary"]["parsed_trace_count"] == 1


@pytest.mark.parametrize("record,fmt,total",[(OPENAI,"openai_messages",1),(TRAJECTORY,"agent_trajectory",1),(LANGCHAIN,"langchain_trace",2)])
def test_real_adapter_and_four_view_identity(pages,record,fmt,total):
    batch=create(pages);fid=upload(pages,json.dumps(record,ensure_ascii=False))
    task=parse(pages,batch,[fid])
    assert task["status"]=="succeeded",task
    scope={"batch_id":batch["batch_id"],"snapshot_id":task["result"]["snapshot_id"]}
    w=pages["client"].get(ROOT+"/workspace",params=scope).json()["data"]
    assert w["summary"]["parsed_trace_count"]==total
    assert w["summary"]["by_format"][0]["code"]==fmt
    assert w["summary"]["file_count"]==1
    trace=w["resolved_selection"]["trace_id"]
    d=pages["client"].get(f"{ROOT}/batches/{batch['batch_id']}/samples/{trace}",params={**scope,"event_types":"tool_call,tool_result"})
    assert d.status_code==200,d.text
    detail=d.json()["data"]
    if detail["events"]["total"] and fmt=="openai_messages":
        assert "User_ID" in (detail["normalized_event"].get("input_json") or "User_ID")
    assert detail["source_id"]==fid
    # All projections share the same sample identity, while statistics has its own version.
    s=pages["client"].get(ROOT+"/statistics",params={**scope,"language":"all"})
    assert s.status_code==200,s.text
    assert s.json()["data"]["list_counts"]["all"]==total
    t=pages["client"].get(f"{ROOT}/batches/{batch['batch_id']}/samples/{trace}",params={**scope,"view":"tools"})
    assert t.status_code==200,t.text
    assert t.json()["data"]["trace_id"]==trace


def test_statistics_full_contract_filters_and_navigation(pages):
    client=pages["client"];w=client.get(ROOT+"/statistics",params={"batch_id":pages["batch"]}).json()["data"]
    assert w["scope"]["projection_version"]==stats.VERSION
    assert w["sample_page"]["page_size"]==6
    assert w["list_counts"]["all"]==8
    assert len(w["kpis"])==8 and len(w["charts"])==8
    assert w["charts"]["tool_ranking"]["total_call_count"]==56
    ctx=w["sample_page"]["context"]
    assert ctx["language"]=="all" and ctx["list_filter"]=="all" and ctx["page_size"]==6
    for field in ("source","time","reasoning"):
        metric=w["completeness"][field]
        assert metric["available_count"]+metric["missing_count"]+metric["invalid_count"]==metric["applicable_count"]
    scoped={**w["scope"],"view":"statistics"}
    risk=client.get(f"{ROOT}/batches/{pages['batch']}/samples",params={**scoped,"list_filter":"risk"}).json()["data"]
    assert risk["total"]==w["list_counts"]["risk"]
    assert client.get(ROOT+"/statistics",params={"batch_id":pages["batch"],"language":"zh"}).json()["data"]["list_counts"]["all"]==4
    trace=w["sample_page"]["items"][5]["trace_id"]
    d=client.get(f"{ROOT}/batches/{pages['batch']}/samples/{trace}",params=scoped).json()["data"]
    assert d["navigation"]["next"]["page"]==2
    assert d["navigation"]["context"]["filter_fingerprint"]==ctx["filter_fingerprint"]
    assert json.loads(d["sample_json"]["json_text"])["trace_id"]==trace


def test_source_filter_does_not_change_ingest_batch_summary(pages):
    batch=create(pages);one=upload(pages,dumps(OPENAI),"one.json");two=upload(pages,dumps(ANTHROPIC),"two.json")
    task=parse(pages,batch,[one,two]);scope={"batch_id":batch["batch_id"],"snapshot_id":task["result"]["snapshot_id"]}
    w=pages["client"].get(ROOT+"/workspace",params={**scope,"source_id":one}).json()["data"]
    assert w["summary"]["parsed_trace_count"]==2 and w["sample_page"]["total"]==1
    problems=pages["client"].get(ROOT+"/workspace",params={**scope,"issue_type":"time_missing"}).json()["data"]
    assert problems["sample_page"]["total"]==2
    for metric in w["validation"]["metrics"]:
        assert metric["passed"]+metric["missing"]+metric["failed"]==metric["eligible"]


def dumps(value):return json.dumps(value,ensure_ascii=False)


def test_atomic_bind_idempotency_and_permissions(pages):
    batch=create(pages);fid=upload(pages,dumps(OPENAI),key="same-upload")
    assert upload(pages,dumps(OPENAI),key="same-upload")==fid
    body={"expected_version":1,"files":[{"file_id":fid,"format_override":"auto"}]}
    url=ROOT+f"/batches/{batch['batch_id']}/files"
    first=post(pages,url,body,key="bind")
    assert first.status_code==200
    assert post(pages,url,body,key="bind").json()["data"]==first.json()["data"]
    assert post(pages,url,body).status_code==409
    b2=create(pages)
    assert post(pages,ROOT+f"/batches/{b2['batch_id']}/files",{"expected_version":1,"files":[{"file_id":fid},{"file_id":"missing"}]}).status_code==404
    with pages["sessions"]() as db:assert db.scalar(select(func.count()).select_from(TraceBinding).where(TraceBinding.batch_id==b2["batch_id"]))==0
    pages["client"].headers["Authorization"]="Bearer "+create_token(2,"user-2","管理员")
    assert post(pages,ROOT+f"/batches/{b2['batch_id']}/files",body).status_code==403


def test_partial_failed_parse_preserves_snapshot_and_no_duplicate_reparse(pages):
    batch=create(pages);fid=upload(pages,dumps(OPENAI)+"\n{broken")
    task=parse(pages,batch,[fid]);assert task["status"]=="succeeded" and task["result"]["outcome"]=="partial"
    old=task["result"]["snapshot_id"]
    w=pages["client"].get(ROOT+"/workspace",params={"batch_id":batch["batch_id"]}).json()["data"]
    bad=upload(pages,"{broken",name="bad.json")
    batch=w["batch"]
    task=parse(pages,batch,[bad],explicit=[bad]);assert task["status"]=="failed"
    after=pages["client"].get(ROOT+"/workspace",params={"batch_id":batch["batch_id"]}).json()["data"]
    assert after["batch"]["active_snapshot_id"]==old and after["summary"]["parsed_trace_count"]==1


def test_long_json_chunks_utf8_version_and_scope(pages):
    with pages["sessions"]() as db:
        s=db.scalar(select(TraceSample).where(TraceSample.trace_id==pages["trace"]))
        s.result={**s.result,"text":"中文😀"*90000}
        db.commit()
    params={"view":"statistics","projection_version":stats.VERSION,"snapshot_id":pages["scope"]["snapshot_id"]}
    url=f"{ROOT}/batches/{pages['batch']}/samples/{pages['trace']}"
    d=pages["client"].get(url,params=params).json()["data"]
    text=d["sample_json"]["json_text"];cursor=d["sample_json"]["next_cursor"]
    assert cursor and not d["sample_json"]["complete"]
    while cursor:
        part=pages["client"].get(url,params={**params,"representation":"sample","content_cursor":cursor}).json()["data"]["content_chunk"]
        assert part["offset_bytes"]==len(text.encode()) and part["content_version"]==d["sample_json"]["content_version"]
        text+=part["text"];cursor=part["next_cursor"]
    assert json.loads(text)["value_result"]["text"]=="中文😀"*90000
    assert hashlib.sha256(text.encode()).hexdigest()==d["sample_json"]["content_sha256"]


@pytest.mark.parametrize("view",["ingest","statistics","execution"])
@pytest.mark.parametrize("kind",["view","package"])
def test_three_page_exports(pages,view,kind):
    scope={**pages["scope"],"projection_version":stats.VERSION if view=="statistics" else pages["scope"]["projection_version"]}
    contents=[view+"_png"] if kind=="view" else ["normalized_json","offline_html","evidence_index","manifest"]
    command={"capability_code":"agent_trace_export","input":{**scope,"view":view,"export_kind":kind,"format":"png" if kind=="view" else "zip",
        "selected_trace_id":pages["trace"],"scope":{"type":"sample","trace_ids":[pages["trace"]]},"contents":contents}}
    r=post(pages,"/api/v1/tasks/execute",command)
    assert r.status_code==200,r.text
    task=pages["client"].get("/api/v1/tasks/"+r.json()["data"]["task_id"]).json()["data"]
    assert task["status"]=="succeeded",task
    aid=task["result"]["artifacts"][0]["artifact_id"]
    ticket=post(pages,ROOT+f"/artifacts/{aid}/download-tickets",{}).json()["data"]
    file=pages["client"].get(ticket["download_url"],headers={"Authorization":""})
    assert file.status_code==200
    if kind=="view":assert file.content.startswith(b'\x89PNG')
    else:
        with zipfile.ZipFile(io.BytesIO(file.content)) as z:
            assert "report.html" in z.namelist()
            assert pages["trace"] not in z.read("normalized.json").decode()


def test_statistics_full_sample_json_export(pages):
    r=post(pages,"/api/v1/tasks/execute",{"capability_code":"agent_trace_export","input":{**pages["scope"],"projection_version":stats.VERSION,
        "view":"statistics","export_kind":"sample_json","format":"json","scope":{"type":"sample","trace_ids":[pages["trace"]]},"contents":["normalized_json"]}})
    assert r.status_code==200,r.text
    task=pages["client"].get("/api/v1/tasks/"+r.json()["data"]["task_id"]).json()["data"]
    assert task["status"]=="succeeded"
    artifact=task["result"]["artifacts"][0]
    assert artifact["mime_type"]=="application/json"


def test_original_format_and_reader_source_protection(pages):
    record=deepcopy(OPENAI)
    record["id"]="13912345678"
    record["messages"][0]["content"]="联系电话 13912345678"
    args='{  "User_ID" : 42, "q" : "public"  }'
    record["messages"][1]["tool_calls"][0]["function"]["arguments"]=args
    original=json.dumps(record,ensure_ascii=False,indent=2)
    batch=create(pages);fid=upload(pages,original,"original.json");task=parse(pages,batch,[fid])
    scope={"snapshot_id":task["result"]["snapshot_id"]}
    w=pages["client"].get(ROOT+"/workspace",params={"batch_id":batch["batch_id"],**scope}).json()["data"]
    url=f"{ROOT}/batches/{batch['batch_id']}/samples/{w['resolved_selection']['trace_id']}"
    d=pages["client"].get(url,params=scope).json()["data"]
    assert d["raw_preview"]["raw_text"]==original
    assert d["normalized_event"]["input_json"]==args
    pages["client"].headers["Authorization"]="Bearer "+create_token(3,"user-3","审计员")
    response=pages["client"].get(url,params={**scope,"view":"statistics","projection_version":stats.VERSION})
    assert response.status_code==200
    assert "13912345678" not in response.text
    assert response.json()["data"]["raw_preview"]["availability"]=="unavailable"


def test_all_pending_files_not_only_visible_first_twenty(pages):
    batch=create(pages);bid=batch["batch_id"]
    ids=[upload(pages,dumps({"messages":[{"role":"user","content":str(i)}]}),f"record-{i}.json") for i in range(21)]
    first=post(pages,ROOT+f"/batches/{bid}/files",{"expected_version":1,"files":[{"file_id":i} for i in ids[:20]]}).json()["data"]["batch"]
    second=post(pages,ROOT+f"/batches/{bid}/files",{"expected_version":first["batch_version"],"files":[{"file_id":ids[-1]}]}).json()["data"]["batch"]
    response=post(pages,"/api/v1/tasks/execute",{"capability_code":"agent_trace_ingest","input":{"batch_id":bid,"expected_version":second["batch_version"],"mode":"parse_pending"}})
    task=pages["client"].get("/api/v1/tasks/"+response.json()["data"]["task_id"]).json()["data"]
    assert task["status"]=="succeeded" and task["result"]["file_count"]==21
    w=pages["client"].get(ROOT+"/workspace",params={"batch_id":bid}).json()["data"]
    assert len(w["files"]["items"])==20 and w["files"]["total"]==21
    assert w["summary"]["parsed_trace_count"]==21


def test_incremental_publish_keeps_old_snapshot(pages):
    batch=create(pages);one=upload(pages,dumps(OPENAI),"first.json")
    first=parse(pages,batch,[one]);old=first["result"]["snapshot_id"]
    before=pages["client"].get(ROOT+"/workspace",params={"batch_id":batch["batch_id"]}).json()["data"]
    two=upload(pages,dumps(ANTHROPIC),"second.json")
    second=parse(pages,before["batch"],[two]);assert second["status"]=="succeeded"
    after=pages["client"].get(ROOT+"/workspace",params={"batch_id":batch["batch_id"]}).json()["data"]
    assert after["summary"]["parsed_trace_count"]==2
    previous=pages["client"].get(ROOT+"/workspace",params={"batch_id":batch["batch_id"],"snapshot_id":old}).json()["data"]
    assert previous["summary"]["parsed_trace_count"]==1


def test_statistics_projection_only_published_after_full_snapshot(pages):
    from app.services.agent_trace_sync import ingest as import_value
    with pages["sessions"]() as db:
        bid=import_value(db,{"system":"business","task_id":"large-value","result_id":"large-result"},
            [{"id":str(i),"execution":{"events":[],"calls":[],"duration_ms":0}} for i in range(601)])
        db.commit()
        from app.models.agent_traces import TraceBatch
        sid=db.get(TraceBatch,bid).active_snapshot_id
        assert stats.materialize(db)==500;db.commit()
        assert db.scalar(select(TraceProjection).where(TraceProjection.snapshot_id==sid,TraceProjection.version==stats.VERSION)) is None
        assert stats.materialize(db)==101;db.commit()
        assert db.scalar(select(TraceProjection).where(TraceProjection.snapshot_id==sid,TraceProjection.version==stats.VERSION)) is not None


def test_selection_long_content_cursor(pages):
    with pages["sessions"]() as db:
        event=db.scalar(select(TraceEvent).where(TraceEvent.trace_id==pages["trace"]).order_by(TraceEvent.ordinal))
        event.payload={**event.payload,"source_excerpt":"中文😀"*20000};eid=event.event_id;db.commit()
    url=f"{ROOT}/batches/{pages['batch']}/samples/{pages['trace']}"
    params={**pages["scope"],"view":"execution","section":"selection","event_id":eid}
    d=pages["client"].get(url,params=params).json()["data"]
    first=d["selection"]["content"]["source_excerpt"]
    assert first["truncated"] and first["next_cursor"]
    part=pages["client"].get(url,params={**params,"selection_cursor":first["next_cursor"]}).json()["data"]
    assert part["selection_segment"]["offset"]==first["loaded_units"]
