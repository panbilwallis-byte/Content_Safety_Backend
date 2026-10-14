from __future__ import annotations

from datetime import datetime, timezone
from math import ceil
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.response import success
from app.core.time import now_shanghai
from app.models.tables import (
    ComplianceAlert,
    ComplianceAudit,
    ComplianceEvidence,
    ComplianceErrorComment,
    Dataset,
    Model,
    ModelCall,
    ModelVersion,
    Task,
    TrainingTask,
)
from app.services.compliance_service import alert_payload, audit_payload, ensure_compliance_data, subject
from uuid import uuid4


router = APIRouter()


def _page(items: list[dict], page: int, page_size: int) -> dict:
    total = len(items)
    return {
        "items": items[(page - 1) * page_size : page * page_size],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": ceil(total / page_size) if total else 0,
    }


def _comment_payload(row: ComplianceErrorComment) -> dict:
    return {
        "id": row.id,
        "error_id": row.error_id,
        "content": row.content,
        "author_id": row.author_id,
        "author_name": row.author_name,
        "author": row.author_name,
        "created_at": row.created_at.isoformat(),
    }


class ErrorCommentBody(BaseModel):
    content: str = Field(min_length=1, max_length=500)


@router.get("/compliance/errors/{error_id}/comments")
def error_comments(error_id: str, db: Session = Depends(get_db)):
    rows = db.scalars(
        select(ComplianceErrorComment)
        .where(ComplianceErrorComment.error_id == error_id)
        .order_by(ComplianceErrorComment.created_at.asc(), ComplianceErrorComment.id.asc())
    ).all()
    return success(data=[_comment_payload(row) for row in rows], message="错误评论查询成功")


@router.post("/compliance/errors/{error_id}/comments")
def add_error_comment(error_id: str, body: ErrorCommentBody, request: Request, db: Session = Depends(get_db)):
    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=422, detail="评论内容不能为空")
    request_id = request.headers.get("X-Request-Id")
    if request_id:
        existing = db.scalar(select(ComplianceErrorComment).where(ComplianceErrorComment.request_id == request_id))
        if existing is not None:
            if existing.error_id != error_id or existing.content != content:
                raise HTTPException(status_code=409, detail="请求标识已用于另一条评论")
            return success(data=_comment_payload(existing), message="错误评论已保存")
    author_id = request.headers.get("X-User-Id")
    row = ComplianceErrorComment(
        id=str(uuid4()), error_id=error_id, request_id=request_id, content=content,
        author_id=author_id, author_name=request.headers.get("X-User-Name") or author_id or "当前用户",
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return success(data=_comment_payload(row), message="错误评论已发布")


def _node(entity_type: str, entity_id: Any, version: str | None, label: str, *, node_id: str | None = None) -> dict:
    ref = subject(entity_type, entity_id, label, version)
    return {**ref, "id": node_id or f"{entity_type}:{entity_id}:{version or '-'}", "type": entity_type}


def _graph(db: Session) -> tuple[list[dict], list[dict]]:
    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    def add_node(node: dict) -> str:
        nodes[node["id"]] = node
        return node["id"]

    def add_edge(edge_id: str, source_id: str, target_id: str, relation: str, evidence_id: str,
                 missing_reason: str | None = None, training_task_id: str | None = None) -> None:
        edges.append({
            "id": edge_id,
            "from_id": source_id,
            "to_id": target_id,
            "relation": relation,
            "verification_state": "missing" if missing_reason else "verified",
            "evidence_refs": [evidence_id],
            "missing_reason": missing_reason,
            "training_task_id": training_task_id,
        })

    datasets = {str(row.id): row for row in db.scalars(select(Dataset)).all()}
    models = {str(row.id): row for row in db.scalars(select(Model)).all()}
    known_dataset_versions: set[tuple[str, str]] = set()
    for dataset in datasets.values():
        node_id = add_node(_node("dataset", dataset.id, dataset.version, dataset.name))
        known_dataset_versions.add((str(dataset.id), dataset.version))

    process_tasks = db.scalars(select(Task).where(Task.capability_code == "data_process")).all()
    for task in process_tasks:
        data = task.input_data or {}
        result = task.result or {}
        dataset_id = str(data.get("dataset_id") or data.get("datasetId") or "unknown")
        input_version = str(data.get("dataset_version_id") or data.get("datasetVersionId") or task.dataset_version or "unknown")
        dataset = datasets.get(dataset_id)
        input_node_id = f"dataset:{dataset_id}:{input_version}"
        if input_node_id not in nodes:
            add_node(_node("dataset", dataset_id, input_version, dataset.name if dataset else f"未登记数据集 {dataset_id}"))
        task_node_id = add_node(_node("task", task.task_id, None, task.name, node_id=f"task:{task.task_id}"))
        missing = None if dataset and (dataset_id, input_version) in known_dataset_versions else "输入数据版本未在资源库登记"
        add_edge(f"edge-process-input-{task.task_id}", input_node_id, task_node_id, "输入数据引用",
                 f"evidence-process-{task.task_id}", missing)
        output_version = result.get("output_version")
        if output_version:
            output_node_id = add_node(_node("dataset", dataset_id, str(output_version), dataset.name if dataset else f"未登记数据集 {dataset_id}"))
            known_dataset_versions.add((dataset_id, str(output_version)))
            add_edge(f"edge-process-output-{task.task_id}", task_node_id, output_node_id, "输出版本登记",
                     f"evidence-process-{task.task_id}")

    training_tasks = db.scalars(select(TrainingTask)).all()
    for task in training_tasks:
        dataset_id = str(task.dataset_id)
        input_node_id = f"dataset:{dataset_id}:{task.dataset_version}"
        dataset = datasets.get(dataset_id)
        if input_node_id not in nodes:
            add_node(_node("dataset", dataset_id, task.dataset_version, dataset.name if dataset else f"未登记数据集 {dataset_id}"))
        task_node_id = add_node(_node("training_task", task.id, task.target_version, task.name, node_id=f"training_task:{task.id}"))
        missing = None if (dataset_id, task.dataset_version) in known_dataset_versions else "训练绑定的数据版本未登记"
        add_edge(f"edge-training-input-{task.id}", input_node_id, task_node_id, "训练数据绑定",
                 f"evidence-training-{task.id}", missing, task.id)

    versions = db.scalars(select(ModelVersion)).all()
    for version in versions:
        model = models.get(str(version.model_id))
        model_node_id = add_node(_node("model", version.model_id, version.version,
                                      model.name if model else f"未登记模型 {version.model_id}"))
        if version.task_id:
            source_id = f"training_task:{version.task_id}"
            if source_id not in nodes:
                add_node(_node("training_task", version.task_id, version.version,
                               f"未登记训练任务 {version.task_id}", node_id=source_id))
            exists = any(task.id == version.task_id for task in training_tasks)
            add_edge(f"edge-training-output-{version.id}", source_id, model_node_id, "训练产物登记",
                     f"evidence-training-{version.task_id}", None if exists else "来源训练任务未登记", version.task_id)

    for model in models.values():
        add_node(_node("model", model.id, model.version, model.name))
    return list(nodes.values()), edges


def _issue_summary(db: Session) -> dict:
    """Aggregate cross-domain compliance gaps without client-side N+1 lineage queries."""
    ensure_compliance_data(db)
    nodes, edges = _graph(db)
    labels = {node["id"]: node["label"] for node in nodes}
    errors: list[dict] = []
    counted_relations = {"输入数据引用", "训练数据绑定", "训练产物登记"}
    link_expected = 0
    link_missing = 0
    for edge in edges:
        if edge["relation"] not in counted_relations:
            continue
        link_expected += 1
        if edge["verification_state"] != "missing":
            continue
        link_missing += 1
        errors.append({
            "id": f"link:{edge['relation']}:{edge['id']}", "source": "system", "layer": "link",
            "title": f"{labels.get(edge['from_id'], edge['from_id'])} -> {labels.get(edge['to_id'], edge['to_id'])}",
            "detail": edge["missing_reason"] or "引用未登记", "state": "open",
            "target": "/compliance/lineage", "comment_count": 0,
        })

    models = list(db.scalars(select(Model)).all())
    versions = list(db.scalars(select(ModelVersion)).all())
    registered = {(str(model.id), model.version) for model in models}
    registered.update((str(version.model_id), version.version) for version in versions)
    calls = list(db.scalars(select(ModelCall)).all())
    link_expected += len(calls)
    for call in calls:
        if (str(call.model_id), call.version) in registered:
            continue
        link_missing += 1
        errors.append({
            "id": f"link:模型版本引用:{call.id}", "source": "system", "layer": "link",
            "title": f"调用记录 {call.id}", "detail": f"引用的模型版本 {call.version} 未在登记表里",
            "state": "open", "target": "/compliance/lineage", "comment_count": 0,
        })

    model_expected = 0
    model_missing = 0
    for task in db.scalars(select(TrainingTask)).all():
        interval = max(1, (task.epochs or 1) // 3)
        saved = {item.get("epoch") for item in (task.checkpoints or [])}
        for epoch in range(1, (task.epoch or 0) + 1):
            if epoch != task.epoch and epoch % interval:
                continue
            model_expected += 1
            if epoch in saved:
                continue
            model_missing += 1
            errors.append({
                "id": f"model:{task.id}:checkpoint:{epoch}", "source": "system", "layer": "model",
                "title": f"训练任务 {task.id}", "detail": f"应有第 {epoch} 轮的检查点，实际未保存",
                "state": "open", "target": "/compliance/model-internal", "comment_count": 0,
            })

    tasks = list(db.scalars(select(Task)).all())
    failed_tasks = [task for task in tasks if task.status == "failed"]
    for task in failed_tasks:
        result = task.result or {}
        errors.append({
            "id": f"task:{task.task_id}:failed", "source": "system", "layer": "task",
            "title": f"{task.name}（{task.task_id}）", "detail": result.get("error_message") or "任务执行失败",
            "state": "open", "target": "/compliance/full-chain", "comment_count": 0,
        })

    return {
        "gaps": {
            "link": {"expected": link_expected, "missing": link_missing},
            "task": {"expected": len(tasks), "missing": len(failed_tasks)},
            "model": {"expected": model_expected, "missing": model_missing},
        },
        "errors": errors,
    }


@router.get("/compliance/issue-summary")
def issue_summary(db: Session = Depends(get_db)):
    return success(data=_issue_summary(db), message="合规问题汇总查询成功")

@router.get("/compliance/overview")
def overview(from_: str = Query(alias="from"), to: str = Query(), scope: str = Query(default="all"), db: Session = Depends(get_db)):
    ensure_compliance_data(db)
    audits = list(db.scalars(select(ComplianceAudit)).all())
    evidences = list(db.scalars(select(ComplianceEvidence)).all())
    alerts = list(db.scalars(select(ComplianceAlert)).all())
    missing = sum(1 for item in evidences if (item.payload or {}).get("integrity_state") == "missing")
    handoff_specs = [
        ("lineage", "数据与模型谱系", "lineage"),
        ("training", "训练过程留痕", "training-monitor"),
        ("inference", "推理过程审计", "reasoning-audit"),
        ("neuron", "神经元激活审计", "neuron-audit"),
        ("alert", "风险告警处置", "risk-alert"),
        ("trace", "全链路追踪", "full-chain"),
    ]
    data = {
        "as_of": now_shanghai().isoformat(), "scope": scope,
        "scope_description": "基于数据库已登记的数据治理、训练、模型调用和证据记录统计",
        "expected_count": len(evidences), "missing_count": missing,
        "handoffs": [
            {"kind": kind, "label": label, "expected_count": len(evidences),
             "verified_count": len(evidences) - missing, "missing_count": missing,
             "unavailable_count": 0, "missing_reason": "存在待补证记录" if missing else None,
             "target": target, "subject_ref": None}
            for kind, label, target in handoff_specs
        ],
        "pending_reviews_count": sum(item.review_status == "pending" for item in audits),
        "completed_audits_count": sum(item.execution_status == "succeeded" for item in audits),
    }
    return success(data=data, message="合规总览查询成功")


@router.get("/compliance/contexts")
def contexts(source_kind: str, source_id: str | None = None, trace_id: str | None = None,
             db: Session = Depends(get_db)):
    ensure_compliance_data(db)
    candidates: list[dict] = []
    if source_kind == "dataset":
        nodes, _ = _graph(db)
        for node in nodes:
            if node["type"] != "dataset":
                continue
            candidates.append({"source_kind": source_kind, "source_id": node["entity_id"],
                               "subject_ref": {key: node[key] for key in ("entity_type", "entity_id", "version_id", "display_id", "label")},
                               "model_version": node["version_id"], "capture_id": None,
                               "label": f"{node['label']} / {node['version_id']} · 数据版本"})
    elif source_kind == "model":
        models = {str(item.id): item for item in db.scalars(select(Model)).all()}
        versions = list(db.scalars(select(ModelVersion)).all())
        for model in models.values():
            all_versions = [model.version, *[item.version for item in versions if item.model_id == model.id]]
            for version in dict.fromkeys(all_versions):
                call = db.scalar(select(ModelCall).where(ModelCall.model_id == str(model.id), ModelCall.version == version))
                candidates.append({"source_kind": source_kind, "source_id": str(model.id),
                                   "subject_ref": subject("model", model.id, model.name, version),
                                   "model_version": version,
                                   "capture_id": "CAPTURE-COMPLIANCE-001" if call else None,
                                   "label": f"{model.name} / {version} · {'已采集激活' if call else '未采集激活'}"})
    elif source_kind == "training_task":
        for item in db.scalars(select(TrainingTask)).all():
            candidates.append({"source_kind": source_kind, "source_id": item.id,
                               "subject_ref": subject("training_task", item.id, item.name, item.target_version),
                               "model_version": item.id, "capture_id": None, "label": f"{item.name} · {item.id}"})
    elif source_kind in {"model_call", "trace"}:
        for item in db.scalars(select(ModelCall)).all():
            candidates.append({"source_kind": source_kind, "source_id": item.id if source_kind == "model_call" else item.trace_id,
                               "subject_ref": subject("model_call", item.id, "模型调用记录", item.version),
                               "model_version": item.version, "capture_id": "CAPTURE-COMPLIANCE-001",
                               "label": f"{item.id} · {item.trace_id or '未记录 Trace'}"})

    selected = [item for item in candidates if source_id is None or str(item["source_id"]) == str(source_id)]
    empty = {"subject_ref": None, "task_id": None, "inference_id": None, "trace_id": None,
             "model_id": None, "model_version": None, "capture_id": None, "candidates": selected or candidates}
    if source_id is None:
        return success(data={"resolution": "ambiguous" if candidates else "not_found", **empty})
    if not selected:
        return success(data={"resolution": "not_found", **empty, "candidates": []})
    if len(selected) > 1:
        return success(data={"resolution": "ambiguous", **empty, "candidates": selected})
    item = selected[0]
    call = db.get(ModelCall, str(item["subject_ref"]["entity_id"])) if item["subject_ref"]["entity_type"] == "model_call" else None
    if trace_id and call and call.trace_id != trace_id:
        return success(data={"resolution": "conflict", **empty, "candidates": selected})
    training = db.get(TrainingTask, str(item["source_id"])) if source_kind == "training_task" else None
    return success(data={
        "resolution": "resolved", "subject_ref": item["subject_ref"],
        "task_id": training.id if training else (call.id if call else None),
        "inference_id": call.id if call else None, "trace_id": call.trace_id if call else (f"trace-{training.id}" if training else None),
        "model_id": call.model_id if call else (training.model_id if training else (item["source_id"] if source_kind == "model" else None)),
        "model_version": item["model_version"], "capture_id": item["capture_id"], "candidates": selected,
    })


@router.get("/compliance/lineage")
def lineage(entity_type: str, entity_id: str, version_id: str | None = None,
            direction: str = Query(default="both", pattern="^(upstream|downstream|both)$"),
            db: Session = Depends(get_db)):
    ensure_compliance_data(db)
    nodes, edges = _graph(db)
    roots = [node for node in nodes if node["entity_type"] == entity_type and str(node["entity_id"]) == str(entity_id)
             and (not version_id or node["version_id"] == version_id)]
    if not roots:
        raise HTTPException(status_code=404, detail="未找到谱系起点")
    ids = {node["id"] for node in roots}
    changed = True
    while changed:
        changed = False
        for edge in edges:
            follow_up = direction != "downstream" and edge["to_id"] in ids
            follow_down = direction != "upstream" and edge["from_id"] in ids
            if (follow_up or follow_down) and ({edge["from_id"], edge["to_id"]} - ids):
                ids.update((edge["from_id"], edge["to_id"]))
                changed = True
    selected_edges = [edge for edge in edges if edge["from_id"] in ids and edge["to_id"] in ids]
    gaps = [{"reason": edge["missing_reason"] or "谱系关系缺少核验依据", "evidence_refs": edge["evidence_refs"], "alert_id": None}
            for edge in selected_edges if edge["verification_state"] == "missing"]
    return success(data={"nodes": [node for node in nodes if node["id"] in ids], "edges": selected_edges, "gaps": gaps}, message="谱系查询成功")


@router.get("/compliance/audits")
def audits(capability_code: str | None = None, subject_type: str | None = None,
           subject_id: str | None = None, version_id: str | None = None, capture_id: str | None = None,
           review_status: str | None = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
           db: Session = Depends(get_db)):
    ensure_compliance_data(db)
    rows = list(db.scalars(select(ComplianceAudit).order_by(ComplianceAudit.created_at.desc())).all())
    rows = [row for row in rows if (not capability_code or row.capability_code == capability_code)
            and (not subject_type or row.subject_type == subject_type)
            and (subject_id is None or row.subject_id == str(subject_id))
            and (not version_id or row.version_id == version_id)
            and (not capture_id or row.capture_id == capture_id)
            and (not review_status or row.review_status == review_status)]
    summaries = []
    for row in rows:
        detail = audit_payload(row)
        summaries.append({key: detail[key] for key in ("id", "display_id", "capability_code", "subject_ref", "review_status", "review_reason", "version")})
    return success(data=_page(summaries, page, page_size), message="审计记录查询成功")


@router.get("/compliance/audits/{audit_id}")
def audit_detail(audit_id: str, db: Session = Depends(get_db)):
    ensure_compliance_data(db)
    row = db.get(ComplianceAudit, audit_id)
    if row is None:
        raise HTTPException(status_code=404, detail="审计记录不存在")
    return success(data=audit_payload(row), message="审计详情查询成功")


@router.get("/alerts/{alert_id}")
def alert_detail(alert_id: str, db: Session = Depends(get_db)):
    ensure_compliance_data(db)
    row = db.get(ComplianceAlert, alert_id)
    if row is None:
        raise HTTPException(status_code=404, detail="告警不存在")
    return success(data=alert_payload(row), message="告警详情查询成功")


@router.get("/compliance/evidence/{evidence_id}")
def evidence_detail(evidence_id: str, db: Session = Depends(get_db)):
    ensure_compliance_data(db)
    row = db.get(ComplianceEvidence, evidence_id)
    if row is None:
        raise HTTPException(status_code=404, detail="证据不存在")
    return success(data=row.payload, message="证据详情查询成功")


@router.get("/compliance/traces/{trace_id}")
def trace_detail(trace_id: str, include_provenance: bool = True, db: Session = Depends(get_db)):
    ensure_compliance_data(db)
    call = db.scalar(select(ModelCall).where(ModelCall.trace_id == trace_id))
    if call is None:
        raise HTTPException(status_code=404, detail="Trace 不存在")
    training = db.scalar(select(TrainingTask).where(TrainingTask.model_id == call.model_id, TrainingTask.target_version == call.version))
    records: list[dict] = []
    if include_provenance and training:
        records.append({"stage": "训练过程", "record_scope": "provenance", "subject_ref": subject("training_task", training.id, training.name, training.target_version), "source_trace_id": f"trace-{training.id}", "occurred_at": training.updated_at.isoformat(), "evidence_refs": [f"evidence-training-{training.id}"], "verification_state": "verified" if training.status == "succeeded" else "missing"})
    records.extend([
        {"stage": "模型推理", "record_scope": "current", "subject_ref": subject("model_call", call.id, "模型调用", call.version), "source_trace_id": trace_id, "occurred_at": call.created_at.isoformat(), "evidence_refs": ["evidence-call-input"], "verification_state": "verified"},
        {"stage": "内容输出", "record_scope": "current", "subject_ref": subject("output", f"OUT-{call.id}", "治理后输出", call.version), "source_trace_id": trace_id, "occurred_at": call.created_at.isoformat(), "evidence_refs": ["evidence-call-output"], "verification_state": "verified"},
    ])
    evidence_rows = [db.get(ComplianceEvidence, ref) for record in records for ref in record["evidence_refs"]]
    fields = [field for row in evidence_rows if row for field in (row.payload or {}).get("redacted_fields", [])]
    keys = ["input", "time", "interface", "version", "output"]
    checks = []
    for key, label in zip(keys, ["输入", "时间", "接口", "版本", "输出"]):
        matches = [item for item in fields if item.get("key") == key]
        state = "missing" if any(item.get("state") == "missing" for item in matches) else ("verified" if matches else "unknown")
        checks.append({"key": key, "label": label, "required": True, "state": state,
                       "evidence_refs": [row.id for row in evidence_rows if row],
                       "missing_reason": f"{label}留痕缺失" if state == "missing" else None})
    gaps = [{"reason": check["missing_reason"], "evidence_refs": check["evidence_refs"], "alert_id": "ALERT-COMPLIANCE-001"}
            for check in checks if check["state"] == "missing"]
    return success(data={"current_trace_id": trace_id, "records": records, "checks": checks, "gaps": gaps,
                         "audit_ref": "AUDIT-REASONING-001", "conclusion": "链路存在待补证项" if gaps else "链路留痕完整",
                         "compliance_status": "alarm" if gaps else "normal"}, message="全链路追踪查询成功")


class AlertAction(BaseModel):
    expected_version: int
    evidence_refs: list[str] = Field(default_factory=list)
    reason: str | None = None
    review_conclusion: str | None = None


@router.post("/alerts/{alert_id}/{action}")
def act_on_alert(alert_id: str, action: str, body: AlertAction, request: Request, db: Session = Depends(get_db)):
    if action not in {"claim", "evidence", "resolve"}:
        raise HTTPException(status_code=404, detail="不支持的告警操作")
    row = db.get(ComplianceAlert, alert_id)
    if row is None:
        raise HTTPException(status_code=404, detail="告警不存在")
    if row.version != body.expected_version:
        raise HTTPException(status_code=409, detail="告警版本已变化，请刷新")
    payload = dict(row.payload or {})
    allowed = payload.get("allowed_actions", [])
    if action not in allowed:
        raise HTTPException(status_code=409, detail="当前状态不允许该操作")
    actor = request.headers.get("X-User-Id", "current-user")
    if action == "claim":
        row.status = "processing"; payload["assignee_id"] = actor; payload["allowed_actions"] = ["evidence", "resolve"]
    elif action == "evidence":
        if not body.evidence_refs:
            raise HTTPException(status_code=422, detail="补证操作至少需要一个证据引用")
        payload["supplementary_evidence_refs"] = list(dict.fromkeys([*payload.get("supplementary_evidence_refs", []), *body.evidence_refs]))
        payload["resolve_blockers"] = [] if body.review_conclusion else ["缺少复核结论"]
    else:
        if payload.get("resolve_blockers"):
            raise HTTPException(status_code=409, detail="仍存在未解决的处置阻塞项")
        row.status = "resolved"; payload["allowed_actions"] = []
    payload.setdefault("events", []).append({"id": f"EVENT-{row.version + 1}", "description": body.reason or action,
                                              "actor_id": actor, "occurred_at": now_shanghai().isoformat()})
    row.version += 1; row.updated_at = now_shanghai(); row.payload = payload
    db.commit(); db.refresh(row)
    return success(data=alert_payload(row), message="告警状态已更新")


class ReviewAction(BaseModel):
    conclusion: str = Field(pattern="^(confirmed|rejected|needs_evidence)$")
    reason: str = Field(min_length=1)
    evidence_refs: list[str] = Field(default_factory=list)
    expected_version: int


@router.post("/compliance/audits/{audit_id}/reviews")
def review_audit(audit_id: str, body: ReviewAction, db: Session = Depends(get_db)):
    row = db.get(ComplianceAudit, audit_id)
    if row is None:
        raise HTTPException(status_code=404, detail="审计记录不存在")
    if row.revision != body.expected_version:
        raise HTTPException(status_code=409, detail="审计版本已变化，请刷新")
    row.review_status = body.conclusion; row.review_reason = body.reason; row.revision += 1; row.updated_at = now_shanghai()
    payload = dict(row.payload or {})
    payload["evidence_refs"] = list(dict.fromkeys([*payload.get("evidence_refs", []), *body.evidence_refs]))
    payload["allowed_actions"] = []
    row.payload = payload
    db.commit(); db.refresh(row)
    return success(data=audit_payload(row), message="人工复核已保存")


@router.get("/alerts")
def alerts(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
           risk_level: str | None = None, status: str | None = None, stage: str | None = None,
           subject_type: str | None = None, subject_id: str | None = None, db: Session = Depends(get_db)):
    ensure_compliance_data(db)
    rows = list(db.scalars(select(ComplianceAlert).order_by(ComplianceAlert.created_at.desc())).all())
    statuses = set(status.split(",")) if status else set()
    rows = [row for row in rows if (not risk_level or row.risk_level == risk_level)
            and (not statuses or row.status in statuses) and (not stage or row.stage == stage)
            and (not subject_type or row.subject_type == subject_type)
            and (subject_id is None or row.subject_id == str(subject_id))]
    summaries = []
    for row in rows:
        detail = alert_payload(row)
        summaries.append({key: detail[key] for key in ("id", "display_id", "subject_ref", "description", "risk_level", "current_status", "stage")})
    return success(data=_page(summaries, page, page_size), message="风险告警查询成功")
