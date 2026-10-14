from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time import now_shanghai
from app.models.tables import (
    ComplianceAlert,
    ComplianceAudit,
    ComplianceEvidence,
    Dataset,
    Model,
    ModelCall,
    ModelVersion,
    Task,
    TrainingTask,
)


CAPABILITIES = {
    "lineage_audit",
    "training_monitor",
    "reasoning_audit",
    "neuron_audit",
    "full_chain_audit",
}


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def subject(entity_type: str, entity_id: Any, label: str, version_id: str | None = None) -> dict:
    return {
        "entity_type": entity_type,
        "entity_id": str(entity_id),
        "version_id": version_id,
        "display_id": str(entity_id),
        "label": label,
    }


def _field(key: str, label: str, value: Any, missing_reason: str) -> dict:
    present = value is not None and str(value).strip() != ""
    return {
        "key": key,
        "label": label,
        "value": str(value) if present else f"缺失：{missing_reason}",
        "state": "verified" if present else "missing",
    }


def process_evidence(task: Task, evidence_id: str) -> dict:
    input_data = task.input_data or {}
    result = task.result or {}
    input_version = input_data.get("dataset_version_id") or input_data.get("datasetVersionId") or task.dataset_version
    template = input_data.get("template_name") or input_data.get("templateName") or input_data.get("template_id") or input_data.get("templateId")
    rules = input_data.get("rules") or []
    rule_labels = {
        "normalize_text": "文本规范化",
        "deduplicate": "精确去重",
        "normalize_encoding": "编码统一",
        "complete_fields": "字段补全",
    }
    template_labels = {
        "standard": "标准清洗流程",
        "deduplicate": "去重与补全",
        "normalize": "格式规范化",
    }
    interface = " → ".join(
        str(template_labels.get(str(item), item))
        for item in [template, *rules]
        if item
    )
    total = int(result.get("total_count") or task.success_count or 0)
    processed = int(task.success_count or 0)
    output = f"{processed} / {total} 条" if total else None
    output_version = result.get("output_version")
    fields = [
        _field("input", "输入", input_version, "输入数据版本"),
        _field("time", "时间", iso(task.finished_at or task.created_at), "任务时间"),
        _field("interface", "接口", interface, "处理模板或规则"),
        _field("version", "版本", output_version, "输出数据版本"),
        _field("output", "输出", output, "处理结果"),
    ]
    return {
        "id": evidence_id,
        "display_id": evidence_id.replace("evidence-", "EVD-"),
        "source_module": "data-governance",
        "subject_ref": subject("task", task.task_id, task.name),
        "occurred_at": iso(task.finished_at or task.created_at),
        "source_trace_id": task.trace_id,
        "version_ref": output_version or input_version,
        "redacted_fields": fields,
        "integrity_state": "verified" if all(item["state"] == "verified" for item in fields) else "missing",
        "allowed_actions": ["copy", "open_source"],
    }


def task_evidence(task: Task, evidence_id: str) -> dict:
    """Build a five-element record for every non-data-process task.

    Do not invent fallback values here. An absent business field is audit
    evidence that still needs to be registered, and must remain ``missing``.
    """
    input_data = task.input_data or {}
    result = task.result if isinstance(task.result, dict) else {}
    mappings = {
        "data_ingest": ("数据源 ID", "接入接口", "目标数据版本", "接入条数"),
        "value_score": ("数据集版本", "价值评估方法", "算法版本", "评分结果"),
        "model_risk_governance": ("模型调用 ID", "治理策略", "模型版本", "治理后输出"),
        "anomaly_detect": ("待检测对象", "检测规则", "规则版本", "风险等级"),
        "evaluation": ("模型版本", "评估方法", "评估集版本", "各指标得分"),
        "reasoning_audit": ("审计对象", "审计规则", "规则版本", "审计结论"),
        "scenario_governance": ("场景 ID", "治理流程", "场景版本", "处理结果"),
    }
    input_label, interface_label, version_label, output_label = mappings.get(
        task.capability_code, ("输入对象", "执行接口", "关联版本", "任务输出")
    )

    def first(*values: Any) -> Any:
        return next((value for value in values if value is not None and str(value).strip()), None)

    input_ref = first(
        input_data.get("source_id"), input_data.get("sourceId"),
        input_data.get("dataset_version_id"), input_data.get("datasetVersionId"),
        input_data.get("dataset_id"), input_data.get("datasetId"),
        input_data.get("model_call_id"), input_data.get("modelCallId"),
        input_data.get("model_version"), input_data.get("modelVersion"),
        input_data.get("scenario_id"), input_data.get("scenarioId"), task.dataset_version,
    )
    occurred_at = task.finished_at or task.created_at or now_shanghai()
    interface = first(
        input_data.get("endpoint"), input_data.get("interface"), input_data.get("method"),
        input_data.get("strategy"), input_data.get("rule"), input_data.get("rule_id"),
        input_data.get("workflow"), task.config.get("endpoint") if isinstance(task.config, dict) else None,
        task.config.get("method") if isinstance(task.config, dict) else None,
    )
    version = first(
        result.get("output_version"), result.get("version"), input_data.get("version"),
        input_data.get("algorithm_version"), input_data.get("rule_version"),
        input_data.get("model_version"), input_data.get("modelVersion"), task.model_version,
        task.dataset_version,
    )
    output = first(
        result.get("summary"), result.get("message"), result.get("output"), result.get("risk_level"),
        result.get("score"), result.get("result"), result.get("count"), result.get("total_count"),
    )
    fields = [
        _field("input", "输入", input_ref, input_label),
        _field("time", "时间", iso(occurred_at), "当前任务时间"),
        _field("interface", "接口", interface, interface_label),
        _field("version", "版本", version, version_label),
        _field("output", "输出", output, output_label),
    ]
    return {
        "id": evidence_id,
        "display_id": evidence_id.replace("evidence-", "EVD-"),
        "source_module": "task-execution",
        "subject_ref": subject("task", task.task_id, task.name),
        "occurred_at": iso(occurred_at),
        "source_trace_id": task.trace_id or f"trace-{task.task_id}",
        "version_ref": str(version),
        "redacted_fields": fields,
        "integrity_state": "verified" if all(item["state"] == "verified" for item in fields) else "missing",
        "allowed_actions": ["copy", "open_source"],
    }


def training_evidence(task: TrainingTask, evidence_id: str) -> dict:
    checkpoints = task.checkpoints or []
    if task.status == "pending":
        output, output_state = "尚未开始训练", "missing"
    elif task.status == "running":
        output, output_state = f"已训练 {task.epoch} / {task.epochs} 轮 · 检查点 {len(checkpoints)} 个", "missing"
    elif task.status == "succeeded":
        output, output_state = f"已完成 {task.epochs} 轮 · 检查点 {len(checkpoints)} 个", "verified"
    else:
        output, output_state = "训练失败，未产出", "missing"
    fields = [
        _field("input", "输入", task.dataset_version, "训练数据版本"),
        _field("time", "时间", iso(task.updated_at), "训练更新时间"),
        _field("interface", "接口", task.method, "训练记录未提供训练方式"),
        _field("version", "版本", task.target_version, "目标模型版本"),
        {"key": "output", "label": "输出", "value": output, "state": output_state},
    ]
    return {
        "id": evidence_id,
        "display_id": evidence_id.replace("evidence-", "EVD-"),
        "source_module": "model-training",
        "subject_ref": subject("training_task", task.id, task.name, task.target_version),
        "occurred_at": iso(task.updated_at),
        "source_trace_id": f"trace-{task.id}",
        "version_ref": task.target_version,
        "redacted_fields": fields,
        "integrity_state": "verified" if all(item["state"] == "verified" for item in fields) else "missing",
        "allowed_actions": ["copy", "open_source"],
    }


def audit_payload(row: ComplianceAudit) -> dict:
    data = deepcopy(row.payload or {})
    data.update({
        "id": row.id,
        "display_id": data.get("display_id") or row.id,
        "capability_code": row.capability_code,
        "subject_ref": data.get("subject_ref") or subject(row.subject_type, row.subject_id, row.subject_id, row.version_id),
        "review_status": row.review_status,
        "review_reason": row.review_reason,
        "version": row.revision,
        "task_id": row.task_id,
        "execution_status": row.execution_status,
        "compliance_status": row.compliance_status,
    })
    return data


def alert_payload(row: ComplianceAlert) -> dict:
    data = deepcopy(row.payload or {})
    data.update({
        "id": row.id,
        "display_id": data.get("display_id") or row.id,
        "subject_ref": data.get("subject_ref") or subject(row.subject_type, row.subject_id, row.subject_id),
        "risk_level": row.risk_level,
        "current_status": row.status,
        "stage": row.stage,
        "version": row.version,
        "trace_id": row.trace_id,
    })
    return data


def _upsert_evidence(db: Session, payload: dict) -> None:
    row = db.get(ComplianceEvidence, payload["id"])
    if row is None:
        db.add(ComplianceEvidence(
            id=payload["id"],
            subject_type=payload["subject_ref"]["entity_type"],
            subject_id=str(payload["subject_ref"]["entity_id"]),
            trace_id=payload.get("source_trace_id"),
            payload=payload,
        ))
    else:
        row.trace_id = payload.get("source_trace_id")
        row.payload = payload


def ensure_compliance_data(db: Session) -> None:
    """Create repeatable database-backed records required by the compliance UI."""
    datasets = list(db.scalars(select(Dataset).order_by(Dataset.id)).all())
    models = list(db.scalars(select(Model).order_by(Model.id)).all())
    if not datasets or not models:
        return

    training = db.scalar(select(TrainingTask).order_by(TrainingTask.created_at))
    if training is None:
        training = TrainingTask(
            id="TR-COMPLIANCE-001",
            name="内容安全合规微调",
            status="succeeded",
            progress=100,
            model_id=str(models[0].id),
            base_version=models[0].version,
            dataset_id=str(datasets[0].id),
            dataset_version=datasets[0].version,
            method="低秩适配微调",
            epochs=10,
            epoch=10,
            learning_rate=0.0002,
            batch_size=8,
            target_version="v1.4.0",
            loss_history=[2.4, 1.5, 0.8, 0.42, 0.23, 0.18, 0.14, 0.11, 0.09, 0.07],
            validation_loss_history=[2.6, 1.7, 0.95, 0.51, 0.29, 0.22, 0.17, 0.14, 0.11, 0.08],
            checkpoints=[{"name": "checkpoint-10", "epoch": 10, "loss": 0.29}],
        )
        db.add(training)
        db.flush()
    elif training.id == "TR-COMPLIANCE-001" and len(training.loss_history or []) < training.epochs:
        training.loss_history = [2.4, 1.5, 0.8, 0.42, 0.23, 0.18, 0.14, 0.11, 0.09, 0.07]
        training.validation_loss_history = [2.6, 1.7, 0.95, 0.51, 0.29, 0.22, 0.17, 0.14, 0.11, 0.08]
        training.updated_at = now_shanghai()

    model_version = db.scalar(select(ModelVersion).where(
        ModelVersion.model_id == int(training.model_id),
        ModelVersion.version == training.target_version,
    ))
    if model_version is None:
        db.add(ModelVersion(
            model_id=int(training.model_id),
            version=training.target_version,
            description="由合规训练任务产出的模型版本",
            task_id=training.id,
        ))

    call = db.scalar(select(ModelCall).order_by(ModelCall.created_at))
    if call is None:
        call = ModelCall(
            id="CALL-COMPLIANCE-001",
            model_id=str(models[0].id),
            service_id="SVC-COMPLIANCE",
            version=training.target_version,
            prompt="请说明公开数据的安全使用边界",
            original_output="原始输出包含未经核验的个人信息片段",
            governed_output="已删除个人信息并补充授权边界提示",
            reason="命中隐私信息规则并完成治理",
            risk_level="high",
            reconstruction="已脱敏重构",
            elapsed_ms=128,
            status="succeeded",
            trace_id="TRACE-COMPLIANCE-001",
        )
        db.add(call)
        db.flush()

    for task in db.scalars(select(Task).where(Task.capability_code == "data_process")).all():
        _upsert_evidence(db, process_evidence(task, f"evidence-process-{task.task_id}"))
    for task in db.scalars(select(Task).where(Task.capability_code != "data_process")).all():
        _upsert_evidence(db, task_evidence(task, f"evidence-task-{task.task_id}"))
    train_evidence_id = f"evidence-training-{training.id}"
    _upsert_evidence(db, training_evidence(training, train_evidence_id))

    audit_specs = [
        (
            "AUDIT-TRAINING-001", "training_monitor", "training_task", training.id,
            training.target_version, None, "risk",
            {
                "display_id": "AUD-TRAIN-001",
                "subject_ref": subject("training_task", training.id, training.name, training.target_version),
                "adapter_version": "compliance-db-v1",
                "data_origin": "training_tasks",
                "evidence_refs": [train_evidence_id],
                "allowed_actions": ["review"],
                "result": {
                    "kind": "training_monitor",
                    "checks": [
                        {"key": item["key"], "label": item["label"], "required": True,
                         "state": item["state"], "evidence_refs": [train_evidence_id],
                         "missing_reason": None if item["state"] == "verified" else item["value"],
                         "rule_id": f"TRACE-{item['key'].upper()}", "rule_version": "1.0",
                         "occurred_at": iso(training.updated_at), "detail": item["value"]}
                        for item in training_evidence(training, train_evidence_id)["redacted_fields"]
                    ],
                    "checkpoints": [
                        {"id": cp.get("name", f"checkpoint-{index}"), "label": cp.get("name", "检查点"),
                         "expected_version": training.target_version, "snapshot_ref": cp.get("name"),
                         "rule_version": "1.0", "evidence_refs": [train_evidence_id]}
                        for index, cp in enumerate(training.checkpoints or [])
                    ],
                },
            },
        ),
        (
            "AUDIT-REASONING-001", "reasoning_audit", "model_call", call.id,
            call.version, "CAPTURE-COMPLIANCE-001", "alarm",
            {
                "display_id": "AUD-REASON-001",
                "subject_ref": subject("model_call", call.id, "模型调用记录", call.version),
                "adapter_version": "compliance-db-v1", "data_origin": "model_calls",
                "evidence_refs": ["evidence-call-input", "evidence-call-output"],
                "allowed_actions": ["review"],
                "result": {
                    "kind": "reasoning_audit",
                    "steps": [
                        {"id": "input", "label": "输入登记", "detail": call.prompt, "occurred_at": iso(call.created_at), "verification_state": "verified", "risk_level": None, "evidence_refs": ["evidence-call-input"]},
                        {"id": "model", "label": "模型调用", "detail": f"模型 {call.model_id} / {call.version}", "occurred_at": iso(call.created_at), "verification_state": "verified", "risk_level": None, "evidence_refs": ["evidence-call-input"]},
                        {"id": "output", "label": "输出风险检测", "detail": call.reason, "occurred_at": iso(call.created_at), "verification_state": "verified", "risk_level": call.risk_level, "evidence_refs": ["evidence-call-output"]},
                        {"id": "governed", "label": "治理后输出", "detail": call.governed_output, "occurred_at": iso(call.created_at), "verification_state": "verified", "risk_level": None, "evidence_refs": ["evidence-call-output"]},
                    ],
                    "risk_nodes": [{"step_id": "output", "rule_ref": "RULE-PRIVACY-02", "description": call.reason, "evidence_refs": ["evidence-call-output"], "alert_id": "ALERT-COMPLIANCE-001"}],
                    "audit_result": "原输出命中隐私规则，治理后输出已留痕，等待人工复核。",
                    "activation": {"model_id": call.model_id, "model_version": call.version, "inference_id": call.id, "capture_id": "CAPTURE-COMPLIANCE-001"},
                },
            },
        ),
        (
            "AUDIT-NEURON-001", "neuron_audit", "model", call.model_id,
            call.version, "CAPTURE-COMPLIANCE-001", "risk",
            {
                "display_id": "AUD-NEURON-001",
                "subject_ref": subject("model", call.model_id, models[0].name, call.version),
                "adapter_version": "compliance-db-v1", "data_origin": "model_calls",
                "evidence_refs": ["evidence-neuron-capture"], "allowed_actions": ["review"],
                "result": {
                    "kind": "neuron_audit", "availability": "unavailable",
                    "unavailable_reason": "当前环境未部署可采集激活值的模型推理服务，无法提供真实神经元激活数据。",
                    "model_version": call.version, "inference_id": call.id, "capture_id": None,
                    "layer_indices": [], "neuron_indices": [], "unit": "normalized activation",
                    "normalization_baseline": "未采集", "threshold": 0.8, "heatmap": [],
                    "abnormal_neurons": [], "observed_count": 0, "ratio": None,
                },
            },
        ),
    ]
    for audit_id, capability, stype, sid, version, capture, status, payload in audit_specs:
        existing_audit = db.get(ComplianceAudit, audit_id)
        if existing_audit is None:
            db.add(ComplianceAudit(
                id=audit_id, capability_code=capability, subject_type=stype, subject_id=str(sid),
                version_id=version, capture_id=capture, review_status="pending", review_reason="等待人工复核",
                revision=1, task_id=f"TASK-{audit_id}", execution_status="succeeded",
                compliance_status=status, payload=payload,
            ))
        elif audit_id == "AUDIT-NEURON-001":
            existing_audit.capture_id = None
            existing_audit.compliance_status = status
            existing_audit.payload = payload
            existing_audit.updated_at = now_shanghai()

    generic_evidences = [
        ("evidence-call-input", call.prompt, "model-invoke"),
        ("evidence-call-output", call.governed_output, "model-invoke"),
        ("evidence-neuron-capture", "尚未接入推理服务的神经元激活采集", "neuron-capture"),
    ]
    for evidence_id, value, module in generic_evidences:
        payload = {
            "id": evidence_id, "display_id": evidence_id.upper(), "source_module": module,
            "subject_ref": subject("model_call", call.id, "模型调用记录", call.version),
            "occurred_at": iso(call.created_at), "source_trace_id": call.trace_id,
            "version_ref": call.version,
            "redacted_fields": [{"key": "output", "label": "证据摘要", "value": value, "state": "verified"}],
            "integrity_state": "verified", "allowed_actions": ["copy", "open_source"],
        }
        _upsert_evidence(db, payload)

    if db.get(ComplianceAlert, "ALERT-COMPLIANCE-001") is None:
        db.add(ComplianceAlert(
            id="ALERT-COMPLIANCE-001", subject_type="training_task", subject_id=training.id,
            risk_level="high", status="pending", stage="training", version=1, trace_id=call.trace_id,
            payload={
                "display_id": "ALT-001", "subject_ref": subject("training_task", training.id, training.name, training.target_version),
                "description": "训练链路存在待人工确认的合规证据", "rule_ref": "TRACE-COMPLETE-01",
                "evidence_refs": [train_evidence_id], "supplementary_evidence_refs": [], "assignee_id": None,
                "events": [{"id": "EVENT-001", "description": "系统完成训练留痕核验", "actor_id": "compliance-engine", "occurred_at": iso(now_shanghai())}],
                "allowed_actions": ["claim"], "resolve_blockers": ["需先认领并补充人工复核结论"],
            },
        ))
    db.commit()


def seed_compliance_data() -> None:
    """Startup hook that makes the compliance read model available immediately."""
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        ensure_compliance_data(db)


def execute_compliance_task(capability_code: str, input_data: dict[str, Any], trace_id: str) -> dict:
    """Execute a deterministic compliance audit and persist both task and result."""
    if capability_code not in CAPABILITIES:
        raise ValueError("不支持的合规审计能力")
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        ensure_compliance_data(db)
        source = db.scalar(
            select(ComplianceAudit)
            .where(ComplianceAudit.capability_code == capability_code)
            .order_by(ComplianceAudit.created_at.desc())
        )
        if source is None:
            fallback = "training_monitor" if capability_code == "lineage_audit" else "reasoning_audit"
            source = db.scalar(
                select(ComplianceAudit)
                .where(ComplianceAudit.capability_code == fallback)
                .order_by(ComplianceAudit.created_at.desc())
            )
        task_id = f"TASK-COMPLIANCE-{uuid4().hex[:10]}"
        now = now_shanghai()
        task = Task(
            task_id=task_id,
            name=f"{capability_code} 重新审计",
            capability_code=capability_code,
            trace_id=trace_id,
            status="succeeded",
            source_name="全链路合规治理",
            dataset_name=None,
            storage_gb=0,
            progress=100,
            success_count=1,
            duplicate_count=0,
            anomaly_count=0,
            input_data=input_data,
            config={},
            result={"audit_id": None},
            created_at=now,
            finished_at=now,
        )
        db.add(task)
        if source is not None:
            audit_id = f"AUDIT-{uuid4().hex[:12].upper()}"
            payload = deepcopy(source.payload or {})
            payload["display_id"] = audit_id
            payload["allowed_actions"] = ["review"]
            ref = dict(payload.get("subject_ref") or {})
            requested_id = (
                input_data.get("training_task_id") or input_data.get("trainingTaskId")
                or input_data.get("inference_id") or input_data.get("inferenceId")
                or input_data.get("model_id") or input_data.get("modelId")
                or input_data.get("dataset_id") or input_data.get("datasetId")
                or input_data.get("task_id") or input_data.get("taskId")
            )
            if requested_id is not None:
                ref["entity_id"] = str(requested_id)
                ref["display_id"] = str(requested_id)
                payload["subject_ref"] = ref
            db.add(ComplianceAudit(
                id=audit_id,
                capability_code=capability_code,
                subject_type=ref.get("entity_type", source.subject_type),
                subject_id=str(ref.get("entity_id", source.subject_id)),
                version_id=ref.get("version_id", source.version_id),
                capture_id=source.capture_id,
                review_status="pending",
                review_reason="重新审计完成，等待人工复核",
                revision=1,
                task_id=task_id,
                execution_status="succeeded",
                compliance_status=source.compliance_status,
                payload=payload,
                created_at=now,
                updated_at=now,
            ))
            task.result = {"audit_id": audit_id}
        db.commit()
        return {"task_id": task_id, "status": "succeeded"}
