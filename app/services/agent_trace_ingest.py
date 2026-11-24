"""File assets, atomic batch binding, real adapters and immutable snapshot publication."""
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import logging
import math
from pathlib import Path
from uuid import uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from app.core.database import SessionLocal
from app.core.time import now_shanghai
from app.models.tables import Task
from app.models.agent_traces import (TraceAsset, TraceBinding, TraceBatch, TraceSnapshot, TraceProjection,
    TraceSample, TraceEvent, TraceCall, TraceRelation, TraceEvidence, TraceIdempotency, TraceStatisticsSample, TraceArtifact)
from app.services.agent_trace_generation import VERSION, stable_id, generated_sample, validate_process, dumps
from app.services import agent_trace_query as q
from app.services import agent_trace_anthropic as anthropic

from app.services.agent_trace_trajectory import EXAMPLE as TRAJECTORY_EXAMPLE, normalize_trajectory

MAX_BYTES = 10 * 1024 * 1024
FORMATS = [
    {"code":"openai_messages","label":"OpenAI Messages","containers":["json","jsonl"],"description":"完整 messages 轨迹，支持工具调用和返回", "availability":"available","schema_version":"1",
     "example_text":dumps({"messages":[{"role":"user","content":"Summarize a public report"},{"role":"assistant","tool_calls":[{"id":"call_1","function":{"name":"search","arguments":'{"q":"public report"}'}}]},{"role":"tool","tool_call_id":"call_1","content":"Public result"}]})},
    {"code":anthropic.CODE,"label":anthropic.LABEL,"containers":["json","jsonl"],
     "description":"Claude Messages 轨迹，支持工具调用、工具返回和 Token", "availability":"available","schema_version":"1",
     "example_text":dumps(anthropic.EXAMPLE)},
    {"code":"langchain_trace","label":"LangChain / LangSmith Trace","containers":["json","jsonl"],"description":"平台 runs 包装，按根运行分组", "availability":"available","schema_version":"1",
     "example_text":dumps({"runs":[{"id":"root","parent_run_id":None,"run_type":"chain","inputs":{"q":"report"},"outputs":{"answer":"summary"}},{"id":"tool","parent_run_id":"root","run_type":"tool","name":"search","inputs":{"q":"report"},"outputs":{"text":"result"}}]})},
    {"code":"agent_trajectory","label":"智能体轨迹 JSONL","containers":["json","jsonl"],"description":"外部智能体 events / tool_events 轨迹（含推理、工具及 Token）", "availability":"available","schema_version":"1",
     "example_text":dumps(TRAJECTORY_EXAMPLE)},
]


def mutation(db, user, path, key, payload, action, *, replay_valid=None):
    if not q.permissions(user)["can_export"]:q.fail(403,"当前账号为只读，不能修改轨迹数据")
    from app.services.agent_trace_export import idem_key
    identity = idem_key(user["uid"], path, key)
    digest = hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()
    old = db.get(TraceIdempotency, identity)
    if old and replay_valid is not None:
        # Serialize replacement of a stale upload receipt, including on SQLite.
        db.execute(update(TraceIdempotency).where(TraceIdempotency.key == identity)
                   .values(expires_at=TraceIdempotency.expires_at).execution_options(synchronize_session=False))
        old = db.get(TraceIdempotency, identity, with_for_update=True, populate_existing=True)
    if old and old.expires_at > now_shanghai():
        if old.payload_hash != digest:
            q.fail(409,"相同请求标识不能用于不同内容")
        if replay_valid is None or replay_valid(old.result):
            return deepcopy(old.result),False
    if old:
        db.delete(old)
        db.flush()
    try:
        record=TraceIdempotency(key=identity,payload_hash=digest,result={},expires_at=now_shanghai()+timedelta(hours=24))
        db.add(record)
        db.flush()
        result=action()
        record.result=result
        db.commit()
        return result,True
    except IntegrityError:
        db.rollback()
        old=db.get(TraceIdempotency,identity)
        if old and old.payload_hash==digest and (replay_valid is None or replay_valid(old.result)):
            return deepcopy(old.result),False
        q.fail(409,"并发请求发生冲突，请刷新后重试")
    except Exception:
        db.rollback()
        raise


def register_asset(db, user, file_id, filename, path, size, digest, request_id):
    if not q.permissions(user)["can_export"]:q.fail(403,"当前账号为只读，不能上传文件")
    def action():
        db.add(TraceAsset(file_id=file_id,owner_id=str(user["uid"]),filename=filename,storage_path=str(path.resolve()),size_bytes=size,sha256=digest))
        db.flush()
        return {"file_id":file_id,"filename":filename,"size":size,"extension":Path(filename).suffix.lower()}
    if not request_id:  # preserve old upload clients; new trace clients always send a request ID.
        result=action(); db.commit(); return result
    def asset_exists(receipt):
        asset_id = receipt.get("file_id")
        asset = db.get(TraceAsset, asset_id, with_for_update=True, populate_existing=True) if asset_id else None
        return asset is not None and asset.owner_id == str(user["uid"])

    result, _ = mutation(db, user, "/files", request_id, {"filename": filename, "size": size, "sha256": digest},
                         action, replay_valid=asset_exists)
    if result["file_id"]!=file_id:
        path.unlink(missing_ok=True)
    return result


def batch_dict(batch, user):
    p=batch.payload
    actions=["view_raw"] if q.permissions(user)["can_view_raw"] else []
    if q.permissions(user)["can_export"]:
        actions += ["export_view","export_package"]
        if batch.source_system=="upload": actions += ["upload","bind_files","parse","delete_batch"]
    return {"batch_id":batch.batch_id,"display_id":batch.display_id,"name":p.get("name","轨迹批次"),
        "batch_version":p.get("batch_version",1),"active_snapshot_id":batch.active_snapshot_id or None,
        "allowed_actions":actions,"staging_changed":p.get("staging_changed",False)}


def create_batch(db,user,name,key):
    if not isinstance(name,str) or not 1<=len(name.strip())<=100: q.fail(422,"批次名称长度应为1至100字符")
    name=name.strip()
    def action():
        bid="batch_"+uuid4().hex
        b=TraceBatch(batch_id=bid,display_id="AUD-"+bid[-10:].upper(),source_system="upload",source_task_id=bid,
            active_snapshot_id="",payload={"name":name,"batch_version":1,"staging_changed":False})
        db.add(b);db.flush();return batch_dict(b,user)
    return mutation(db,user,"/compliance/agent-traces/batches",key,{"name":name},action)[0]


def lock_batch(db, bid):
    # A no-op write also serializes batch mutations on SQLite, where FOR UPDATE is ignored.
    db.execute(update(TraceBatch).where(TraceBatch.batch_id==bid).values(payload=TraceBatch.payload)
               .execution_options(synchronize_session=False))
    return db.scalar(select(TraceBatch).where(TraceBatch.batch_id==bid).with_for_update()
                     .execution_options(populate_existing=True))


def check_edit(db,bid):
    batch = lock_batch(db, bid)
    if not batch:q.fail(404,"批次不存在")
    q.llm_access(db,batch)
    if batch.source_system!="upload":q.fail(409,"价值分析批次由分析任务管理，请创建独立上传批次")
    running=db.scalar(select(Task.task_id).where(Task.capability_code=="agent_trace_ingest",Task.status.in_(["pending","running"]),Task.input_data["batch_id"].as_string()==bid).with_for_update())
    if running:q.fail(409,"当前批次正在解析，任务："+running)
    return batch


def bindings(db,bid,lock=False):
    query = select(TraceBinding).where(TraceBinding.batch_id==bid).order_by(TraceBinding.file_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return list(db.scalars(query))


def lock_asset(db, file_id):
    db.execute(update(TraceAsset).where(TraceAsset.file_id == file_id).values(owner_id=TraceAsset.owner_id)
               .execution_options(synchronize_session=False))
    return db.scalar(select(TraceAsset).where(TraceAsset.file_id == file_id).with_for_update()
                     .execution_options(populate_existing=True))


def contains_reference(value, references):
    if isinstance(value, str):
        return value in references
    if isinstance(value, dict):
        return any(contains_reference(v, references) for v in value.values())
    if isinstance(value, list):
        return any(contains_reference(v, references) for v in value)
    return False


def cleanup_asset(db, user, file_id):
    """Retryable cleanup after unbinding; serialize against new batch bindings."""
    try:
        # A replay may have opened a repeatable-read snapshot before another binding committed.
        db.commit()
        asset = lock_asset(db, file_id)
        if asset is None:
            db.commit()
            return "deleted"
        if asset.owner_id != str(user["uid"]):
            db.rollback()
            return "retained"
        references = {file_id, asset.storage_path}
        referenced = db.scalar(select(TraceBinding.id).where(TraceBinding.file_id == file_id).limit(1)) is not None
        if not referenced:
            referenced = any(contains_reference(row, references) for row in db.scalars(select(TraceSample.payload)))
        if not referenced:
            referenced = any(contains_reference(row, references) for row in db.scalars(select(TraceSample.result)))
        if not referenced:
            referenced = any(contains_reference(row, references) for row in db.scalars(select(TraceSnapshot.payload)))
        if not referenced:
            referenced = any(contains_reference([task.input_data, task.config, task.result], references)
                             for task in db.scalars(select(Task)))
        if referenced:
            db.commit()
            return "retained"
        from app.routers.files import UPLOAD_DIR
        root = UPLOAD_DIR.resolve()
        path = Path(asset.storage_path).resolve()
        if not path.is_relative_to(root) or path == root:
            db.rollback()
            logging.getLogger(__name__).warning("Refusing asset cleanup outside upload directory: %s", file_id)
            return "pending"
        path.unlink(missing_ok=True)
        db.delete(asset)
        db.commit()
        return "deleted"
    except Exception:
        db.rollback()
        logging.getLogger(__name__).exception("Asset cleanup pending for %s", file_id)
        return "pending"


def remove_batch(db, batch, user, body):
    bid = batch.batch_id
    files = bindings(db, bid, lock=True)
    snapshots = select(TraceSnapshot.snapshot_id).where(TraceSnapshot.batch_id == bid)
    if "expected_snapshot_id" not in body:
        if files or db.scalar(snapshots.limit(1)) or db.scalar(select(TraceArtifact.artifact_id)
                                                               .where(TraceArtifact.batch_id == bid).limit(1)):
            q.fail(409, "确认删除后才能移除批次内的文件和审计记录")
    elif body["expected_snapshot_id"] != (batch.active_snapshot_id or None):
        q.fail(409, "批次审计结果已更新，请刷新后重新确认删除")
    file_ids = [row.file_id for row in files]
    if file_ids and db.scalar(select(TraceAsset.file_id).where(
        TraceAsset.file_id.in_(file_ids), TraceAsset.owner_id != str(user["uid"])
    ).limit(1)):
        q.fail(403, "批次含有其他账号上传的文件")

    tasks = list(db.scalars(select(Task).where(
        Task.capability_code.in_(["agent_trace_ingest", "agent_trace_export"]),
        Task.input_data["batch_id"].as_string() == bid,
    ).with_for_update()))
    if any(task.status in {"pending", "running"} for task in tasks):
        q.fail(409, "批次仍有解析或导出任务，请完成后再删除")

    snapshot_ids = list(db.scalars(snapshots.with_for_update()))
    artifacts = list(db.scalars(select(TraceArtifact).where(TraceArtifact.batch_id == bid).with_for_update()))
    for model in (TraceSample, TraceEvent, TraceCall, TraceRelation, TraceEvidence,
                  TraceStatisticsSample, TraceProjection):
        db.execute(delete(model).where(model.snapshot_id.in_(snapshot_ids))
                   .execution_options(synchronize_session=False))
    db.execute(delete(TraceSnapshot).where(TraceSnapshot.batch_id == bid))
    for row in [*artifacts, *tasks, *files]:
        db.delete(row)
    db.delete(batch)
    return file_ids, [artifact.storage_name for artifact in artifacts]


def delete_item(db, user, bid, body, key):
    expected = body.get("expected_version")
    file_id = body.get("file_id")
    if type(expected) is not int or expected < 1 or "file_id" not in body:
        q.fail(422, "expected_version须为正整数，file_id必须显式提供")
    if file_id is not None and (not isinstance(file_id, str) or not file_id.strip()):
        q.fail(422, "file_id须为非空字符串或null")
    snapshot = body.get("expected_snapshot_id")
    if snapshot is not None and (not isinstance(snapshot, str) or not snapshot.strip()):
        q.fail(422, "expected_snapshot_id须为非空字符串或null")
    if not q.permissions(user)["can_export"]:
        q.fail(403, "当前账号为只读，不能修改轨迹数据")
    # Replays still check current access, but may refer to an already deleted resource.
    batch = db.get(TraceBatch, bid)
    if batch:
        q.llm_access(db, batch)
        if batch.source_system != "upload":
            q.fail(409, "仅独立上传批次支持删除")
    asset = db.get(TraceAsset, file_id) if file_id is not None else None
    if asset and asset.owner_id != str(user["uid"]):
        q.fail(403, "没有此上传文件的访问权限")

    def action():
        batch = check_edit(db, bid)
        if batch.payload.get("batch_version", 1) != expected:
            q.fail(409, "批次版本冲突，当前版本：" + str(batch.payload.get("batch_version", 1)))
        if file_id is not None:
            asset = lock_asset(db, file_id)
            if asset is None:
                q.fail(404, "上传文件不存在")
            if asset.owner_id != str(user["uid"]):
                q.fail(403, "没有此上传文件的访问权限")
            binding = db.scalar(select(TraceBinding).where(TraceBinding.batch_id == bid, TraceBinding.file_id == file_id)
                                .with_for_update().execution_options(populate_existing=True))
            if binding is None:
                q.fail(404, "文件不属于当前批次")
            p = binding.payload
            if p.get("parse_status") not in {"pending", "failed"} or p.get("parsed_trace_count") != 0:
                q.fail(409, "仅允许删除待解析或解析失败且成功解析数为零的文件")
            db.delete(binding)
            batch.payload = {**batch.payload, "batch_version": expected + 1, "staging_changed": True}
            result_batch = batch_dict(batch, user)
        else:
            file_ids, artifact_names = remove_batch(db, batch, user, body)
            result_batch = None
        db.flush()
        return {"deleted": True, "batch_id": bid, "file_id": file_id, "batch": result_batch,
                "asset_cleanup": "pending" if file_id is not None else "not_applicable",
                **({"cleanup_file_ids": file_ids, "cleanup_artifact_names": artifact_names} if file_id is None else {})}

    path = f"/batches/{bid}/deletions"
    result, _ = mutation(db, user, path, key, body, action)
    result = deepcopy(result)
    if file_id is not None:
        result["asset_cleanup"] = cleanup_asset(db, user, file_id)
        from app.services.agent_trace_export import idem_key
        record = db.get(TraceIdempotency, idem_key(user["uid"], path, key))
        record.result = result
        db.commit()
    else:
        file_ids = result.pop("cleanup_file_ids", [])
        names = result.pop("cleanup_artifact_names", [])
        states = {cleanup_asset(db, user, bound_id) for bound_id in file_ids}
        if states:
            result["asset_cleanup"] = next(state for state in ("pending", "retained", "deleted") if state in states)
        if names:
            from app.services.agent_trace_export import directory
            try:
                root = directory()
                for name in names:
                    if Path(name).name != name or name in {"", ".", ".."}:
                        continue
                    target = (root / name).resolve()
                    if target.parent == root:
                        target.unlink(missing_ok=True)
            except OSError:
                logging.getLogger(__name__).exception("Trace artifact cleanup pending for batch %s", bid)
    return result


def bind(db,user,bid,body,key):
    expected=body.get("expected_version"); files=body.get("files")
    if type(expected) is not int or not isinstance(files,list) or not 1<=len(files)<=20:q.fail(422,"版本及文件列表无效，一次最多20个文件")
    if any(not isinstance(f,dict) or not isinstance(f.get("file_id"),str) for f in files):q.fail(422,"文件标识无效")
    if len({f["file_id"] for f in files})!=len(files):q.fail(422,"不能重复绑定同一文件")
    assets=[]
    for f in files:
        asset=db.get(TraceAsset,f["file_id"])
        if not asset:q.fail(404,"上传文件不存在")
        if asset.owner_id!=str(user["uid"]):q.fail(403,"没有此上传文件的访问权限")
        if asset.size_bytes>MAX_BYTES:q.fail(413,"轨迹文件不能超过10MiB")
        if Path(asset.filename).suffix.lower() not in {".json",".jsonl"}:q.fail(415,"轨迹接入只支持JSON/JSONL")
        if f.get("format_override","auto") not in {"auto",*[v["code"] for v in FORMATS]}:q.fail(422,"格式覆盖无效")
        assets.append(asset)
    # Replayed requests must still authorize both the batch and assets.
    b=db.get(TraceBatch,bid)
    if not b:q.fail(404,"批次不存在")
    q.llm_access(db,b)
    def action():
        b=check_edit(db,bid)
        if b.payload.get("batch_version",1)!=expected:q.fail(409,"批次版本冲突，当前版本："+str(b.payload.get("batch_version",1)))
        for file_id in sorted(a.file_id for a in assets):
            locked = lock_asset(db, file_id)
            if locked is None:q.fail(404,"上传文件不存在")
            if locked.owner_id != str(user["uid"]):q.fail(403,"没有此上传文件的访问权限")
        changed=False
        for f,asset in zip(files,assets):
            identity=stable_id("bind_",bid,asset.file_id)
            row=db.scalar(select(TraceBinding).where(TraceBinding.id==identity).with_for_update()
                          .execution_options(populate_existing=True))
            override=f.get("format_override","auto")
            if row is None or row.payload.get("format_override")!=override:
                changed=True
                value={"format_override":override,"detected_format":None,"recognition_state":"pending","parse_status":"pending",
                    "parsed_trace_count":0,"failed_record_count":0,"input_candidate_count":None,"error_examples":[]}
                if row:row.payload=value
                else:db.add(TraceBinding(id=identity,batch_id=bid,file_id=asset.file_id,payload=value))
        if changed:b.payload={**b.payload,"batch_version":expected+1,"staging_changed":True}
        db.flush()
        return {"batch":batch_dict(b,user),"affected_files":[a.file_id for a in assets],"staging_changed":b.payload.get("staging_changed",False)}
    return mutation(db,user,f"/batches/{bid}/files",key,body,action)[0]


def records(text,extension):
    if extension==".jsonl":
        for line_no,line in enumerate(text.splitlines(),1):
            if line.strip():
                try:yield json.loads(line),line,line_no,None
                except ValueError:yield None,line,line_no,"invalid_json"
    else:
        parsed=json.loads(text)
        if isinstance(parsed,list):
            decoder=json.JSONDecoder(); pos=text.index('[')+1
            for entry in parsed:
                while pos<len(text) and text[pos] in ' \r\n\t,':pos+=1
                _,end=decoder.raw_decode(text,pos)
                yield entry,text[pos:end],text[:pos].count('\n')+1,None
                pos=end
        else:yield parsed,text,1,None


def normalize(record,file_id,index,raw,line,override):
    if not isinstance(record,dict):raise ValueError("轨迹必须为对象")
    if isinstance(record.get("events"),list) and record.get("sample_id"):
        if override not in {"auto","agent_trajectory"}:raise ValueError("格式覆盖与核心结构不一致")
        return normalize_trajectory(record,file_id,index,raw,line,normalize)
    if override=="agent_trajectory":raise ValueError("智能体轨迹缺少 sample_id 或 events")
    source_record = record
    messages=record.get("messages")
    detected="langchain_trace" if isinstance(record.get("runs"),list) else None
    if isinstance(messages,list):
        detected = (anthropic.CODE if override == anthropic.CODE or anthropic.is_anthropic(record)
                    else "openai_messages")
    fmt=detected if override=="auto" else override
    if not fmt or (fmt=="langchain_trace" and not isinstance(record.get("runs"),list)) or (fmt!="langchain_trace" and not isinstance(messages,list)):
        raise ValueError("无法识别或格式覆盖与记录结构不匹配")
    if override!="auto" and detected!=fmt:raise ValueError("格式覆盖与核心结构不一致")
    if fmt == anthropic.CODE:
        record, source_paths = anthropic.message_view(record)
        messages = record["messages"]
    trace=stable_id("trc_",file_id,index)
    competition = None
    if fmt == "openai_messages" and "trajectory_id" in record:
        identity = record["trajectory_id"]
        if not isinstance(identity, str) or not identity or len(identity) > 255 or identity == "aggregate":
            raise ValueError("trajectory_id 必须为1至255字符，且不能为 aggregate")
        from app.services.agent_trace_competition import analyze
        competition = analyze(record)
    events=[];calls=[];pending={}; issues=[]
    def event(kind,role,text,path):
        n=len(events)+1;eid=stable_id("ev_",trace,n)
        e={"event_id":eid,"display_id":f"STEP-{n:02}","ordinal":n,"event_type":kind,"label":{"tool_call":"工具调用","tool_result":"工具返回","message":"消息","run":"运行记录"}.get(kind,kind),
            "role":role,"description":str(text),"source_excerpt":str(text),"reasoning_summary":"源日志未记录推理摘要", "risk_level":"unknown","relative_time_ms":None,"call_id":None,
            "source_ref":{"source_id":file_id,"record_index":index+1,"line_start":line,"source_path":path},"data_origin":"actual"}
        events.append(e);return e
    def call(source_id,name,args,path):
        if not source_id or source_id in pending:issues.append("call_unlinked")
        e=event("tool_call","assistant",args,path)
        cid=stable_id("call_",trace,len(calls));e["call_id"]=cid
        c={"call_id":cid,"display_id":f"CALL-{len(calls)+1:02}","order":len(calls)+1,"attempt_id":stable_id("attempt_",cid),"attempt_index":0,"source_call_id":source_id,"parent_call_id":None,
           "tool_id":name or "unknown","tool_name":name or "unknown","event_id":e["event_id"],"step_display_id":e["display_id"],"request_event_id":e["event_id"],"return_event_id":None,
           "to_participant_id":name or "unknown","from_participant_id":"agent","duration_ms":None,"relative_start_ms":None,"relative_end_ms":None,
           "execution_status":"unknown","permission_decision":"unknown","granted_scopes":[],"requested_scopes":[],"result_code":"NOT_RECORDED",
           "input_json":args if isinstance(args,str) else dumps(args),"output_json":"null","input_summary":str(args)[:512],"output_summary":"尚无关联返回","behavior_tags":[],
           "evidence_chain":[{"event_id":e["event_id"],"display_id":e["display_id"]}],"analysis":{"permission_comparison":{"rows":[],"actual_decision":{"message":"源日志没有记录权限判定"}},"error_attribution":None},"data_origin":"actual"}
        calls.append(c);pending[source_id]=c
    def result(source_id,output,path,error=False,metadata=None):
        e=event("tool_result","tool",output,path)
        c=pending.pop(source_id,None)
        if not c:issues.append("call_unlinked");return
        from app.services.agent_trace_call_status import outcome, command_from, task_instructions
        metadata = {**(metadata or {}), **({"is_error": True} if error else {})}
        parsed_status = outcome(output, metadata=metadata, command=command_from(c["input_json"]),
                                instructions=task_instructions(record), tool_name=c["tool_name"])
        c.update(return_event_id=e["event_id"],output_json=output if isinstance(output,str) else dumps(output),output_summary=str(output)[:512],
            outcome_metadata=metadata, **parsed_status)
        e["call_id"]=c["call_id"];c["evidence_chain"].append({"event_id":e["event_id"],"display_id":e["display_id"]})
    if fmt=="langchain_trace":
        runs=record["runs"]
        for j,run in enumerate(runs):
            if not isinstance(run,dict) or not run.get("id"):raise ValueError("运行节点缺少id")
            if run.get("run_type")=="tool":
                call(run["id"],run.get("name"),dumps(run.get("inputs")),f"runs[{j}].inputs")
                if "outputs" in run or run.get("error"):result(run["id"],dumps(run.get("outputs") or run.get("error")),f"runs[{j}].outputs",bool(run.get("error")))
            else:event("run",run.get("run_type","chain"),dumps({"inputs":run.get("inputs"),"outputs":run.get("outputs")}),f"runs[{j}]")
    else:
        for j,m in enumerate(messages):
            if not isinstance(m,dict) or not isinstance(m.get("role"),str):raise ValueError("消息角色缺失")
            content=m.get("content")
            before = len(events)
            if m["role"]=="tool":result(m.get("tool_call_id"),content if isinstance(content,str) else dumps(content),f"messages[{j}]",
                metadata={k:m[k] for k in ("execution_status", "status", "is_error", "isError", "success", "error_code",
                    "exit_code", "exitCode", "returncode", "return_code", "http_status", "status_code", "permission_decision") if k in m})
            else:
                if m["role"] == "assistant":
                    if pending: issues.append("call_unlinked")
                    pending.clear()
                if content is not None:event("message",m["role"],content if isinstance(content,str) else dumps(content),f"messages[{j}].content")
                elif m["role"] == "assistant" and (m.get("reasoning_content") or m.get("reasoning")) and not m.get("tool_calls"):
                    field = "reasoning_content" if m.get("reasoning_content") else "reasoning"
                    event("message", "assistant", "", f"messages[{j}].{field}")
                call_ids = [c.get("id") for c in (m.get("tool_calls") or [])]
                duplicate_ids = {v for v in call_ids if call_ids.count(v) > 1}
                for k,c in enumerate(m.get("tool_calls",[])):
                    fn=c.get("function",{});args=fn.get("arguments","{}")
                    try:json.loads(args) if isinstance(args,str) else None
                    except ValueError:issues.append("structure_invalid")
                    call(c.get("id"),fn.get("name"),args,f"messages[{j}].tool_calls[{k}]")
                for duplicate_id in duplicate_ids:
                    pending.pop(duplicate_id, None)
                    issues.append("call_unlinked")
            for emitted in events[before:]:
                reasoning = m.get("reasoning_content") or m.get("reasoning")
                if isinstance(reasoning, str) and reasoning:
                    emitted.update(reasoning_original=reasoning, reasoning_summary=reasoning, reasoning_provenance="source_log")
                timestamp = m.get("ts_ms")
                if ((type(timestamp) is int and timestamp >= 0) or
                        (type(timestamp) is float and math.isfinite(timestamp) and timestamp >= 0)):
                    emitted["relative_time_ms"] = timestamp
    if pending:issues.append("call_unlinked")
    metadata=record.get("metadata") if isinstance(record.get("metadata"),dict) else {}
    duration=record.get("duration_ms",metadata.get("duration_ms"))
    if duration is None and record.get("start_time") and record.get("end_time"):
        try:duration=(datetime.fromisoformat(record["end_time"].replace('Z','+00:00'))-datetime.fromisoformat(record["start_time"].replace('Z','+00:00'))).total_seconds()*1000
        except (ValueError,TypeError):pass
    if not isinstance(duration,(int,float)) or isinstance(duration,bool) or duration<0:duration=None
    usage=record.get("usage",metadata.get("usage",{})) or {}
    if not isinstance(usage,dict):usage={}
    token={"input_tokens":usage.get("input_tokens",usage.get("prompt_tokens")),"output_tokens":usage.get("output_tokens",usage.get("completion_tokens")),
           "reasoning_tokens":usage.get("reasoning_tokens"),"output_includes_reasoning":bool(usage.get("completion_tokens_details",{}).get("reasoning_tokens"))}
    if token["reasoning_tokens"] is None:token["reasoning_tokens"]=usage.get("completion_tokens_details",{}).get("reasoning_tokens")
    if duration is None:issues.append("time_missing")
    if token["input_tokens"] is None or token["output_tokens"] is None:issues.append("usage_missing")
    execution={"events":events,"calls":calls,"duration_ms":duration,"token_usage":token,"task_status":record.get("task_status","unknown"),
        "source":{"source_id":file_id,"format_code":fmt,"format_label":next(f["label"] for f in FORMATS if f["code"]==fmt)},
        "raw_text":raw,"issue_types":sorted(set(issues)),"source_ref":{"source_id":file_id,"record_index":index+1,"line_start":line,"source_path":"$"},
        "test_category":record.get("test_category") or metadata.get("test_category"),"inefficiency_groups":[],"data_origin":"actual"}
    if competition is not None:
        execution.update(original_sample_id=record["trajectory_id"], competition_report=competition,
                         origin_type="external_import")
        state = metadata.get("record") or {}
        execution["session_completed"] = state.get("is_session_completed")
        execution["task_status"] = "completed" if state.get("is_session_completed") is True else "unknown"
        # Keep the original message and scoring-step coordinates alongside UI event IDs.
        from app.services.agent_trace_competition import parse
        scoring_calls, _, _, _, _, locations = parse(record)
        for e in events:
            path = e["source_ref"]["source_path"]
            message_index = int(path.split("[")[1].split("]")[0])
            e["source_ref"]["message_index"] = message_index
            ids = locations.get(message_index, [])
            if ".tool_calls[" in path:
                call_index = int(path.split(".tool_calls[")[1].split("]")[0])
                ids = [c["step_id"] for c in scoring_calls if c["message_index"] == message_index and c["call_index"] == call_index]
            e["source_step_ids"] = ids
    if fmt == anthropic.CODE:
        identity = source_record.get("trajectory_id", source_record.get("sample_id", source_record.get("id")))
        if identity is not None and (not isinstance(identity, str) or not identity or len(identity) > 255):
            raise ValueError("Anthropic 轨迹标识必须为1至255字符")
        execution.update(original_sample_id=identity, origin_type="external_import",
                         provider="anthropic", model=source_record.get("model"))
        for e in events:
            ref = e["source_ref"]
            ref["source_path"] = source_paths[ref["source_path"]]
    language=record.get("language",metadata.get("language","unknown"))
    language=language if language in {"zh","en","other","unknown"} else "other"
    from app.services.agent_trace_audit import recorded_audit
    recorded_audit(source_record, execution, events, calls)
    return {"id":f"{file_id}:{index}","text":raw,"language":language,"execution":execution,"source_record":source_record}


def split_roots(record):
    if not isinstance(record,dict) or not isinstance(record.get("runs"),list):return [record]
    runs=record["runs"];by_id={r.get("id"):r for r in runs if isinstance(r,dict)}
    if len(by_id)!=len(runs):raise ValueError("运行id缺失或重复")
    groups={}
    for run in runs:
        node=run;seen=set()
        while node.get("parent_run_id") is not None:
            if node.get("id") in seen or node["parent_run_id"] not in by_id:raise ValueError("运行树循环或父节点不存在")
            seen.add(node["id"]);node=by_id[node["parent_run_id"]]
        groups.setdefault(node["id"],[]).append(run)
    return [{**record,"runs":v} for v in groups.values()]


def submit(db,body,user,key,name):
    from app.services.agent_trace_model_audit import STRATEGIES, configuration

    bid=body.get("batch_id")
    if not isinstance(bid,str):q.fail(422,"batch_id必填")
    batch=db.get(TraceBatch,bid)
    if not batch:q.fail(404,"批次不存在")
    q.llm_access(db,batch)
    mode=body.get("mode","parse_pending")
    if mode not in {"parse_pending","retry_failed","reparse_all"}:q.fail(422,"解析mode无效")
    strategy = body.get("audit_strategy", "default")
    if not isinstance(strategy, str) or strategy not in STRATEGIES:
        q.fail(422, "轨迹审计策略无效")
    def action():
        b=check_edit(db,bid)
        if body.get("expected_version")!=b.payload.get("batch_version",1):q.fail(409,"批次版本冲突")
        all_files=bindings(db,bid,lock=True)
        explicit=body.get("file_ids")
        if explicit is not None and (not isinstance(explicit,list) or not explicit or len(explicit)>100 or not all(isinstance(i,str) for i in explicit)):q.fail(422,"file_ids无效")
        if explicit is not None and not set(explicit)<={f.file_id for f in all_files}:q.fail(409,"文件不属于批次")
        eligible={"failed","partial"} if mode=="retry_failed" else {"pending","failed","partial"}
        selected=[f for f in all_files if (explicit is None or f.file_id in explicit) and (mode=="reparse_all" or f.payload["parse_status"] in eligible)]
        if not selected:q.fail(422,"没有符合条件的待解析文件")
        tid="tr_ingest_"+uuid4().hex
        provider = STRATEGIES[strategy]
        model_settings = {}
        if provider is not None:
            model = configuration(provider)
            model_settings = {"audit_provider": model.provider, "audit_model": model.model}
        frozen={**body,"file_ids":[f.file_id for f in selected],"mode":mode,"audit_strategy":strategy}
        task=Task(task_id=tid,name=name or "智能体轨迹解析",capability_code="agent_trace_ingest",status="pending",progress=0,input_data=frozen,
            config={"owner_id":str(user["uid"]),"batch_id":bid,"bindings":[{"file_id":f.file_id,"format_override":f.payload["format_override"]} for f in selected],
                    **model_settings},result={"kind":"agent_trace_ingest","batch_id":bid})
        db.add(task)
        for f in selected:f.payload={**f.payload,"parse_status":"running","last_task_id":tid}
        db.flush()
        return {"task_id":tid,"status":"pending","progress":0,"result":task.result}
    return mutation(db,user,"/tasks/execute",key,{"capability_code":"agent_trace_ingest","input":body,"name":name},action)


def prepare_sample(sample, order):
    from app.services.agent_trace_audit import recorded_audit
    from app.services.agent_trace_rules import audit_process

    trace, payload, events, calls, relations = generated_sample("upload", sample, order)
    for event in events:
        event["risk_level"] = "unknown"
    record = sample.get("source_record", {})
    evidence = audit_process(payload, events, calls, recorded_audit(record, payload, events, calls), record=record)
    validate_process(payload, events, calls)
    return trace, payload, events, calls, relations, evidence


def publish(db,batch,tid,parsed,selected_ids, *, prepared=None, staging_task_id=None):
    from app.services.agent_trace_statistics import statistics_sample
    from app.services.agent_trace_rules import VERSION as RULE_VERSION, audit_process
    from app.services.agent_trace_audit import recorded_audit
    sid=stable_id("snap_",batch.batch_id,tid)
    old=batch.active_snapshot_id
    carried=list(db.scalars(select(TraceSample).where(TraceSample.snapshot_id==old))) if old else []
    carried=[s for s in carried if s.payload["source"]["source_id"] not in selected_ids]
    snapshot = TraceSnapshot(snapshot_id=sid, batch_id=batch.batch_id, payload={
        "schema_version": "1", "batch_version_at_start": batch.payload["batch_version"],
        "sample_count": len(carried) + len(parsed), "data_origin": "actual", "rule_version": RULE_VERSION,
        **({"staging_task_id": staging_task_id} if staging_task_id else {}),
    })
    db.add(snapshot)
    for oldrow in carried:
        db.add(TraceSample(id=stable_id("sample_",sid,oldrow.trace_id),snapshot_id=sid,trace_id=oldrow.trace_id,sample_id=oldrow.sample_id,display_id=oldrow.display_id,
            ingest_order=oldrow.ingest_order,language=oldrow.language,result=oldrow.result,payload=oldrow.payload))
        for model in (TraceEvent,TraceCall,TraceRelation,TraceEvidence):
            for row in db.scalars(select(model).where(model.snapshot_id==old,model.trace_id==oldrow.trace_id)):
                args={"id":stable_id("row_",sid,row.id),"snapshot_id":sid,"trace_id":row.trace_id,"payload":row.payload}
                if model==TraceEvent:args.update(event_id=row.event_id,ordinal=row.ordinal)
                if model==TraceCall:args.update(call_id=row.call_id,ordinal=row.ordinal)
                if model==TraceEvidence:args.update(event_id=row.event_id,projection_version=row.projection_version)
                db.add(model(**args))
    db.flush()
    # A partial reparse can carry older samples. Audit them in this new snapshot too.
    for carried_row in carried:
        if carried_row.payload.get("rule_audit", {}).get("version") == RULE_VERSION:
            continue
        execution = deepcopy(carried_row.payload)
        prior_scope = {"snapshot_id": old}
        execution["events"] = q.process_records(db, prior_scope, carried_row.trace_id, TraceEvent)
        execution["calls"] = q.process_records(db, prior_scope, carried_row.trace_id, TraceCall)
        for event in execution["events"]:
            event["risk_level"] = "unknown"
        source_evidence = recorded_audit(carried_row.result.get("source_record", {}), execution, execution["events"], execution["calls"])
        audit_rows = audit_process(execution, execution["events"], execution["calls"], source_evidence,
                                   record=carried_row.result.get("source_record", {}))
        current = db.scalar(select(TraceSample).where(TraceSample.snapshot_id==sid, TraceSample.trace_id==carried_row.trace_id))
        current.payload = {k:v for k,v in execution.items() if k not in {"events","calls"}}
        for model, field, identity in ((TraceEvent,"events","event_id"),(TraceCall,"calls","call_id")):
            values = {e[identity]:e for e in execution[field]}
            for row in db.scalars(select(model).where(model.snapshot_id==sid,model.trace_id==carried_row.trace_id)):
                row.payload = values[getattr(row,identity)]
        for row in list(db.scalars(select(TraceEvidence).where(TraceEvidence.snapshot_id==sid,TraceEvidence.trace_id==carried_row.trace_id))):
            db.delete(row)
        db.flush()
        for evidence in audit_rows:
            db.add(TraceEvidence(id=stable_id("row_",sid,carried_row.trace_id,evidence["evidence_id"]), snapshot_id=sid,
                trace_id=carried_row.trace_id,event_id=evidence["event_id"],projection_version=VERSION,payload=evidence))
    start = max((s.ingest_order for s in carried), default=-1) + 1
    if staging_task_id:
        snapshot.payload = {**snapshot.payload, "next_order": start, "file_updates": {}, "parsed_count": 0}
        db.add_all(statistics_sample(row) for row in db.scalars(
            select(TraceSample).where(TraceSample.snapshot_id == sid)))
    else:
        if prepared is None:
            prepared = (prepare_sample(sample, n) for n, sample in enumerate(parsed, start))
        for n, (sample, projection) in enumerate(zip(parsed, prepared, strict=True), start):
            store_prepared(db, sid, sample, n, projection)
        activate_snapshot(db, batch, snapshot)
    db.flush()
    return sid


def activate_snapshot(db, batch, snapshot):
    from app.services.agent_trace_statistics import VERSION as STATS_VERSION

    sid = snapshot.snapshot_id
    snapshot.payload = {k: v for k, v in snapshot.payload.items()
                        if k not in {"staging_task_id", "next_order", "file_updates", "parsed_count", "rules_complete",
                                     "model_completed_trace_count", "model_unavailable_trace_count"}}
    for version in (VERSION, STATS_VERSION):
        db.add(TraceProjection(projection_id=stable_id("proj_", sid, version), snapshot_id=sid,
                               version=version, payload={"policy_version": version, "data_origin": "actual"}))
    batch.active_snapshot_id = sid
    batch.payload = {**batch.payload, "staging_changed": False}


def store_prepared(db, sid, sample, order, projection, *, stage=False):
    trace, payload, events, calls, relations, audit_rows = projection
    identity = payload.get("original_sample_id")
    row = TraceSample(id=stable_id("sample_", sid, trace), snapshot_id=sid, trace_id=trace,
                      sample_id=str(identity or sample["id"]),
                      display_id=str(identity) if identity and len(str(identity)) <= 64 else f"AUD-{order+1:04}",
                      ingest_order=order, language=sample["language"],
                      result={k: v for k, v in sample.items() if k != "execution"}, payload=payload)
    db.add(row)
    for evidence in audit_rows:
        db.add(TraceEvidence(id=stable_id("row_", sid, trace, evidence["evidence_id"]), snapshot_id=sid,
              trace_id=trace, event_id=evidence["event_id"], projection_version=VERSION, payload=evidence))
    for event in events:
        db.add(TraceEvent(id=stable_id("row_", sid, trace, event["event_id"]), snapshot_id=sid,
              trace_id=trace, event_id=event["event_id"], ordinal=event["ordinal"], payload=event))
    for call in calls:
        db.add(TraceCall(id=stable_id("row_", sid, trace, call["call_id"]), snapshot_id=sid,
              trace_id=trace, call_id=call["call_id"], ordinal=call["order"], payload=call))
    for relation in relations:
        db.add(TraceRelation(id=stable_id("row_", sid, trace, relation["edge_id"]), snapshot_id=sid,
              trace_id=trace, payload=relation))
    if stage:
        from app.services.agent_trace_statistics import statistics_sample
        db.add(statistics_sample(row))


def parse_asset(file_id, filename, path, digest, override):
    errors, success = [], []
    formats, candidate = set(), 0
    try:
        raw_bytes = Path(path).read_bytes()
        if len(raw_bytes) > MAX_BYTES or hashlib.sha256(raw_bytes).hexdigest() != digest:
            raise ValueError("源文件完整性校验失败")
        for record, raw, line, error in records(raw_bytes.decode("utf-8-sig"), Path(filename).suffix.lower()):
            candidate += 1
            if candidate > 10000:
                raise ValueError("单文件轨迹记录超过10000条")
            try:
                if error:
                    raise ValueError("JSONL记录无法解析")
                for root in split_roots(record):
                    value = normalize(root, file_id, len(success), raw, line, override)
                    value["execution"]["source"]["filename"] = filename
                    success.append(value)
                    formats.add(value["execution"]["source"]["format_code"])
            except (ValueError, KeyError, TypeError, AttributeError):
                errors.append({"record_index": candidate, "line_start": line, "source_path": "$",
                               "code": "invalid_record", "message": "记录结构或调用参数无效"})
    except (OSError, ValueError, UnicodeError):
        errors.append({"record_index": 1, "line_start": 1, "source_path": "$",
                       "code": "invalid_file", "message": "文件格式、编码或完整性无效"})
    detected = "mixed" if len(formats) > 1 else next(iter(formats), None)
    return success, {
        "detected_format": detected, "recognition_state": "recognized" if detected else "unknown",
        "parse_status": ("partial" if success else "failed") if errors else "succeeded",
        "parsed_trace_count": len(success), "failed_record_count": len(errors),
        "input_candidate_count": max(candidate, len(success) + len(errors)), "error_examples": errors[:3],
    }


def run(tid, sessions=SessionLocal):
    from app.services.agent_trace_batch_audit import run_batch

    run_batch(tid, sessions)

def sample_item(row):
    p=row.payload
    return {**q.sample_summary(row),"filename":p["source"].get("filename","价值分析记录"),"source_format":p["source"].get("format_code","value_analysis"),
            "test_category":(p.get("test_category") or "unknown") if not isinstance(p.get("test_category"),dict) else p["test_category"].get("code","unknown"),"issue_types":p.get("issue_types",[])}


def selected_rows(db,scope,params):
    stmt=select(TraceSample).where(TraceSample.snapshot_id==scope["snapshot_id"]).order_by(TraceSample.ingest_order,TraceSample.trace_id)
    result=list(db.scalars(stmt));source=params.get("source_id");issue=params.get("issue_type");search=str(params.get("q","")).strip().casefold()
    if len(search)>100:q.fail(422,"搜索过长")
    if source and not any(r.payload["source"]["source_id"]==source for r in result):q.fail(409,"来源不属于此快照")
    return [r for r in result if (not source or r.payload["source"]["source_id"]==source) and (not issue or issue in r.payload.get("issue_types",[]))
            and (not search or search in (r.display_id+" "+r.sample_id).casefold())]


def samples(db,scope,params):
    return q.page([sample_item(r) for r in selected_rows(db,scope,params)],q.number(params,"page",1,2147483647),q.number(params,"page_size",20))


def detail(db,scope,trace,params,user):
    row=q.get_sample(db,scope,trace);p=row.payload
    if params.get("source_id") and params["source_id"]!=p["source"]["source_id"]:q.fail(409,"样本来源不匹配")
    all_events=q.process_records(db,scope,trace,TraceEvent)
    types=set(params.get("event_types","").split(','))-{''}
    if types-{"message","run","recorded_step","value_result","tool_call","tool_result","policy_decision","user","thought","final","error","system"}:q.fail(422,"事件类型无效")
    events=[e for e in all_events if not types or e["event_type"] in types]
    eid=params.get("event_id")
    if eid and not any(e["event_id"]==eid for e in all_events):q.fail(404,"事件不存在")
    if eid and not any(e["event_id"]==eid for e in events):q.fail(409,"选中事件被当前筛选排除")
    chosen=next((e for e in events if e["event_id"]==eid),None) if eid else next((e for e in events if e["event_type"]=="tool_call"),events[0] if events else None)
    size=q.number(params,"event_page_size",50,200);num=q.number(params,"event_page",1,2147483647)
    if eid:num=events.index(chosen)//size+1
    calls={c["call_id"]:c for c in q.process_records(db,scope,trace,TraceCall)}
    rendered=[]
    for e in events:
        c=calls.get(e.get("call_id"),{})
        rendered.append({**e,"tool_name":c.get("tool_name"),"input_json":c.get("input_json"),"output_json":c.get("output_json"),"duration_ms":c.get("duration_ms")})
    raw=p.get("raw_text") or dumps(row.result)
    if not q.permissions(user)["can_view_raw"]:raw=dumps(q.redact(row.result))
    preview=q.text_preview(raw,32768)
    related=selected_rows(db,scope,{k:v for k,v in params.items() if k not in {"trace_id","event_id","event_page"}})
    position=next((i for i,r in enumerate(related) if r.trace_id==trace),None)
    if position is None:q.fail(409,"样本不属于当前筛选")
    def locator(i):
        if i<0 or i>=len(related):return None
        r=related[i];return {"trace_id":r.trace_id,"display_id":r.display_id,"source_id":r.payload["source"]["source_id"],"page":i//q.number(params,"page_size",20)+1}
    normalized=next((e for e in rendered if chosen and e["event_id"]==chosen["event_id"]),None)
    return {**sample_item(row),**scope,"selected_event_id":chosen["event_id"] if chosen else None,"selected_event_page":events.index(chosen)//size+1 if chosen else None,
        "events":q.page(q.redact(rendered,preserve_ids=True),num,size),"normalized_event":q.redact(normalized,preserve_ids=True),
        "event_options":q.redact([{k:e.get(k) for k in ("event_id","ordinal","event_type","label","tool_name")} for e in rendered],preserve_ids=True),
        "raw_preview":{"raw_text":preview["text"],"kind":"full" if not preview["truncated"] else "fragment","truncated":preview["truncated"],"line_start":1,"total_lines":raw.count('\n')+1},
        "source_ref":chosen.get("source_ref") if chosen and chosen.get("source_ref") else {"source_path":"$","record_index":row.ingest_order+1},
        "mapping":{"format_label":p["source"]["format_label"],"rows":[{"source_path":(chosen.get("source_ref",{}).get("source_path","$") if chosen else "$"),"target_path":"event","description":"源日志到规范事件","state":"mapped" if p.get("data_origin")=="actual" else "generated"}]},
        "navigation":{"previous":locator(position-1),"next":locator(position+1)}}


def workspace(db,params,user):
    from collections import Counter
    options=q.workspace(db,{"include":"options","option_q":params.get("option_q",""),"batch_option_page":params.get("batch_option_page",1)},"tools",user)["batch_options"]
    bid=params.get("batch_id") or (options["items"][0]["batch_id"] if options["items"] else None)
    batch=db.get(TraceBatch,bid) if bid else None
    if bid and not batch:q.fail(404,"批次不存在")
    if batch:q.llm_access(db,batch)
    for item in options["items"]:item.update(batch_dict(db.get(TraceBatch,item["batch_id"]),user))
    output={"resolved_selection":{"batch_id":bid,"snapshot_id":None,"source_id":None,"trace_id":None},"formats":FORMATS,
        "upload_policy":{"allowed_extensions":[".json",".jsonl"],"max_file_bytes":MAX_BYTES,"max_files_per_request":20},
        "source_options":q.page([],1,50),"batch_options":options,"batch":batch_dict(batch,user) if batch else None,"files":q.page([],1,20),
        "summary":{"file_count":0,"supported_format_count":len(FORMATS),"imported_format_count":0,"parsed_trace_count":0,"failed_record_count":0,"category_mode":"single","by_language":[],"by_category":[],"by_format":[]},
        "validation":{"trace_total":0,"metrics":[{"code":c,"passed":0,"eligible":0,"missing":0,"failed":0,"not_applicable":0,"state":"not_applicable"} for c in ("structure","call_link","time","usage")],"can_continue":False,"blockers":["尚无有效轨迹"]},
        "issues":[],"sample_page":q.page([],1,20),"active_tasks":[],"export_options":{"scopes":["batch","filtered","sample"],"anonymize_required":True,
            "contents":[{"code":c,"availability":"available"} for c in ("normalized_json","offline_html","evidence_index","manifest","error_report")]}}
    if not batch:return output
    from app.repositories.task_repository import task_to_dict
    output["active_tasks"]=[task_to_dict(t) for t in db.scalars(select(Task).where(Task.capability_code.in_(["agent_trace_ingest","agent_trace_export"]),Task.status.in_(["pending","running"]),Task.input_data["batch_id"].as_string()==bid))]
    file_rows=[]
    for b in bindings(db,bid):
        a=db.get(TraceAsset,b.file_id)
        file_rows.append({"file_id":a.file_id,"filename":a.filename,"size_bytes":a.size_bytes,**b.payload})
    sid=params.get("snapshot_id") or batch.active_snapshot_id
    if not sid:
        if params.get("source_id") and params["source_id"] not in {f["file_id"] for f in file_rows}:q.fail(409,"来源不属于此批次")
        output["files"]=q.page(file_rows,q.number(params,"files_page",1,2147483647),20)
        return output
    _,scope=q.resolve(db,bid,params)
    all_rows=list(db.scalars(select(TraceSample).where(TraceSample.snapshot_id==sid).order_by(TraceSample.ingest_order,TraceSample.trace_id)))
    from app.services.agent_trace_rules import audit_summary
    output["audit_summary"] = audit_summary([r.payload for r in all_rows])
    sources={r.payload["source"]["source_id"]:r.payload["source"] for r in all_rows}
    options_source=[{"source_id":i,"filename":v.get("filename","价值分析记录")} for i,v in sources.items()]
    oq=params.get("option_q","").casefold()
    options_source=[s for s in options_source if oq in s["filename"].casefold()]
    output["source_options"]=q.page(options_source,q.number(params,"option_page",1,2147483647),50)
    if not file_rows and batch.source_system != "upload":
        file_rows=[{"file_id":i,"filename":v.get("filename","价值分析记录"),"size_bytes":0,"detected_format":v.get("format_code"),"format_override":"auto","recognition_state":"recognized","parse_status":"succeeded",
            "parsed_trace_count":sum(r.payload["source"]["source_id"]==i for r in all_rows),"input_candidate_count":None,"failed_record_count":0,"error_examples":[]} for i,v in sources.items()]
    output["files"]=q.page(file_rows,q.number(params,"files_page",1,2147483647),20)
    source=params.get("source_id")
    if not source and not params.get("issue_type"):
        selected_trace = next((r for r in all_rows if r.trace_id == params.get("trace_id")), None)
        source = selected_trace.payload["source"]["source_id"] if selected_trace else next(iter(sources),None)
    selection_params={**params,"source_id":source}
    selected=selected_rows(db,scope,selection_params)
    target=params.get("trace_id")
    if target and not any(r.trace_id==target for r in selected):q.fail(409,"目标样本不在查询范围")
    chosen=next((r for r in selected if r.trace_id==target),selected[0] if selected else None)
    current_page=q.number(params,"page",1,2147483647)
    if target:current_page=selected.index(chosen)//q.number(params,"page_size",20)+1
    output["sample_page"]=samples(db,scope,{**selection_params,"page":current_page})
    output["resolved_selection"]={"batch_id":bid,"snapshot_id":sid,"source_id":source or (chosen.payload["source"]["source_id"] if chosen else None),"trace_id":chosen.trace_id if chosen else None}
    formats=Counter(r.payload["source"].get("format_code","value_analysis") for r in all_rows)
    cats=Counter(sample_item(r)["test_category"] for r in all_rows)
    output["summary"]={"file_count":len(sources),"supported_format_count":len(FORMATS),"imported_format_count":len(formats),"parsed_trace_count":len(all_rows),"failed_record_count":sum(f["failed_record_count"] for f in file_rows),
        "category_mode":"single","by_language":[{"code":l,"count":n} for l,n in Counter(r.language for r in all_rows).items()],
        "by_category":[{"code":c,"label":c,"count":n} for c,n in cats.items()],"by_format":[{"code":c,"trace_count":n} for c,n in formats.items()]}
    metrics=[]
    for code,issue in (("structure","structure_invalid"),("call_link","call_unlinked"),("time","time_missing"),("usage","usage_missing")):
        na=sum(r.payload["call_count"]==0 and "call_unlinked" not in r.payload.get("issue_types",[]) for r in all_rows) if code=="call_link" else 0
        missing=sum(issue in r.payload.get("issue_types",[]) for r in all_rows);eligible=len(all_rows)-na
        metrics.append({"code":code,"passed":max(0,eligible-missing),"eligible":eligible,"missing":missing if code in {"time","usage"} else 0,
                        "failed":missing if code in {"structure","call_link"} else 0,"not_applicable":na,"state":"missing" if missing else "verified" if eligible else "not_applicable"})
    output["validation"]={"trace_total":len(all_rows),"metrics":metrics,"can_continue":bool(all_rows),"blockers":[] if all_rows else ["没有有效轨迹"]}
    issue_codes=sorted({i for r in all_rows for i in r.payload.get("issue_types",[])})
    output["issues"]=[{"code":code,"severity":"advisory","affected_trace_count":sum(code in r.payload.get("issue_types",[]) for r in all_rows),"impact":"影响对应指标的完整性，保留源记录",
        "examples":[{"trace_id":r.trace_id,"display_id":r.display_id,"source_id":r.payload["source"]["source_id"],"page":i//20+1} for i,r in enumerate(all_rows) if code in r.payload.get("issue_types",[])][:3]} for code in issue_codes]
    return output
