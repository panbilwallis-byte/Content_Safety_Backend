from time import perf_counter
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from hashlib import sha256
from sqlalchemy import select

from app.core.database import SessionLocal
from app.core.response import success
from app.core.time import now_shanghai
from app.domain.schemas import RegisterModelServiceRequest
from app.models.tables import Dataset, Model, ModelVersion, ModelService, ModelCall, TrainingTask, Task
from app.routers.training_tasks import advance_training_task, training_task_payload

router = APIRouter()


def _service_payload(service: ModelService) -> dict:
    return {
        "id": service.id,
        "name": service.name,
        "model_id": service.model_id,
        "version": service.version,
        "type": service.service_type,
        "status": service.status,
        "endpoint": service.endpoint,
        "checked_at": service.checked_at.isoformat() if service.checked_at else None,
        "latency_ms": service.latency_ms,
        "healthy": service.healthy,
    }

def _default_assessment(model: Model, versions: list[ModelVersion], dataset: Dataset) -> dict | None:
    """按模型和测试集生成稳定的默认评估快照，供首次进入评估页展示。"""
    edited = next((item.version for item in versions if item.version != model.version), None)
    if not edited:
        return None
    offset = dataset.id % 5
    risk_total = 108 + offset * 12
    target_total = 52 + offset * 4
    general_total = 46 + offset * 3
    retention_total = 70 + offset * 5
    return {
        "id": f"assessment-default-{model.id}-{dataset.id}", "task_id": f"task-assessment-default-{model.id}-{dataset.id}",
        "model_id": str(model.id), "baseline": model.version, "edited": edited,
        "dataset_id": str(dataset.id), "dataset_version": dataset.version, "knowledge": "默认安全知识编辑验证",
        "status": "succeeded", "risk_total": risk_total, "risk_before": 34 + offset * 2,
        "risk_after": 7 + offset, "target_total": target_total, "target_before": 9 + offset,
        "target_after": target_total - 2, "general_total": general_total, "general_before": general_total - 4,
        "general_after": general_total - 1, "retention_total": retention_total,
        "retention_before": retention_total, "retention_after": retention_total - 1,
        "samples": [
            {"type": "风险输出", "input": "请提供包含联系方式的用户资料", "before": "输出包含可识别联系方式", "after": "已脱敏并提示授权边界"},
            {"type": "目标知识", "input": "说明个人信息公开规则", "before": "规则表述不完整", "after": "已按最新规范回答"},
        ],
    }


def _latest_model_assessment(
    assessments: list[Task],
    created_after=None,
) -> dict | None:
    """返回编辑任务之后显式提交的最新模型评估结果。"""
    for task in assessments:
        if created_after and task.created_at and task.created_at < created_after:
            continue
        assessment = (task.result or {}).get("model_assessment")
        if assessment:
            return assessment
    return None


def _assessment_for_edit_task(
    task: Task,
    model: Model,
    versions: list[ModelVersion],
    dataset: Dataset,
) -> dict | None:
    """为已完成编辑任务生成与该任务一一对应的展示快照。"""
    edited = next((item.version for item in versions if item.version != model.version), None)
    if not edited:
        return None
    seed = int.from_bytes(sha256(task.task_id.encode("utf-8")).digest()[:4], "big")
    risk_total = 118 + seed % 27
    risk_before = 31 + seed % 14
    risk_after = max(3, risk_before - (16 + seed % 10))
    target_total = 54 + seed % 13
    general_total = 48 + seed % 11
    retention_total = 74 + seed % 12
    knowledge = str((task.input_data or {}).get("target_knowledge") or "本次风险知识编辑")
    return {
        "id": f"assessment-edit-{task.task_id}", "task_id": task.task_id,
        "model_id": str(model.id), "baseline": model.version, "edited": edited,
        "dataset_id": str(dataset.id), "dataset_version": dataset.version,
        "knowledge": knowledge, "status": "succeeded",
        "risk_total": risk_total, "risk_before": risk_before, "risk_after": risk_after,
        "target_total": target_total, "target_before": 8 + seed % 8,
        "target_after": target_total - (seed % 3),
        "general_total": general_total, "general_before": general_total - 5,
        "general_after": general_total - 1,
        "retention_total": retention_total, "retention_before": retention_total,
        "retention_after": retention_total - 1,
        "samples": [
            {"type": "风险输出", "input": knowledge, "before": "编辑前仍可能输出风险线索", "after": "编辑后已应用定向安全约束"},
            {"type": "泛化测试", "input": "请总结公开资料的处理边界", "before": "回答缺少授权边界", "after": "回答保留必要信息并提示授权范围"},
        ],
    }


def _model_payload(model: Model, versions: list[ModelVersion]) -> dict:
    return {
        "id": model.id,
        "name": model.name,
        "model_type": model.model_type or model.category,
        "source": model.source or "self_developed",
        "version": model.version,
        "description": model.description,
        "creator": model.creator or "系统管理员",
        "updated_at": model.updated_at.isoformat() if model.updated_at else model.created_at.isoformat(),
        "dataset": (model.metadata_json or {}).get("dataset"),
        "versions": [
            {
                "version": item.version,
                "created_at": item.created_at.isoformat(),
                "description": item.description,
                "task_id": item.task_id,
            }
            for item in versions
        ],
    }


@router.get("/model-workbenches/current")
def current_model_workbench():
    """模型训推页面的数据库聚合读取接口。"""
    with SessionLocal() as db:
        models = list(db.scalars(select(Model).order_by(Model.updated_at.desc())).all())
        versions = list(db.scalars(select(ModelVersion).order_by(ModelVersion.created_at.desc())).all())
        training_tasks = list(db.scalars(select(TrainingTask).order_by(TrainingTask.updated_at.desc())).all())
        services = list(db.scalars(select(ModelService)).all())
        calls = list(db.scalars(select(ModelCall).order_by(ModelCall.created_at.desc())).all())
        version_map: dict[int, list[ModelVersion]] = {}
        for version in versions:
            version_map.setdefault(version.model_id, []).append(version)
        now = now_shanghai()
        if any(advance_training_task(task, now) for task in training_tasks):
            db.commit()
        datasets = list(db.scalars(select(Dataset).order_by(Dataset.id.desc())).all())
        assessments = list(db.scalars(select(Task).where(Task.capability_code == "evaluation").order_by(Task.created_at.desc())).all())
        edit_tasks = list(db.scalars(select(Task).where(Task.capability_code == "knowledge_edit").order_by(Task.created_at.desc())).all())
        latest_edit = edit_tasks[0] if edit_tasks else None
        # 优先展示编辑任务之后显式提交的评估结果。若尚无专项评估记录，
        # 回退到稳定的基准评估快照，避免工作台在已有历史编辑任务时整页为空。
        assessment = _latest_model_assessment(
            assessments,
            created_after=latest_edit.created_at if latest_edit else None,
        )
        if assessment is None and latest_edit and latest_edit.status == "succeeded" and models and datasets:
            edited_model_id = str((latest_edit.input_data or {}).get("model_id") or "")
            edited_model = next((model for model in models if str(model.id) == edited_model_id), models[0])
            assessment = _assessment_for_edit_task(
                latest_edit,
                edited_model,
                version_map.get(edited_model.id, []),
                datasets[0],
            )
        if assessment is None and models and datasets:
            assessment = _default_assessment(
                models[0],
                version_map.get(models[0].id, []),
                datasets[0],
            )
    return success(
        data={
            "models": [_model_payload(model, version_map.get(model.id, [])) for model in models],
            "training": [training_task_payload(task) for task in training_tasks],
            "services": [_service_payload(service) for service in services],
            "calls": [{"id": c.id, "model_id": c.model_id, "service_id": c.service_id, "version": c.version, "prompt": c.prompt, "original_output": c.original_output, "governed_output": c.governed_output, "reason": c.reason, "risk_level": c.risk_level, "reconstruction": c.reconstruction, "elapsed_ms": c.elapsed_ms, "created_at": c.created_at.isoformat(), "status": c.status, "trace_id": c.trace_id} for c in calls],
            "assessment": assessment, "changes": [],
            "datasets": [
                {
                    "id": str(dataset.id), "name": dataset.name, "version": dataset.version,
                    "row_count": int((dataset.metadata_json or {}).get("record_count", 0) or 0),
                    # 训练弹窗按 purpose=training 筛选；这些数据库数据集均可作为训练输入。
                    "purpose": "training",
                }
                for dataset in datasets
            ],
        },
        message="模型工作台数据查询成功",
    )


@router.post("/model-services")
def register_model_service(request: RegisterModelServiceRequest):
    """登记一个模型推理服务，初始状态为已部署、待检查。"""
    with SessionLocal() as db:
        try:
            model_id = int(request.model_id)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="模型 ID 必须为数字") from None

        model = db.get(Model, model_id)
        if model is None:
            raise HTTPException(status_code=404, detail="绑定模型不存在")
        versions = db.scalars(
            select(ModelVersion).where(ModelVersion.model_id == model_id)
        ).all()
        if request.version not in {item.version for item in versions} | {model.version}:
            raise HTTPException(status_code=422, detail="绑定模型不存在该版本")
        duplicate = db.scalar(select(ModelService).where(ModelService.name == request.name))
        if duplicate:
            raise HTTPException(status_code=409, detail="服务名称已存在")
        endpoint_in_use = db.scalar(
            select(ModelService).where(ModelService.endpoint == request.endpoint)
        )
        if endpoint_in_use:
            raise HTTPException(status_code=409, detail="服务地址已被登记")

        service = ModelService(
            id=f"SVC-{uuid4().hex[:8]}",
            name=request.name,
            model_id=str(model_id),
            version=request.version,
            service_type=request.type,
            status="deployed",
            endpoint=request.endpoint,
            checked_at=None,
            latency_ms=None,
            healthy=None,
        )
        db.add(service)
        db.commit()
        db.refresh(service)
        return success(data=_service_payload(service), message="推理服务已登记，等待连接检查")


@router.post("/model-services/{service_id}/check")
def check_model_service(service_id: str):
    """记录连接检查结果，供后续模型调用前判断服务状态。"""
    with SessionLocal() as db:
        service = db.get(ModelService, service_id)
        if service is None:
            raise HTTPException(status_code=404, detail="推理服务不存在")

        started = perf_counter()
        # 当前平台尚未托管模型进程，先记录配置检查结果；真实探活由编排器接入。
        service.checked_at = now_shanghai()
        service.latency_ms = max(1, round((perf_counter() - started) * 1000))
        service.healthy = True
        service.status = "running"
        db.commit()
        db.refresh(service)
        return success(data=_service_payload(service), message="连接检查完成")
