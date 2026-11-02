"""评估页入口：后台解析版本绑定，不要求测试人员选择真实模型。"""
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.database import SessionLocal
from app.core.response import success
from app.models.tables import Task
from app.services import model_comparison as service

router = APIRouter()


class Comparison(BaseModel):
    model_id: int = Field(gt=0)
    baseline: str = Field(min_length=1, max_length=100)
    edited: str = Field(min_length=1, max_length=100)
    dataset_id: int = Field(gt=0)
    dataset_version: str = Field(min_length=1, max_length=100)


@router.post("/model-workbenches/comparisons", status_code=202)
def create(body: Comparison, request: Request, background: BackgroundTasks):
    token = request.headers.get("X-LLM-Authorization", "")
    with SessionLocal() as db:
        task = service.create(db, body, token)
        task_id = task.task_id
    background.add_task(service.execute, task_id, token)
    return success({"taskId": task_id, "status": "pending"}, "真实对照任务已提交")


@router.get("/model-workbenches/comparisons/{task_id}")
def progress(task_id: str, request: Request):
    own = service.owner(request.headers.get("X-LLM-Authorization", ""))
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        if not task or (task.config or {}).get("owner") != own:
            raise HTTPException(404, "对照评估不存在")
        return success({"taskId": task_id, "status": task.status, "progress": task.progress,
                        "result": task.result})
