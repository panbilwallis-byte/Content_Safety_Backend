from hashlib import sha256
from random import Random
from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, HTTPException

from app.core.database import SessionLocal
from app.core.response import success
from app.core.time import now_shanghai
from app.domain.schemas import CreateTrainingTaskRequest
from app.models.tables import Dataset, Model, TrainingTask


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


def advance_training_task(task: TrainingTask, now: datetime) -> bool:
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
    loss, validation_loss, checkpoints = _generate_training_logs(task.id, task.epochs, target_epoch)
    task.epoch = target_epoch
    task.progress = round(target_epoch * 100 / task.epochs)
    task.loss_history = loss
    task.validation_loss_history = validation_loss
    task.checkpoints = checkpoints
    task.updated_at = now
    if target_epoch == task.epochs:
        task.status = "succeeded"
        task.progress = 100
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
        if db.get(Model, model_id) is None:
            raise HTTPException(status_code=404, detail="基础模型不存在")
        if db.get(Dataset, dataset_id) is None:
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
        db.commit()
        db.refresh(task)
        return success(data=training_task_payload(task), message="训练任务已创建并开始训练")
