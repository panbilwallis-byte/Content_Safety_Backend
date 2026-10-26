import os
from hashlib import sha256
from random import Random
from datetime import datetime
from uuid import uuid4

import httpx
from fastapi import Header

from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.core.response import success
from app.core.time import now_shanghai
from app.domain.schemas import CreateTrainingTaskRequest, CreateRealTrainingRequest, SyncTrainingTaskRequest
from app.models.tables import Dataset, Model, ModelVersion, TrainingTask
from app.services.compliance_event_service import add_event


router = APIRouter()

LOG_DELAY_SECONDS = 10
EPOCH_INTERVAL_SECONDS = 60


def training_task_payload(task: TrainingTask) -> dict:
    return {
        "id": task.id,
        "name": task.name,
        "description": "新建训练任务",
        "status": task.status,
        "progress": task.progress,
        "model_id": task.model_id,
        "base_version": task.base_version,
        "dataset_id": task.dataset_id,
        "dataset_version": task.dataset_version,
        "epochs": task.epochs,
        "epoch": task.epoch,
        "learning_rate": task.learning_rate,
        "batch_size": task.batch_size,
        "target_version": task.target_version,
        "method": task.method,
        "elapsed": "训练中" if task.status == "running" else "尚未开始",
        "updated_at": task.updated_at.isoformat(),
        "loss": task.loss_history or [],
        "validation_loss": task.validation_loss_history or [],
        "checkpoints": task.checkpoints or [],
    }


def _generate_training_logs(task_id: str, epochs: int, current_epoch: int) -> tuple[list[float], list[float], list[dict]]:
    """生成可复现的演示训练日志，并与任务一同持久化。"""
    randomizer = Random(int.from_bytes(sha256(task_id.encode("utf-8")).digest()[:8], "big"))
    initial_loss = randomizer.uniform(2.2, 3.1)
    loss = [round(initial_loss * (0.68**index) + randomizer.uniform(-0.03, 0.03), 3) for index in range(current_epoch + 1)]
    validation_loss = [round(value * randomizer.uniform(1.04, 1.12), 3) for value in loss]
    checkpoint_interval = max(1, epochs // 3)
    checkpoints = [
        {"name": f"checkpoint-{index:02d}", "epoch": index, "loss": validation_loss[index]}
        for index in range(1, current_epoch + 1)
        if index == current_epoch or index % checkpoint_interval == 0
    ]
    return loss, validation_loss, checkpoints


def register_trained_version(db: Session, task: TrainingTask) -> None:
    """训练成功后把目标版本登记为模型产物，保证模型管理页与训练台账互相对得上。"""
    try:
        model_id = int(task.model_id)
    except (TypeError, ValueError):
        return
    model = db.get(Model, model_id)
    if model is None or not task.target_version:
        return
    exists = db.scalar(
        select(ModelVersion).where(
            ModelVersion.model_id == model_id,
            ModelVersion.version == task.target_version,
        )
    )
    if exists is None:
        db.add(ModelVersion(
            model_id=model_id,
            version=task.target_version,
            description=f"由训练任务「{task.name}」（{task.id}）产出",
            task_id=task.id,
        ))
    # 仅当基础版本仍是当前版本时才前移指针，避免用旧任务把新版本改回去。
    if model.version == task.base_version:
        model.version = task.target_version
        model.updated_at = now_shanghai()


def ensure_trained_model_versions() -> None:
    """启动时补登历史成功训练任务的模型版本，修复登记逻辑上线前留下的缺口。"""
    with SessionLocal() as db:
        tasks = db.scalars(
            select(TrainingTask).where(TrainingTask.status == "succeeded")
        ).all()
        for task in tasks:
            register_trained_version(db, task)
        db.commit()


def advance_training_task(db: Session, task: TrainingTask, now: datetime) -> bool:
    """按真实经过时间缓慢推进演示训练；每次推进均写回数据库。"""
    if task.status != "running":
        return False
    elapsed_seconds = max(0, int((now - task.created_at).total_seconds()))
    if elapsed_seconds < LOG_DELAY_SECONDS:
        return False
    target_epoch = min(
        task.epochs,
        1 + (elapsed_seconds - LOG_DELAY_SECONDS) // EPOCH_INTERVAL_SECONDS,
    )
    if target_epoch <= task.epoch:
        return False
    previous_epoch = task.epoch
    loss, validation_loss, checkpoints = _generate_training_logs(task.id, task.epochs, target_epoch)
    task.epoch = target_epoch
    task.progress = round(target_epoch * 100 / task.epochs)
    task.loss_history = loss
    task.validation_loss_history = validation_loss
    task.checkpoints = checkpoints
    task.updated_at = now
    for epoch_index in range(previous_epoch + 1, target_epoch + 1):
        add_event(db, subject_type="training_task", subject_id=task.id, training_task_id=task.id,
                  event_type="训练轮次完成",
                  detail=f"第 {epoch_index} 轮：训练损失 {loss[epoch_index - 1]}，"
                         f"验证损失 {validation_loss[epoch_index - 1]}")
    for checkpoint in checkpoints:
        if checkpoint["epoch"] > previous_epoch:
            add_event(db, subject_type="training_task", subject_id=task.id, training_task_id=task.id,
                      event_type="检查点保存",
                      detail=f"{checkpoint['name']}（第 {checkpoint['epoch']} 轮，"
                             f"验证损失 {checkpoint['loss']}）")
    if target_epoch == task.epochs:
        task.status = "succeeded"
        task.progress = 100
        register_trained_version(db, task)
        add_event(db, subject_type="training_task", subject_id=task.id, training_task_id=task.id,
                  event_type="训练任务完成",
                  detail=f"共 {task.epochs} 轮完成，目标版本 {task.target_version} 已登记")
    return True


@router.post("/training-tasks")
def create_training_task(request: CreateTrainingTaskRequest):
    """创建并立即开始演示训练，同时保存训练日志和检查点。"""
    now = now_shanghai()
    with SessionLocal() as db:
        try:
            model_id = int(request.model_id)
            dataset_id = int(request.dataset_id)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="模型和数据集 ID 必须为数字") from None
        model = db.get(Model, model_id)
        if model is None:
            raise HTTPException(status_code=404, detail="基础模型不存在")
        dataset = db.get(Dataset, dataset_id)
        if dataset is None:
            raise HTTPException(status_code=404, detail="训练数据集不存在")

        task_id = f"TR-{uuid4().hex[:8]}"
        task = TrainingTask(
            id=task_id,
            name=request.name.strip(),
            status="running",
            progress=0,
            model_id=str(model_id),
            base_version=request.base_version.strip(),
            dataset_id=str(dataset_id),
            dataset_version=request.dataset_version.strip(),
            epochs=request.epochs,
            epoch=0,
            learning_rate=request.learning_rate,
            batch_size=request.batch_size,
            target_version=request.target_version.strip(),
            method=request.method.strip(),
            loss_history=[],
            validation_loss_history=[],
            checkpoints=[],
            created_at=now,
            updated_at=now,
        )
        db.add(task)
        add_event(db, subject_type="training_task", subject_id=task_id, training_task_id=task_id,
                  event_type="训练任务启动",
                  detail=f"{task.name}：模型 {model.name} {task.base_version}，"
                         f"数据集 {dataset.name} {task.dataset_version}，"
                         f"{task.epochs} 轮 · {task.method}")
        if (dataset.metadata_json or {}).get("usage") == "test":
            add_event(db, subject_type="training_task", subject_id=task_id, training_task_id=task_id,
                      event_type="训练数据越权访问尝试", level="high",
                      detail=f"数据集「{dataset.name}」标记为测试专用（usage=test），"
                             f"不应作为训练输入，请核实任务 {task_id}")
        db.commit()
        db.refresh(task)
        return success(data=training_task_payload(task), message="训练任务已创建并开始训练")


LLM_TRAINING_BASE_URL = os.getenv("LLM_TRAINING_BASE_URL", "http://127.0.0.1:8001/api/v1/training")
LLM_SETUP_SECRET = os.getenv("LLM_SETUP_SECRET", "")


@router.post("/training-tasks/real")
def create_real_training_task(request: CreateRealTrainingRequest,
                              authorization: str = Header(default="")):
    """代理模型服务建迷你脚本任务，成功后登记同款台账。

    建单优先使用本服务配置的模型服务令牌（环境变量 LLM_ACCESS_TOKEN），未配置时
    退回透传浏览器头；平台登录令牌只用于 8000 网关，不再送往模型服务。
    X-Setup-Secret 只保存在本服务环境变量中，不进前端产物。
    """
    if not LLM_SETUP_SECRET:
        raise HTTPException(status_code=501, detail="未配置 LLM_SETUP_SECRET，无法代理真实训练建单")
    read_headers = _llm_read_headers(authorization)
    with SessionLocal() as db:
        try:
            model_id = int(request.model_id)
            dataset_id = int(request.dataset_id)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="模型和数据集 ID 必须为数字") from None
        model = db.get(Model, model_id)
        if model is None:
            raise HTTPException(status_code=404, detail="基础模型不存在")
        dataset = db.get(Dataset, dataset_id)
        if dataset is None:
            raise HTTPException(status_code=404, detail="训练数据集不存在")
        upstream = httpx.post(
            f"{LLM_TRAINING_BASE_URL}/script-tasks",
            json={"name": request.name.strip(), "script_id": "mini-mlp",
                  "parameters": {"epochs": request.epochs, "learning_rate": request.learning_rate}},
            headers={"Authorization": read_headers["Authorization"], "X-Setup-Secret": LLM_SETUP_SECRET},
            timeout=30,
        )
        if upstream.status_code >= 400:
            try:
                message = upstream.json().get("message") or upstream.text[:200]
            except ValueError:
                message = upstream.text[:200]
            # 上游的"令牌无效/无权"是本服务与模型服务之间的配置问题，映射为 502，
            # 避免前端把 401 一律当成平台登录过期而误踢用户下线。
            status = 502 if upstream.status_code in {401, 403} else upstream.status_code
            raise HTTPException(status_code=status, detail=f"模型服务建单失败：{message}")
        llm_task = upstream.json()["data"]
        now = now_shanghai()
        task = TrainingTask(
            id=llm_task["id"], name=request.name.strip(), status="pending", progress=0,
            model_id=str(model_id), base_version=request.base_version.strip(),
            dataset_id=str(dataset_id), dataset_version=request.dataset_version.strip(),
            epochs=request.epochs, epoch=0, learning_rate=request.learning_rate,
            batch_size=request.batch_size, target_version=request.target_version.strip(),
            method=request.method.strip(), loss_history=[], validation_loss_history=[],
            checkpoints=[], created_at=now, updated_at=now,
        )
        db.add(task)
        add_event(db, subject_type="training_task", subject_id=task.id, training_task_id=task.id,
                  event_type="训练任务启动",
                  detail=f"{task.name}：真实脚本 mini-mlp，模型 {model.name} {task.base_version}，"
                         f"数据集 {dataset.name} {task.dataset_version}，{task.epochs} 轮 · {task.method}")
        if (dataset.metadata_json or {}).get("usage") == "test":
            add_event(db, subject_type="training_task", subject_id=task.id, training_task_id=task.id,
                      event_type="训练数据越权访问尝试", level="high",
                      detail=f"数据集「{dataset.name}」标记为测试专用（usage=test），请核实任务 {task.id}")
        db.commit()
        db.refresh(task)
        payload = training_task_payload(task)
        payload["llm_task_id"] = llm_task["id"]
        return success(data=payload, message="真实训练任务已创建，等待模型服务执行")


LLM_ACCESS_TOKEN = os.getenv("LLM_ACCESS_TOKEN", "")


def _llm_read_headers(authorization: str) -> dict:
    # 优先用本服务配置的模型令牌；未配置时退回透传浏览器令牌，保持旧行为可用。
    token = LLM_ACCESS_TOKEN or (authorization[7:] if authorization.startswith("Bearer ") else "")
    if not token:
        raise HTTPException(status_code=422, detail="未配置模型服务访问令牌（环境变量 LLM_ACCESS_TOKEN）")
    return {"Authorization": f"Bearer {token}"}


def _llm_read(url: str, params: dict, authorization: str):
    upstream = httpx.get(url, params=params, headers=_llm_read_headers(authorization), timeout=30)
    if upstream.status_code >= 400:
        try:
            message = upstream.json().get("message") or upstream.text[:200]
        except ValueError:
            message = upstream.text[:200]
        raise HTTPException(status_code=upstream.status_code, detail=f"模型服务：{message}")
    return success(data=upstream.json()["data"])


@router.get("/training-tasks/{task_id}/detail")
def proxy_training_detail(task_id: str, authorization: str = Header(default="")):
    """台账只读代理：任务详情。浏览器无需持有模型服务令牌。"""
    return _llm_read(f"{LLM_TRAINING_BASE_URL}/tasks/{task_id}", {}, authorization)


@router.get("/training-tasks/{task_id}/metrics")
def proxy_training_metrics(task_id: str, after_id: int = Query(0, ge=0),
                           limit: int = Query(1000, ge=1, le=1000),
                           authorization: str = Header(default="")):
    """台账只读代理：逐 step 训练指标（游标增量）。"""
    return _llm_read(f"{LLM_TRAINING_BASE_URL}/tasks/{task_id}/metrics",
                     {"after_id": after_id, "limit": limit}, authorization)


@router.get("/training-tasks/{task_id}/artifacts")
def proxy_training_artifacts(task_id: str, authorization: str = Header(default="")):
    """台账只读代理：真实产物清单（文件名/大小/SHA-256）。"""
    return _llm_read(f"{LLM_TRAINING_BASE_URL}/tasks/{task_id}/artifacts", {}, authorization)


@router.get("/training-tasks/{task_id}/artifacts/{artifact_id}/download")
def proxy_training_artifact_download(task_id: str, artifact_id: str,
                                     authorization: str = Header(default="")):
    """台账只读代理：产物原始字节流下载。"""
    upstream = httpx.get(
        f"{LLM_TRAINING_BASE_URL}/tasks/{task_id}/artifacts/{artifact_id}/download",
        headers=_llm_read_headers(authorization), timeout=120, follow_redirects=True)
    if upstream.status_code >= 400:
        try:
            message = upstream.json().get("message") or upstream.text[:200]
        except ValueError:
            message = upstream.text[:200]
        raise HTTPException(status_code=upstream.status_code, detail=f"模型服务：{message}")
    disposition = upstream.headers.get("content-disposition", "")
    return Response(content=upstream.content,
                    media_type=upstream.headers.get("content-type", "application/octet-stream"),
                    headers={"Content-Disposition": disposition or f'attachment; filename="{artifact_id}"'})


@router.post("/training-tasks/{task_id}/sync")
def sync_real_training_task(task_id: str, request: SyncTrainingTaskRequest):
    """把模型服务的真实进度同步进台账；轮次、检查点与终态生成合规事件。"""
    with SessionLocal() as db:
        task = db.get(TrainingTask, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="训练任务不存在")
        if len(task_id) < 20:
            # 旧演示任务由服务端时间推进，不接受外部同步，避免双写打架。
            return success(data=training_task_payload(task), message="演示任务由服务端推进，忽略同步")
        previous_epoch = task.epoch
        previous_status = task.status
        task.status = request.status
        task.epoch = min(request.epoch, task.epochs)
        task.progress = round(request.progress)
        task.loss_history = request.loss_history
        task.validation_loss_history = request.validation_loss_history
        task.checkpoints = request.checkpoints
        task.updated_at = now_shanghai()
        for epoch_index in range(previous_epoch + 1, task.epoch + 1):
            train_loss = request.loss_history[epoch_index - 1] if len(request.loss_history) >= epoch_index else "—"
            valid_loss = (request.validation_loss_history[epoch_index - 1]
                          if len(request.validation_loss_history) >= epoch_index else "—")
            add_event(db, subject_type="training_task", subject_id=task.id, training_task_id=task.id,
                      event_type="训练轮次完成",
                      detail=f"第 {epoch_index} 轮（真实训练）：训练损失 {train_loss}，验证损失 {valid_loss}")
        for checkpoint in request.checkpoints:
            if isinstance(checkpoint, dict) and checkpoint.get("epoch", 0) > previous_epoch:
                add_event(db, subject_type="training_task", subject_id=task.id, training_task_id=task.id,
                          event_type="检查点保存",
                          detail=f"{checkpoint.get('name', 'checkpoint')}（第 {checkpoint.get('epoch')} 轮，"
                                 f"验证损失 {checkpoint.get('loss')}）")
        if request.status in {"succeeded", "failed", "canceled"} and previous_status not in {
                "succeeded", "failed", "canceled"}:
            if request.status == "succeeded":
                register_trained_version(db, task)
                add_event(db, subject_type="training_task", subject_id=task.id, training_task_id=task.id,
                          event_type="训练任务完成",
                          detail=f"真实训练完成 {task.epoch}/{task.epochs} 轮，目标版本 {task.target_version} 已登记")
            else:
                add_event(db, subject_type="training_task", subject_id=task.id, training_task_id=task.id,
                          event_type="训练任务失败", level="high",
                          detail=request.error_message or f"任务以 {request.status} 结束")
        db.commit()
        db.refresh(task)
        return success(data=training_task_payload(task), message="训练进度已同步")
