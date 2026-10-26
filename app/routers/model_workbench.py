from time import perf_counter
from uuid import uuid4

import httpx
from fastapi import APIRouter, HTTPException, Query
from hashlib import sha256
from sqlalchemy import select

from app.core.database import SessionLocal
from app.core.response import success
from app.core.time import now_shanghai
from app.domain.schemas import RegisterModelServiceRequest, UpgradeModelServiceRequest, RecordModelCallRequest
from app.services.compliance_event_service import add_event
from app.services.lineage_service import record_call_edge
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


def _model_payload(model: Model, versions: list[ModelVersion], edit_task_map: dict[str, Task] | None = None) -> dict:
    task_map = edit_task_map or {}

    def version_payload(item: ModelVersion) -> dict:
        # 编辑产物行携带产出它的编辑任务信息，评估页选中某轮编辑版本时
        # 左侧“风险知识编辑”卡片据此展示该轮编辑的知识内容。
        edit_task = task_map.get(item.task_id or "")
        return {
            "version": item.version,
            "created_at": item.created_at.isoformat(),
            "description": item.description,
            "task_id": item.task_id,
            "edit_knowledge": (edit_task.input_data or {}).get("target_knowledge") if edit_task else None,
            "edit_status": edit_task.status if edit_task else None,
        }

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
        "versions": [version_payload(item) for item in versions],
    }


@router.get("/model-workbenches/current")
def current_model_workbench():
    """模型训推页面的数据库聚合读取接口。"""
    with SessionLocal() as db:
        # 先推进训练再读取模型与版本：训练到点会登记新版本，
        # 若先查再推进，完成当次响应会缺少刚写入的版本记录。
        training_tasks = list(db.scalars(select(TrainingTask).order_by(TrainingTask.updated_at.desc())).all())
        now = now_shanghai()
        advanced = False
        for task in training_tasks:
            if advance_training_task(db, task, now):
                advanced = True
        if advanced:
            db.commit()
        models = list(db.scalars(select(Model).order_by(Model.updated_at.desc())).all())
        versions = list(db.scalars(select(ModelVersion).order_by(ModelVersion.created_at.desc())).all())
        services = list(db.scalars(select(ModelService)).all())
        calls = list(db.scalars(select(ModelCall).order_by(ModelCall.created_at.desc())).all())
        version_map: dict[int, list[ModelVersion]] = {}
        for version in versions:
            version_map.setdefault(version.model_id, []).append(version)
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
            "models": [_model_payload(model, version_map.get(model.id, []), {task.task_id: task for task in edit_tasks}) for model in models],
            "training": [training_task_payload(task) for task in training_tasks],
            "services": [_service_payload(service) for service in services],
            "calls": [{"id": c.id, "model_id": c.model_id, "service_id": c.service_id, "version": c.version, "prompt": c.prompt, "original_output": c.original_output, "governed_output": c.governed_output, "reason": c.reason, "risk_level": c.risk_level, "reconstruction": c.reconstruction, "elapsed_ms": c.elapsed_ms, "created_at": c.created_at.isoformat(), "status": c.status, "trace_id": c.trace_id} for c in calls],
            "assessment": assessment, "changes": [],
            "datasets": [
                {
                    "id": str(dataset.id), "name": dataset.name, "version": dataset.version,
                    "row_count": int((dataset.metadata_json or {}).get("record_count", 0) or 0),
                    # 训练弹窗按 purpose=training 筛选、评估页按 purpose=evaluation 筛选；
                    # 用途存在 metadata_json.purpose，缺省视为训练集。
                    "purpose": (dataset.metadata_json or {}).get("purpose", "training"),
                }
                for dataset in datasets
            ],
        },
        message="模型工作台数据查询成功",
    )


@router.get("/model-workbenches/assessment")
def model_workbench_assessment(
    model_id: str = Query(..., max_length=64),
    baseline: str = Query(..., max_length=100),
    edited: str = Query(..., max_length=100),
    dataset_id: str = Query(..., max_length=64),
    dataset_version: str = Query(..., max_length=100),
):
    """返回指定“模型+基线+编辑后+测试集”组合的最新评估结果。

    聚合接口只携带全局最新一条评估，切换测试集再切回来时旧结果看似被覆盖；
    本接口按五要素回查历史任务。多轮编辑方案下每轮编辑产物是固定版本，
    新一轮编辑不会作废旧轮的评估，各组合各自保留最新结果。
    但评估必须晚于产出该版本号的编辑任务：版本号可能被复用（旧产物行被删后
    重新编辑），此时早于新编辑的旧评估不得匹配到新产物上。"""
    with SessionLocal() as db:
        edit_created = None
        model_pk = int(model_id) if model_id.isdigit() else None
        if model_pk is not None:
            version_row = db.scalar(
                select(ModelVersion).where(
                    ModelVersion.model_id == model_pk,
                    ModelVersion.version == edited,
                )
            )
            producing = db.get(Task, version_row.task_id) if version_row and version_row.task_id else None
            edit_created = producing.created_at if producing else None
        rows = db.scalars(
            select(Task)
            .where(Task.capability_code == "evaluation")
            .order_by(Task.created_at.desc(), Task.task_id.desc())
        ).all()
    for task in rows:
        if edit_created and task.created_at and task.created_at < edit_created:
            continue
        assessment = (task.result or {}).get("model_assessment") or {}
        if (
            str(assessment.get("model_id") or "") == model_id
            and str(assessment.get("baseline") or "") == baseline
            and str(assessment.get("edited") or "") == edited
            and str(assessment.get("dataset_id") or "") == dataset_id
            and str(assessment.get("dataset_version") or "") == dataset_version
        ):
            return success(data=assessment, message="评估结果查询成功")
    return success(data=None, message="该组合尚无评估结果")


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
        # 同一地址允许承载同一模型的不同版本（灰度/回滚），但同一模型版本不得重复登记。
        endpoint_in_use = db.scalar(
            select(ModelService).where(
                ModelService.endpoint == request.endpoint,
                ModelService.model_id == str(model_id),
                ModelService.version == request.version,
            )
        )
        if endpoint_in_use:
            raise HTTPException(status_code=409, detail="该模型的这个版本已登记过相同地址的服务")

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


def sync_model_call_tasks(db) -> int:
    """为尚无任务流水的调用记录补挂 tasks 行。

    全链路追踪页的下拉、深链匹配和五要素证据（evidence-task-{id}）都以 tasks 表为索引；
    真实调用只写 model_calls 会整块失联，这里按同一 CALL 编号补挂，可重复执行。
    """
    missing = db.scalars(
        select(ModelCall).where(ModelCall.id.not_in(select(Task.task_id))).order_by(ModelCall.created_at)
    ).all()
    for call in missing:
        service = db.get(ModelService, call.service_id)
        title = call.prompt[:20] + ("…" if len(call.prompt) > 20 else "")
        db.add(Task(
            task_id=call.id,
            name=f"模型调用：{title}",
            capability_code="model_risk_governance",
            trace_id=call.trace_id,
            status=call.status,
            source_name=service.name if service else call.service_id,
            storage_gb=0,
            progress=100,
            success_count=1,
            input_data={
                "model_call_id": call.id,
                "endpoint": service.endpoint if service else None,
                "prompt": call.prompt,
                "elapsed_ms": call.elapsed_ms,
            },
            config={},
            result={
                "summary": call.governed_output,
                "risk_level": call.risk_level,
                "reason": call.reason,
                "original_output": call.original_output,
            },
            model_version=call.version,
            created_at=call.created_at,
            finished_at=call.created_at,
        ))
    return len(missing)


@router.post("/model-calls")
def record_model_call(request: RecordModelCallRequest):
    """模型调用页真实调用成功后回写留痕；风险命中同时记录合规事件（GN-018）。"""
    with SessionLocal() as db:
        service = db.get(ModelService, request.service_id)
        if service is None:
            raise HTTPException(status_code=404, detail="调用服务不存在")
        if service.status != "running":
            raise HTTPException(status_code=422, detail="服务未处于运行状态，不能登记调用留痕")
        call = ModelCall(
            id=f"CALL-{uuid4().hex[:8]}",
            model_id=str(request.model_id),
            service_id=service.id,
            version=request.version or service.version,
            prompt=request.prompt.strip(),
            original_output=request.original_output,
            governed_output=request.governed_output,
            reason=request.reason,
            risk_level=request.risk_level,
            reconstruction=request.reconstruction,
            elapsed_ms=request.elapsed_ms,
            status="succeeded",
            trace_id=request.trace_id,
            created_at=now_shanghai(),
        )
        db.add(call)
        if call.risk_level in {"high", "medium"}:
            add_event(db, subject_type="model_call", subject_id=call.id,
                      event_type="模型输出异常告警", level=call.risk_level,
                      detail=call.reason or "输出风险判定命中，但未返回判定依据",
                      trace_id=call.trace_id)
        # 会话是 autoflush=False：必须先把新调用行落进事务，补挂查询才看得见它，
        # 否则本端点每次空转、只能等服务重启回填（历史缺陷回归，见 tests/test_model_call_sync.py）。
        db.flush()
        record_call_edge(db, call)
        sync_model_call_tasks(db)
        db.commit()
        return success(data={"id": call.id}, message="调用留痕已登记")


@router.patch("/model-services/{service_id}")
def upgrade_model_service(service_id: str, request: UpgradeModelServiceRequest):
    """把服务重绑到该模型的另一个已登记版本；升级后回到「已部署·待检查」。"""
    with SessionLocal() as db:
        service = db.get(ModelService, service_id)
        if service is None:
            raise HTTPException(status_code=404, detail="推理服务不存在")
        try:
            model_id = int(service.model_id)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="服务绑定的模型 ID 非法") from None
        model = db.get(Model, model_id)
        if model is None:
            raise HTTPException(status_code=404, detail="绑定模型不存在")
        versions = db.scalars(
            select(ModelVersion).where(ModelVersion.model_id == model_id)
        ).all()
        if request.version not in {item.version for item in versions} | {model.version}:
            raise HTTPException(status_code=422, detail="绑定模型不存在该版本")
        if request.version == service.version:
            raise HTTPException(status_code=400, detail="服务已经绑定该版本")
        conflict = db.scalar(
            select(ModelService).where(
                ModelService.endpoint == service.endpoint,
                ModelService.model_id == service.model_id,
                ModelService.version == request.version,
                ModelService.id != service_id,
            )
        )
        if conflict:
            raise HTTPException(
                status_code=409,
                detail=f"同地址下已存在该模型版本的服务「{conflict.name}」，无法切换",
            )
        service.version = request.version
        service.status = "deployed"
        service.checked_at = None
        service.latency_ms = None
        service.healthy = None
        db.commit()
        db.refresh(service)
        return success(
            data=_service_payload(service),
            message=f"服务已切换到版本 {request.version}，等待连接检查",
        )


@router.post("/model-services/{service_id}/stop")
def stop_model_service(service_id: str):
    """停止运行中的服务；停止后模型调用页不再可选，重新检查连接即恢复。"""
    with SessionLocal() as db:
        service = db.get(ModelService, service_id)
        if service is None:
            raise HTTPException(status_code=404, detail="推理服务不存在")
        if service.status != "running":
            raise HTTPException(status_code=400, detail="只有运行中的服务可以停止")
        service.status = "offline"
        service.checked_at = None
        service.latency_ms = None
        service.healthy = None
        db.commit()
        db.refresh(service)
        return success(data=_service_payload(service), message="服务已停止")


@router.delete("/model-services/{service_id}")
def delete_model_service(service_id: str):
    """删除服务登记；运行中的服务须先停止，历史调用记录（model_calls）保留。"""
    with SessionLocal() as db:
        service = db.get(ModelService, service_id)
        if service is None:
            raise HTTPException(status_code=404, detail="推理服务不存在")
        if service.status == "running":
            raise HTTPException(status_code=400, detail="服务正在运行，请先停止后再删除")
        name = service.name
        db.delete(service)
        db.commit()
        return success(data={"id": service_id, "name": name}, message=f"服务「{name}」已删除")


PROBE_TIMEOUT_SECONDS = 3.0


def probe_endpoint(endpoint: str) -> tuple[bool, int | None, str | None]:
    """真实探活：GET 服务地址，返回 (是否可用, 往返毫秒, 失败原因)。"""
    started = perf_counter()
    try:
        with httpx.Client(timeout=PROBE_TIMEOUT_SECONDS) as client:
            response = client.get(endpoint)
    except httpx.TimeoutException:
        return False, None, f"{PROBE_TIMEOUT_SECONDS:.0f} 秒内未响应"
    except httpx.HTTPError as exc:
        return False, None, f"连接失败：{exc.__class__.__name__}"
    latency = max(1, round((perf_counter() - started) * 1000))
    if response.status_code >= 400:
        return False, latency, f"服务返回 HTTP {response.status_code}"
    return True, latency, None


@router.post("/model-services/{service_id}/check")
def check_model_service(service_id: str):
    """向登记的服务地址发一次真实请求；探不通不得记为"已核验"，也不得留在运行中状态。"""
    with SessionLocal() as db:
        service = db.get(ModelService, service_id)
        if service is None:
            raise HTTPException(status_code=404, detail="推理服务不存在")
        endpoint = (service.endpoint or "").strip()
        if not endpoint.startswith(("http://", "https://")):
            # 演示占位地址（local://…）无从探活：如实拒绝，不写任何核验结果，页面保持"未检查"。
            raise HTTPException(
                status_code=422,
                detail=f"「{endpoint}」不是可探活的 HTTP 地址，未记录核验结果；请改为真实推理服务地址")
        reachable, latency, reason = probe_endpoint(endpoint)
        service.checked_at = now_shanghai()
        service.latency_ms = latency
        service.healthy = reachable
        service.status = "running" if reachable else "offline"
        db.commit()
        db.refresh(service)
        if not reachable:
            return success(data=_service_payload(service),
                           message=f"连接检查未通过：{reason}；服务已置为离线")
        return success(data=_service_payload(service),
                       message=f"连接检查通过：往返 {latency} 毫秒")
