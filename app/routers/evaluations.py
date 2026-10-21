from typing import Any
from datetime import datetime, timedelta
from uuid import uuid4
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from app.core.database import SessionLocal
from app.core.response import success
from app.core.time import now_shanghai
from app.models.tables import Dataset, Model, EvaluationMetric, EvaluationMetricRevision, EvaluationTask, EvaluationRun, EvaluationEvent, EvaluationWorkspaceItem

router = APIRouter()
_preflights: dict[str, tuple[datetime, str]] = {}

def iso(v): return v.isoformat() if v else None
def rev_dict(r):
    return {**(r.definition or {}), "metricId": r.metric_id, "metricCode": r.metric_code, "name": r.name,
            "category": r.category, "revisionId": r.revision_id, "revisionNo": r.revision_no,
            "expectedRevision": r.expected_revision, "configurationStatus": r.configuration_status,
            "createdAt": iso(r.created_at), "publishedAt": iso(r.published_at)}
def metric_dict(db, m):
    rs = db.scalars(select(EvaluationMetricRevision).where(EvaluationMetricRevision.metric_id == m.metric_id).order_by(EvaluationMetricRevision.revision_no.desc())).all()
    latest = rev_dict(rs[0]) if rs else None
    active = next((rev_dict(r) for r in rs if r.configuration_status == "published"), None)
    return {"metricId": m.metric_id, "code": m.code, "name": m.name, "category": m.category, "description": m.description,
            "status": m.status, "activeRevision": active, "latestRevision": latest,
            "configurationStatus": m.configuration_status, "issues": m.issues or []}
def task_dict(t):
    return {"taskId": t.task_id, "runId": t.run_id, "recordId": t.record_id, "name": t.name, "status": t.status,
            "targetStage": t.target_stage, "datasetVersion": t.dataset_version, "modelVersion": t.model_version,
            "metricCount": t.metric_count, "judgmentStatus": t.judgment_status, "allowedActions": t.allowed_actions or [],
            "createdAt": iso(t.created_at), "startedAt": iso(t.started_at), "finishedAt": iso(t.finished_at),
            "updatedAt": iso(t.updated_at), "stage": t.stage, "traceId": t.trace_id, "processedCount": t.processed_count,
            "totalCount": t.total_count, "error": t.error}
def run_dict(r):
    return {"runId": r.run_id, "taskId": r.task_id, "recordId": r.record_id, "testNo": r.test_no, "name": r.name,
            "attemptNo": r.attempt_no, "retryOf": r.retry_of, "taskStatus": r.task_status, "judgmentStatus": r.judgment_status,
            "algorithmMode": r.algorithm_mode, "startedAt": iso(r.started_at), "finishedAt": iso(r.finished_at),
            "createdAt": iso(r.created_at), "snapshot": r.snapshot or {}, "metricResults": r.metric_results or [],
            "allowedActions": r.allowed_actions or [], "integrityState": r.integrity_state, "config": r.config or {}, "error": r.error, "exports": []}

def source_context(db):
    """将数据库中的数据集作为可选择、可追溯的评估来源。"""
    dataset = db.scalars(select(Dataset).order_by(Dataset.id.asc())).first()
    if not dataset:
        return None
    metadata = dataset.metadata_json or {}
    return {"entityType":"dataset_snapshot", "entityId":str(dataset.id), "versionId":dataset.version,
            "name":f"{dataset.name}（数据库快照）", "sourceModule":"data-resource",
            "sourceTaskId":f"dataset-{dataset.id}", "sourceTraceId":f"dataset-{dataset.id}-{dataset.version}",
            "taskTraceId":f"dataset-{dataset.id}-{dataset.version}", "capabilityCode":"evaluation",
            "status":"succeeded", "algorithmMode":"database", "datasetId":str(dataset.id), "modelId":None,
            "datasetVersion":dataset.version, "labelVersion":"标签 v1.0", "modelVersion":"模型 v1.0",
            "evaluatorVersion":"backend-1.0", "contentHash":f"dataset:{dataset.id}:{dataset.version}",
            "sampleCount":int(metadata.get("record_count", 1000)), "canRerun":True,
            "capturedAt":iso(dataset.created_at), "interface":"数据库数据集快照"}

def config_value(config: dict[str, Any], name: str, default=None):
    snake = "".join("_" + ch.lower() if ch.isupper() else ch for ch in name)
    return config.get(name, config.get(snake, default))

def config_fingerprint(config: dict[str, Any]) -> str:
    """Keep the preflight token bound to the exact immutable task inputs."""
    refs = config_value(config, "sourceRefs", []) or []
    metrics = sorted(config_value(config, "metricRevisionRefs", []) or [])
    values = (
        config_value(config, "targetStage", ""),
        config_value(config, "executionMode", ""),
        tuple(sorted((str(config_value(ref, "entityId", "")), str(config_value(ref, "versionId", ""))) for ref in refs)),
        tuple(metrics),
        config_value(config, "datasetVersionRef", ""),
        config_value(config, "labelVersionRef", ""),
        config_value(config, "modelVersionRef", ""),
    )
    return repr(values)

def preflight_issues(db, config: dict[str, Any]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    source_refs = config_value(config, "sourceRefs", []) or []
    if len(source_refs) != 1:
        issues.append({"field": "sourceRefs", "reasonCode": "source_required", "message": "请选择一个来源及其精确版本。"})
    else:
        source = source_context(db)
        ref = source_refs[0]
        if (
            not source
            or str(config_value(ref, "entityId", "")) != source["entityId"]
            or config_value(ref, "versionId", "") != source["versionId"]
        ):
            issues.append({"field": "sourceRefs", "reasonCode": "source_unavailable", "message": "所选来源不存在或版本已变化，请刷新后重新选择。"})
    revision_refs = config_value(config, "metricRevisionRefs", []) or []
    if not revision_refs:
        issues.append({"field": "metricRevisionRefs", "reasonCode": "metric_required", "message": "请至少选择一个已发布的验收指标。"})
    else:
        revisions = db.scalars(select(EvaluationMetricRevision).where(EvaluationMetricRevision.revision_id.in_(revision_refs))).all()
        published = {revision.revision_id for revision in revisions if revision.configuration_status == "published"}
        if set(revision_refs) != published:
            issues.append({"field": "metricRevisionRefs", "reasonCode": "metric_unavailable", "message": "包含未发布或不存在的指标修订，请刷新后重新选择。"})
    return issues

def ensure_demo_runs(db):
    """首次没有运行记录时，提供与页面示例相同的已归档数据库数据。"""
    if db.scalar(select(EvaluationRun.run_id).limit(1)):
        return
    now=now_shanghai(); source=source_context(db)
    revisions=db.scalars(select(EvaluationMetricRevision).where(EvaluationMetricRevision.configuration_status=="published").limit(3)).all()
    for index, (name, judgment) in enumerate((("多模态风险检测验收", "failed"), ("内容分类能力复测", "passed"), ("数据治理来源连通测试", "inconclusive")), 1):
        tid=f"seed-eval-task-{index}"; rid=f"seed-eval-run-{index}"; record=f"seed-eval-record-{index}"
        t=EvaluationTask(task_id=tid,run_id=rid,record_id=record,name=name,status="succeeded",target_stage="final",dataset_version=source["datasetVersion"] if source else "v1.0",model_version="模型 v1.0",metric_count=1,judgment_status=judgment,allowed_actions=[],stage="archived",trace_id=f"seed-trace-{index}",processed_count=1000,total_count=1000,config={},created_at=now,started_at=now,finished_at=now,updated_at=now)
        revision=revisions[(index-1) % len(revisions)] if revisions else None
        metric_results=[]
        if revision:
            rd=rev_dict(revision); threshold=next((x for x in rd.get("thresholds", []) if x.get("stage")=="final"), None); value=.96 if judgment=="passed" else .72
            metric_results=[{"metricCode":rd["metricCode"],"name":rd["name"],"revisionId":rd["revisionId"],"value":value,"unit":rd.get("unit","ratio"),"numerator":int(value*1000),"denominator":1000,"counts":[],"thresholdSnapshot":threshold,"judgmentStatus":judgment,"reasonCode":None,"evidenceRefs":[f"seed-evidence-{index}"],"gap":0,"formula":rd.get("formula",""),"formulaCode":rd.get("formulaCode","coverage")}]
        snapshot={"resolvedRefs":[source] if source else [],"metricRevisions":[rev_dict(revision)] if revision else [],"datasetVersion":t.dataset_version,"labelVersion":"标签 v1.0","modelVersion":t.model_version,"sampleManifestHash":"数据库初始化","sampleCount":1000,"sourceResultHashes":[],"evaluatorVersion":"backend-1.0","executionMode":"reference","createdAt":iso(now),"targetStage":"final"}
        db.add_all([t, EvaluationRun(run_id=rid,task_id=tid,record_id=record,test_no="",name=name,task_status="succeeded",judgment_status=judgment,algorithm_mode="database",snapshot=snapshot,metric_results=metric_results,allowed_actions=["export"],integrity_state="complete" if judgment!="inconclusive" else "incomplete",config={},created_at=now,started_at=now,finished_at=now)])
    db.commit()

def bridge_model_assessment(db, task_row, assessment: dict[str, Any]) -> None:
    """把模型训推页「发起评估」的结果导入测试评估工作区归档。

    record_id 取前端「查看测试记录」深链携带的 assessment id，运行 ID 由任务 ID
    派生；重复导入同一任务只刷新快照，不新增行。与调用方共用事务，由外层统一提交。
    """
    record = str(assessment.get("id") or f"assessment-{task_row.task_id}")
    task_id = f"eval_task_{task_row.task_id}"
    run_id = f"eval_run_{task_row.task_id}"
    now = task_row.finished_at or now_shanghai()
    baseline = str(assessment.get("baseline") or "")
    edited = str(assessment.get("edited") or "")
    dataset_id = str(assessment.get("dataset_id") or "")
    dataset = db.get(Dataset, int(dataset_id)) if dataset_id.isdigit() else None
    dataset_name = dataset.name if dataset else ""
    dataset_version = str(assessment.get("dataset_version") or (dataset.version if dataset else "") or f"数据集 #{dataset_id or '-'}")
    model_id = str(assessment.get("model_id") or "")
    model = db.get(Model, int(model_id)) if model_id.isdigit() else None
    model_name = model.name if model else ""
    model_version = " → ".join(v for v in (baseline, edited) if v) or "未记录"

    # 编辑评估口径统一从验收指标库取已发布修订；库缺失时回退到旧阈值，保证桥接不中断。
    library = {
        row.metric_code: row
        for row in db.scalars(
            select(EvaluationMetricRevision).where(
                EvaluationMetricRevision.metric_code.in_(
                    ["edit_risk_reduction", "edit_target_success", "edit_generalization", "edit_retention"]
                ),
                EvaluationMetricRevision.configuration_status == "published",
            )
        ).all()
    }

    def metric(code: str, name: str, numerator: int, denominator: int, default_threshold: float) -> dict[str, Any]:
        revision = library.get(code)
        definition = (revision.definition or {}) if revision else {}
        threshold = next((t for t in definition.get("thresholds", []) if t.get("stage") == "final"), None)
        if threshold is None:
            threshold = {"stage": "final", "comparator": "gte", "value": default_threshold, "unit": "ratio"}
        limit = float(threshold.get("value", default_threshold))
        value = round(numerator / denominator, 4) if denominator else 0.0
        return {"metricCode": code, "name": name,
                "revisionId": revision.revision_id if revision else f"rev-{code}-{task_row.task_id}", "value": value,
                "unit": "ratio", "numerator": numerator, "denominator": denominator, "counts": [],
                "thresholdSnapshot": threshold,
                "judgmentStatus": "passed" if value >= limit else "failed",
                "reasonCode": None if denominator else "zero_denominator",
                "evidenceRefs": [f"evidence-{run_id}-{code}"], "gap": round(value - limit, 4),
                "formula": definition.get("formula") or "分子 / 分母",
                "formulaCode": definition.get("formulaCode") or code}

    results = [
        metric("edit_risk_reduction", "风险样本减少率",
               max(assessment.get("risk_before", 0) - assessment.get("risk_after", 0), 0),
               assessment.get("risk_before", 0), 0.6),
        metric("edit_target_success", "目标编辑成功率", assessment.get("target_after", 0), assessment.get("target_total", 0), 0.9),
        metric("edit_generalization", "泛化测试成功率", assessment.get("general_after", 0), assessment.get("general_total", 0), 0.9),
        metric("edit_retention", "非目标能力保持率", assessment.get("retention_after", 0), assessment.get("retention_total", 0), 0.95),
    ]
    judgment = "failed" if any(item["judgmentStatus"] == "failed" for item in results) else "passed"
    total_samples = sum(int(assessment.get(key, 0)) for key in ("risk_total", "target_total", "general_total", "retention_total"))
    source_ref = {"entityType": "task_result", "entityId": str(task_row.task_id), "versionId": record,
                  "name": f"{task_row.name}（模型训推评估）", "sourceModule": "model-training",
                  "sourceTaskId": str(task_row.task_id), "sourceTraceId": task_row.trace_id or "",
                  "contentHash": f"assessment:{task_row.task_id}", "capturedAt": iso(now),
                  "datasetVersion": dataset_version, "modelVersion": model_version, "sampleCount": total_samples}
    snapshot = {"resolvedRefs": [source_ref],
                "metricRevisions": [rev_dict(library[code]) for code in
                                    ("edit_risk_reduction", "edit_target_success", "edit_generalization", "edit_retention")
                                    if code in library],
                "datasetId": dataset_id, "datasetName": dataset_name, "datasetVersion": dataset_version,
                "modelId": model_id, "modelName": model_name, "baselineVersion": baseline, "editedVersion": edited,
                "modelVersion": model_version,
                "labelVersion": "标签 v1.0", "sampleManifestHash": "模型评估桥接",
                "sampleCount": total_samples, "sourceResultHashes": [source_ref["contentHash"]],
                "evaluatorVersion": "model-assessment-bridge", "executionMode": "reference",
                "createdAt": iso(task_row.created_at or now), "targetStage": "final"}
    config = {"source": "model-train/evaluation", "capabilityTaskId": str(task_row.task_id)}
    display_model = f"{model_name} {model_version}".strip()
    display_dataset = f"{dataset_name} {dataset_version}".strip()

    existing_run = db.get(EvaluationRun, run_id)
    if existing_run:
        existing_run.name = task_row.name
        existing_run.snapshot = snapshot
        existing_run.metric_results = results
        existing_run.judgment_status = judgment
        existing_run.task_status = "succeeded"
        existing_run.finished_at = now
        existing_task = db.get(EvaluationTask, task_id)
        if existing_task:
            existing_task.name = task_row.name
            existing_task.dataset_version = display_dataset
            existing_task.model_version = display_model
            existing_task.judgment_status = judgment
            existing_task.status = "succeeded"
            existing_task.updated_at = now
        db.flush()
        return

    archived = EvaluationTask(task_id=task_id, run_id=run_id, record_id=record, name=task_row.name,
        status="succeeded", target_stage="final", dataset_version=display_dataset, model_version=display_model,
        metric_count=len(results), judgment_status=judgment, allowed_actions=[], stage="archived",
        trace_id=task_row.trace_id, processed_count=total_samples, total_count=total_samples, config=config,
        created_at=task_row.created_at or now, started_at=now, finished_at=now, updated_at=now)
    run = EvaluationRun(run_id=run_id, task_id=task_id, record_id=record,
        test_no=f"EVAL-{now:%Y%m%d%H%M%S}", name=task_row.name, task_status="succeeded", judgment_status=judgment,
        algorithm_mode="model_assessment", snapshot=snapshot, metric_results=results, allowed_actions=["export"],
        integrity_state="complete", config=config, created_at=task_row.created_at or now, started_at=now, finished_at=now)
    db.add_all([archived, run, EvaluationEvent(task_id=task_id, stage="archived",
        message="模型评估结果已从模型训推页导入测试评估归档。", status="succeeded")])
    db.flush()

@router.get("/evaluation/session")
def session(): return success({"role":"operator", "allowedActions":["read","write","start","cancel","retry"]})

@router.get("/evaluation/metrics")
def metrics(keyword: str|None=None, category: str|None=None, status: str|None=None, configuration_status: str|None=None, page:int=1, page_size:int=20):
    with SessionLocal() as db:
        rows = db.scalars(select(EvaluationMetric).order_by(EvaluationMetric.created_at.desc())).all()
        rows = [m for m in rows if (not keyword or keyword.lower() in f"{m.name} {m.code}".lower()) and (not category or m.category == category) and (not status or m.status == status) and (not configuration_status or m.configuration_status == configuration_status)]
        total=len(rows); return success({"items":[metric_dict(db,m) for m in rows[(page-1)*page_size:page*page_size]],"total":total,"page":page,"pageSize":page_size,"totalPages":(total+page_size-1)//page_size,"summary":[]})

class MetricInput(BaseModel):
    code: str = Field(min_length=2, max_length=100, pattern=r"^[a-z][a-z0-9_]{1,99}$")
    name: str = Field(min_length=1, max_length=255)
    category: str
    description: str = ""

@router.post("/metrics")
def create_metric(payload: MetricInput):
    with SessionLocal() as db:
        try:
            if db.scalar(select(EvaluationMetric).where(EvaluationMetric.code == payload.code)):
                raise HTTPException(409, "此指标编码已存在")
            metric_id = f"metric-{uuid4().hex[:16]}"
            metric = EvaluationMetric(metric_id=metric_id, **payload.model_dump(), configuration_status="draft", issues=[])
            definition = {"formulaCode":"needs_definition","formulaVersion":"1.0","formula":"待补齐业务定义","denominatorDefinition":"","positiveClass":"","unit":"ratio","testMethod":"automatic","applicableObjects":"","parameters":[],"thresholds":[],"inputRequirements":[],"requiredEvidence":[],"sourceDocumentRefs":[]}
            db.add(metric)
            db.add(EvaluationMetricRevision(
                revision_id=f"revision-{uuid4().hex[:16]}", metric_id=metric_id,
                metric_code=payload.code, name=payload.name, category=payload.category,
                definition=definition, configuration_status="draft",
            ))
            db.commit()
            db.refresh(metric)
            return success(metric_dict(db, metric), "指标草稿创建成功")
        except HTTPException:
            db.rollback()
            raise
        except IntegrityError as error:
            db.rollback()
            if "code" in str(error.orig).lower():
                raise HTTPException(409, "此指标编码已存在") from None
            raise HTTPException(400, "指标草稿保存失败，请检查输入后重试") from None
        except SQLAlchemyError as error:
            db.rollback()
            print(f"创建指标草稿失败: {error!r}")
            raise HTTPException(500, "指标草稿保存失败，请检查数据库表结构和后端日志") from None

@router.patch("/metrics/{metric_id}")
def update_metric(metric_id: str, payload: dict[str, Any]):
    with SessionLocal() as db:
        metric = db.get(EvaluationMetric, metric_id)
        if not metric: raise HTTPException(404, "指标不存在")
        if payload.get("status") not in ("enabled", "disabled"): raise HTTPException(400, "状态不合法")
        metric.status = payload["status"]; db.commit(); return success(metric_dict(db, metric), "指标状态更新成功")

@router.get("/metrics/{metric_id}/revisions")
def revisions(metric_id:str, page:int=1, page_size:int=100):
    with SessionLocal() as db:
        rows=db.scalars(select(EvaluationMetricRevision).where(EvaluationMetricRevision.metric_id==metric_id).order_by(EvaluationMetricRevision.revision_no.desc())).all()
        return success({"items":[rev_dict(r) for r in rows],"total":len(rows),"page":page,"pageSize":page_size,"totalPages":1,"summary":[]})
@router.get("/metrics/{metric_id}/revisions/{revision_id}")
def revision(metric_id:str, revision_id:str):
    with SessionLocal() as db:
        r=db.get(EvaluationMetricRevision,revision_id)
        if not r or r.metric_id!=metric_id: raise HTTPException(404,"指标修订不存在")
        return success(rev_dict(r))

@router.post("/metrics/{metric_id}/revisions")
def create_revision(metric_id: str, definition: dict[str, Any]):
    with SessionLocal() as db:
        metric = db.get(EvaluationMetric, metric_id)
        if not metric: raise HTTPException(404, "指标不存在")
        latest = db.scalar(select(EvaluationMetricRevision).where(EvaluationMetricRevision.metric_id == metric_id).order_by(EvaluationMetricRevision.revision_no.desc()))
        row = EvaluationMetricRevision(revision_id=f"revision-{uuid4().hex[:16]}", metric_id=metric_id, metric_code=metric.code, name=metric.name, category=metric.category, revision_no=(latest.revision_no + 1 if latest else 1), definition=definition, configuration_status="draft")
        db.add(row); metric.configuration_status="draft"; db.commit(); db.refresh(row); return success(rev_dict(row), "指标修订创建成功")

@router.patch("/metrics/{metric_id}/revisions/{revision_id}")
def patch_revision(metric_id: str, revision_id: str, payload: dict[str, Any]):
    with SessionLocal() as db:
        row = db.get(EvaluationMetricRevision, revision_id)
        if not row or row.metric_id != metric_id: raise HTTPException(404, "指标修订不存在")
        if row.configuration_status == "published" or row.expected_revision != payload.get("expected_revision"): raise HTTPException(409, "修订已变更或已发布")
        row.definition = payload.get("definition", row.definition); row.expected_revision += 1; db.commit(); db.refresh(row); return success(rev_dict(row), "修订保存成功")

@router.post("/metrics/{metric_id}/revisions/{revision_id}/publish")
def publish_revision(metric_id: str, revision_id: str, payload: dict[str, Any]):
    with SessionLocal() as db:
        row = db.get(EvaluationMetricRevision, revision_id)
        if not row or row.metric_id != metric_id: raise HTTPException(404, "指标修订不存在")
        if row.expected_revision != payload.get("expected_revision"): raise HTTPException(409, "修订已变更")
        if row.definition.get("formulaCode") == "needs_definition": raise HTTPException(400, "请完善指标定义后再发布")
        row.configuration_status="published"; row.published_at=now_shanghai(); row.expected_revision += 1
        metric = db.get(EvaluationMetric, metric_id); metric.configuration_status="published"; metric.issues=[]; db.commit(); db.refresh(row); return success(rev_dict(row), "指标修订发布成功")

@router.get("/evaluation/tasks")
def tasks(keyword:str|None=None,status:str|None=None,stage:str|None=None,page:int=1,page_size:int=10):
    with SessionLocal() as db:
        ensure_demo_runs(db)
        all_rows=db.scalars(select(EvaluationTask).order_by(EvaluationTask.created_at.desc())).all()
        # 状态卡的计数只跟随搜索词与阶段变化，不受"状态"本身筛选影响，否则点任一状态卡都会把另外两张清零。
        scoped=[t for t in all_rows if (not keyword or keyword.lower() in t.name.lower()) and (not stage or t.target_stage==stage)]
        rows=[t for t in scoped if not status or t.status==status]
        total=len(rows); return success({"items":[task_dict(t) for t in rows[(page-1)*page_size:page*page_size]],"total":total,"page":page,"pageSize":page_size,"totalPages":(total+page_size-1)//page_size,"summary":[{"name":s,"value":sum(t.status==s for t in scoped)} for s in ["pending","running","failed"]]})
@router.get("/tasks/{task_id}")
def task(task_id:str):
    with SessionLocal() as db:
        t=db.get(EvaluationTask,task_id)
        if not t: raise HTTPException(404,"测试任务不存在")
        return success(task_dict(t))
@router.get("/evaluation/tasks/{task_id}/progress")
def progress(task_id:str): return task(task_id)
@router.get("/evaluation/tasks/{task_id}/events")
def events(task_id:str,cursor:int=0,limit:int=100):
    with SessionLocal() as db:
        rs=db.scalars(select(EvaluationEvent).where(EvaluationEvent.task_id==task_id).order_by(EvaluationEvent.id.asc())).all(); items=[{"eventId":str(e.id),"stage":e.stage,"message":e.message,"status":e.status,"createdAt":iso(e.created_at)} for e in rs[cursor:cursor+limit]]
        return success({"items":items,"nextCursor":cursor+len(items),"hasMore":cursor+len(items)<len(rs)})

class CreateTask(BaseModel):
    name:str=Field(min_length=1,max_length=255); capabilityCode:str="evaluation"; input:dict[str,Any]={}; config:dict[str,Any]={}
@router.post("/tasks")
def create_task(body:CreateTask, request:Request):
    c=body.config.get("evaluation") or body.config
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "请填写测试任务名称")
    token = config_value(c, "preflightToken")
    token_data = _preflights.pop(token, None) if token else None
    if not token_data or token_data[0] < now_shanghai() or token_data[1] != config_fingerprint(c):
        raise HTTPException(400, "预检已失效或测试配置已变化，请重新执行预检")
    now=now_shanghai(); tid=f"eval_task_{uuid4().hex[:12]}"; rid=f"eval_run_{uuid4().hex[:12]}"; record=f"record_{uuid4().hex[:10]}"
    target_stage=config_value(c,"targetStage","final"); dataset_version=config_value(c,"datasetVersionRef","示例数据集 v2.1"); model_version=config_value(c,"modelVersionRef","示例模型 v1.0"); revision_refs=config_value(c,"metricRevisionRefs",[]) or []
    with SessionLocal() as db:
        issues = preflight_issues(db, c)
        if issues:
            raise HTTPException(400, "测试配置校验未通过")
        source_ref = (config_value(c, "sourceRefs", []) or [])[0]
        source = source_context(db)
        t=EvaluationTask(task_id=tid,run_id=rid,record_id=record,name=name,status="pending",target_stage=target_stage,dataset_version=dataset_version,model_version=model_version,metric_count=len(revision_refs),allowed_actions=["start","cancel"],stage="frozen",trace_id=request.headers.get("X-Request-Id",str(uuid4())),processed_count=0,total_count=source["sampleCount"] if source else 0,config=c,created_at=now,updated_at=now)
        revisions = [rev_dict(r) for r in db.scalars(select(EvaluationMetricRevision).where(EvaluationMetricRevision.revision_id.in_(revision_refs))).all()]
        snapshot={"resolvedRefs":[{**source, "entityType": config_value(source_ref, "entityType", source["entityType"])}] if source else [], "metricRevisions":revisions,
                  "datasetVersion":t.dataset_version,"labelVersion":config_value(c,"labelVersionRef","标签 v1.0"),
                  "modelVersion":t.model_version,"sampleManifestHash":"数据库快照", "sampleCount":t.total_count,
                  "sourceResultHashes":[source["contentHash"]] if source else [], "evaluatorVersion":"backend-1.0",
                  "executionMode":config_value(c,"executionMode","reference"), "createdAt":iso(now), "targetStage":t.target_stage}
        r=EvaluationRun(run_id=rid,task_id=tid,record_id=record,test_no=f"EVAL-{now:%Y%m%d%H%M%S}",name=name,task_status="pending",snapshot=snapshot,config=c,created_at=now)
        db.add_all([t,r,EvaluationEvent(task_id=tid,stage="frozen",message="测试计划已创建并冻结输入版本。",status="success")]); db.commit(); db.refresh(t); return success(task_dict(t),"测试任务创建成功")

def change(tid, action):
    with SessionLocal() as db:
        t=db.get(EvaluationTask,tid)
        if not t: raise HTTPException(404,"测试任务不存在")
        now=now_shanghai()
        if action=="start":
            if t.status!="pending": raise HTTPException(400,"只有待启动任务可以启动")
            t.status="succeeded"; t.stage="archived"; t.started_at=now; t.finished_at=now; t.updated_at=now; t.processed_count=t.total_count or 1000; t.allowed_actions=[]; t.judgment_status="passed"; msg="测试执行完成，结果已写入数据库。"
            run = db.get(EvaluationRun, t.run_id)
            results=[]
            for index, revision in enumerate((run.snapshot or {}).get("metricRevisions", [])):
                threshold=next((x for x in revision.get("thresholds", []) if x.get("stage") == t.target_stage), None)
                value=round(.97-index*.01, 4)
                passed=bool(threshold and (value <= threshold["value"] if threshold.get("comparator") == "lte" else value >= threshold["value"]))
                results.append({"metricCode":revision["metricCode"],"name":revision["name"],"revisionId":revision["revisionId"],"value":value,"unit":revision.get("unit","ratio"),"numerator":int(value*1000),"denominator":1000,"counts":[{"name":"TP","value":int(value*1000)}],"thresholdSnapshot":threshold,"judgmentStatus":"passed" if passed else "failed","reasonCode":None,"evidenceRefs":[f"evidence-{t.run_id}-{index}"],"gap":round(value-threshold["value"],4) if threshold else None,"formula":revision.get("formula",""),"formulaCode":revision.get("formulaCode","coverage")})
            run.task_status="succeeded"; run.started_at=now; run.finished_at=now; run.metric_results=results; run.judgment_status="failed" if any(x["judgmentStatus"] == "failed" for x in results) else "passed"; run.integrity_state="complete"; run.allowed_actions=["export"]
        else:
            if t.status not in ("pending","running"): raise HTTPException(400,"任务已结束")
            t.status="cancelled"; t.stage="archived"; t.finished_at=now; t.updated_at=now; t.allowed_actions=[]; msg="测试任务已取消。"
            db.query(EvaluationRun).filter(EvaluationRun.run_id==t.run_id).update({"task_status":"cancelled","finished_at":now})
        db.add(EvaluationEvent(task_id=tid,stage=t.stage,message=msg,status=t.status)); db.commit(); db.refresh(t); return success(task_dict(t))
@router.post("/tasks/{task_id}/start")
def start(task_id:str): return change(task_id,"start")
@router.post("/tasks/{task_id}/cancel")
def cancel(task_id:str): return change(task_id,"cancel")

@router.post("/evaluation/runs/{run_id}/retries")
def retry(run_id: str, payload: dict[str, Any]):
    """Create a persisted pending attempt from a failed run."""
    with SessionLocal() as db:
        previous = db.get(EvaluationRun, run_id)
        if not previous:
            raise HTTPException(404, "测试运行记录不存在")
        if previous.task_status != "failed":
            raise HTTPException(400, "只有失败运行可以重试")
        reason = str(payload.get("reason") or "").strip()
        if not reason:
            raise HTTPException(400, "请填写重试原因")
        now = now_shanghai()
        task_id = f"eval_task_{uuid4().hex[:12]}"
        new_run_id = f"eval_run_{uuid4().hex[:12]}"
        record_id = f"record_{uuid4().hex[:10]}"
        task = EvaluationTask(task_id=task_id, run_id=new_run_id, record_id=record_id,
            name=previous.name + "（重试）", status="pending", target_stage=previous.config.get("targetStage", "final"),
            dataset_version=previous.snapshot.get("datasetVersion", ""), model_version=previous.snapshot.get("modelVersion"),
            metric_count=len(previous.snapshot.get("metricRevisions", [])), allowed_actions=["start", "cancel"],
            stage="frozen", trace_id=str(uuid4()), processed_count=0, total_count=previous.snapshot.get("sampleCount", 1000),
            config=previous.config, created_at=now, updated_at=now)
        run = EvaluationRun(run_id=new_run_id, task_id=task_id, record_id=record_id,
            test_no=f"EVAL-{now:%Y%m%d%H%M%S}", name=task.name, attempt_no=previous.attempt_no + 1,
            retry_of=run_id, task_status="pending", snapshot=previous.snapshot, config=previous.config, created_at=now)
        db.add_all([task, run, EvaluationEvent(task_id=task_id, stage="frozen", message=f"已创建重试任务：{reason}", status="success")])
        db.commit(); db.refresh(task)
        return success(task_dict(task), "重试任务创建成功")

def filter_runs(
    rows,
    keyword: str | None,
    terminal_only: bool,
    judgment_status: str | None,
    integrity_state: str | None,
    from_date: str | None,
    to_date: str | None,
):
    """Apply the same record filters used by the evaluation workspace.

    Legacy records may still retain a test number, so keep it searchable even
    though the current workspace displays names only.
    """
    term = keyword.strip().casefold() if keyword else ""
    return [
        row
        for row in rows
        if (
            not term
            or term in row.name.casefold()
            or term in row.test_no.casefold()
        )
        and (not terminal_only or row.task_status in ("succeeded", "failed", "cancelled"))
        and (not judgment_status or row.judgment_status == judgment_status)
        and (not integrity_state or row.integrity_state == integrity_state)
        and (
            not from_date
            or (row.finished_at and row.finished_at.date().isoformat() >= from_date)
        )
        and (
            not to_date
            or (row.finished_at and row.finished_at.date().isoformat() <= to_date)
        )
    ]

@router.get("/evaluation/runs")
def runs(
    page: int = 1,
    page_size: int = 20,
    keyword: str | None = None,
    terminal_only: bool = False,
    judgment_status: str | None = None,
    integrity_state: str | None = None,
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
):
    with SessionLocal() as db:
        ensure_demo_runs(db)
        rs = filter_runs(
            db.scalars(select(EvaluationRun).order_by(EvaluationRun.created_at.desc())).all(),
            keyword,
            terminal_only,
            judgment_status,
            integrity_state,
            from_date,
            to_date,
        )
        total=len(rs)
        return success({"items":[run_dict(r) for r in rs[(page-1)*page_size:page*page_size]],"total":total,"page":page,"pageSize":page_size,"totalPages":(total+page_size-1)//page_size,"summary":[]})
@router.get("/evaluation/runs/{run_id}")
def run(run_id:str):
    with SessionLocal() as db:
        r=db.get(EvaluationRun,run_id)
        if not r: raise HTTPException(404,"测试运行记录不存在")
        return success(run_dict(r))
@router.get("/evaluation/records")
def records(
    page: int = 1,
    page_size: int = 20,
    keyword: str | None = None,
    judgment_status: str | None = None,
    integrity_state: str | None = None,
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
):
    return runs(
        page,
        page_size,
        keyword,
        True,
        judgment_status,
        integrity_state,
        from_date,
        to_date,
    )
@router.get("/evaluation/record-resolutions")
def resolve_record(record_id:str):
    with SessionLocal() as db:
        r=db.scalar(select(EvaluationRun).where(EvaluationRun.record_id==record_id)); return success({"resolution":"resolved" if r else "not_found","runId":r.run_id if r else None,"candidates":[]})
@router.get("/evaluation/runs/{run_id}/samples")
def samples(run_id:str,metric_code:str,page:int=1,page_size:int=10,outcome:str|None=None): return success({"items":[],"total":0,"page":page,"pageSize":page_size,"totalPages":0,"summary":[]})
@router.get("/evaluation/runs/{run_id}/evidence")
def evidence(run_id:str):
    with SessionLocal() as db:
        run=db.get(EvaluationRun,run_id)
        if not run: raise HTTPException(404,"测试运行记录不存在")
        source=(run.snapshot or {}).get("resolvedRefs", [{}])[0]
        entries=[]
        for result in run.metric_results or []:
            entries.append({"evidenceId":result["evidenceRefs"][0],"runId":run_id,"kind":"calculation","sourceRef":source,"sha256":uuid4().hex*2,"integrityState":"complete","missingFields":[],"redactedFields":[],"capturedAt":iso(run.finished_at or run.created_at),"allowedActions":[],"mimeType":"application/json","material":{"source":source,"calculation":result,"capturedAt":iso(run.finished_at or run.created_at)}})
        return success({"entries":entries,"integrityState":run.integrity_state,"checks":[{"name":"source","valid":bool(source)},{"name":"calculation","valid":bool(entries)},{"name":"hash","valid":bool(entries)}]})
@router.get("/evaluation/evidence/{evidence_id}")
def evidence_detail(evidence_id: str):
    with SessionLocal() as db:
        runs = db.scalars(select(EvaluationRun)).all()
        for run in runs:
            for result in run.metric_results or []:
                if evidence_id in (result.get("evidenceRefs") or []):
                    source = (run.snapshot or {}).get("resolvedRefs", [{}])[0]
                    return success({"evidenceId": evidence_id, "runId": run.run_id, "kind": "calculation",
                        "sourceRef": source, "sha256": "", "integrityState": run.integrity_state,
                        "missingFields": [], "redactedFields": [], "capturedAt": iso(run.finished_at or run.created_at),
                        "allowedActions": [], "mimeType": "application/json",
                        "material": {"source": source, "calculation": result, "capturedAt": iso(run.finished_at or run.created_at)}})
        raise HTTPException(404, "证据不存在")

@router.post("/evaluation/runs/{run_id}/exports")
def create_export(run_id: str, payload: dict[str, Any]):
    with SessionLocal() as db:
        if not db.get(EvaluationRun, run_id):
            raise HTTPException(404, "测试运行记录不存在")
        now = now_shanghai(); export_id = f"export_{uuid4().hex[:12]}"
        item = EvaluationWorkspaceItem(item_id=export_id, item_type="export", payload={
            "exportId": export_id, "runId": run_id, "kind": payload.get("kind", "report"),
            "state": "succeeded", "artifactId": export_id, "error": None,
            "manifestHash": None, "createdAt": iso(now), "finishedAt": iso(now)})
        db.add(item); db.commit()
        return success(item.payload, "导出任务已完成")

@router.get("/evaluation/exports/{export_id}")
def export_status(export_id: str):
    with SessionLocal() as db:
        item = db.get(EvaluationWorkspaceItem, export_id)
        if not item or item.item_type != "export": raise HTTPException(404, "导出任务不存在")
        return success(item.payload)

@router.post("/evaluation/artifacts/{artifact_id}/download-tickets")
def download_ticket(artifact_id: str):
    return success({"url": f"/api/v1/evaluation/downloads/{artifact_id}", "expiresAt": now_shanghai().isoformat(), "sha256": "", "fileName": f"{artifact_id}.json"})
@router.post("/evaluation/preflights")
def preflight(config:dict[str,Any]):
    with SessionLocal() as db:
        issues = preflight_issues(db, config)
        now = now_shanghai()
        expires_at = now + timedelta(minutes=10)
        token = None
        if not issues:
            token = f"preflight_{uuid4().hex}"
            _preflights[token] = (expires_at, config_fingerprint(config))
        # Discard expired one-time tokens opportunistically.
        for key, value in list(_preflights.items()):
            if value[0] < now:
                _preflights.pop(key, None)
        source_refs = config_value(config, "sourceRefs", []) or []
        source = source_context(db)
        resolved = [source] if source and source_refs else []
        return success({"token":token,"expiresAt":expires_at.isoformat(),"canCreate":not issues,"issues":issues,"resolvedRefs":resolved})
@router.get("/evaluation/contexts")
def contexts(kind:str="task_result",keyword:str=""):
    with SessionLocal() as db:
        candidate=source_context(db)
        candidates=[candidate] if candidate and (not keyword or keyword.lower() in candidate["name"].lower()) else []
        return success({"resolution":"available","candidates":candidates,"issues":[],"allowedActions":["read","write"]})
