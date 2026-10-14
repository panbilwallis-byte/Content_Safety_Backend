
# 下一步：创建“任务仓储层”。
# 仓储层的职责是保存和查询数据。现在先不用数据库，用 Python 列表模拟一张任务表；
# 服务重启后数据会清空，这符合当前学习阶段

# 用列表模拟数据库中的 tasks 表
# TASKS = []


# def save(task: dict):
#     """
#     保存一条任务记录。
#     以后这里会改为 INSERT INTO tasks ...
#     """
#     TASKS.append(task)
#     return task


# def find_by_id(task_id: str):
#     """
#     按任务 ID 查找一条任务。
#     找到就返回任务，找不到返回 None。
#     """
#     for task in TASKS:
#         if task["task_id"] == task_id:
#             return task

#     return None


# def update(task_id: str, changes: dict):
#     """
#     修改已有任务的部分字段。
#     例如把 status 从 running 改成 succeeded。
#     """
#     task = find_by_id(task_id)

#     if task is None:
#         return None

#     task.update(changes)
#     return task


# def list_all(page: int = 1, page_size: int = 20):
#     """
#     分页查询任务。

#     page：第几页，从 1 开始
#     page_size：每页返回多少条
#     """

#     start = (page - 1) * page_size
#     end = start + page_size

#     return {
#         "items": TASKS[start:end],
#         "total": len(TASKS),
#         "page": page,
#         "page_size": page_size,
#     }


from typing import Any

from sqlalchemy import func, select

from app.core.database import SessionLocal
from app.models.tables import Task

from sqlalchemy.orm import Session



def task_summary_to_dict(task: Task) -> dict[str, Any]:
    """Serialize fields needed by task tables without the task payload/result."""
    result = task.result or {}
    return {
        "task_id": task.task_id, "name": task.name, "capability_code": task.capability_code,
        "status": task.status, "source_name": task.source_name or "未填写来源",
        "dataset_name": task.dataset_name or "未指定数据集", "storage_gb": float(task.storage_gb or 0),
        "progress": 100 if task.progress is None or (task.progress == 0 and task.status == "succeeded") else task.progress,
        "success_count": task.success_count or 0, "duplicate_count": task.duplicate_count or 0,
        "anomaly_count": task.anomaly_count or 0, "processed_count": result.get("processed_count", task.success_count or 0), "total_count": result.get("total_count", task.success_count or 0), "output_version": result.get("output_version"), "rule_name": result.get("rule_name", "标准清洗流程"), "elapsed_ms": result.get("elapsed_ms"),
        "dataset_version": task.dataset_version, "model_version": task.model_version, "trace_id": task.trace_id,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
    }

def task_to_dict(task: Task) -> dict[str, Any]:
    result = task.result or {}
    # 数据处理任务的领域字段保存在 result JSON 中，统一展开给工作台页面。
    process_fields = {
        "processed_count": result.get("processed_count", task.success_count or 0),
        "total_count": result.get("total_count", task.success_count or 0),
        "remaining_seconds": result.get("remaining_seconds"),
        "steps": result.get("steps", []),
        "comparisons": result.get("comparisons", []),
        "output_version": result.get("output_version"),
        "rule_name": result.get("rule_name", "标准清洗流程"),
        "error_message": result.get("error_message"),
    }
    return {
    "task_id": task.task_id,
    "name": task.name,
    "capability_code": task.capability_code,
    "status": task.status,

    "source_name": task.source_name or "未填写来源",
    "dataset_name": task.dataset_name or "未指定数据集",
    "storage_gb": float(task.storage_gb or 0),

    "progress": (
        100
        if task.progress is None
        or (
            task.progress == 0
            and task.status == "succeeded"
        )
        else task.progress
    ),

    "success_count": task.success_count or 0,
    "duplicate_count": task.duplicate_count or 0,
    "anomaly_count": task.anomaly_count or 0,
    "elapsed_ms": result.get("elapsed_ms"),

    "input": task.input_data,
    "config": task.config,
    "result": result,
    **process_fields,
    "dataset_version": task.dataset_version,
    "model_version": task.model_version,
    "trace_id": task.trace_id,

    "created_at": (
        task.created_at.isoformat()
        if task.created_at
        else None
    ),
    "finished_at": (
        task.finished_at.isoformat()
        if task.finished_at
        else None
    ),
    }


def save(task_data: dict[str, Any], db: Session):
    task = Task(
        task_id=task_data["task_id"],
        name=task_data["name"],
        capability_code=task_data["capability_code"],
        status=task_data["status"],

        source_name=task_data.get("source_name"),
        dataset_name=task_data.get("dataset_name"),
        storage_gb=task_data.get("storage_gb", 0),
        progress=task_data.get("progress", 100),
        success_count=task_data.get("success_count", 0),
        duplicate_count=task_data.get("duplicate_count", 0),
        anomaly_count=task_data.get("anomaly_count", 0),

        input_data=task_data.get("input", {}),
        config=task_data.get("config", {}),
        result=task_data.get("result"),
        dataset_version=task_data.get("dataset_version"),
        model_version=task_data.get("model_version"),
        created_at=task_data["created_at"],
        finished_at=task_data.get("finished_at"),
        trace_id=task_data.get("trace_id"),
    )

    db.add(task)
    db.flush()

    return task_to_dict(task)


def find_by_id(task_id: str):
    """
    根据任务 ID 从 MySQL 查询任务。
    """

    with SessionLocal() as db:
        task = db.scalar(
            select(Task).where(Task.task_id == task_id)
        )

        if task is None:
            return None

        return task_to_dict(task)


def update(
    task_id: str,
    changes: dict[str, Any],
    db: Session,
):
    task = db.scalar(
        select(Task).where(Task.task_id == task_id)
    )

    if task is None:
        return None

    for field in (
        "status",
        "dataset_name",
        "storage_gb",
        "result",
        "finished_at",
        "progress",
        "success_count",
        "duplicate_count",
        "anomaly_count",
    ):
        if field in changes:
            setattr(task, field, changes[field])

    db.flush()

    return task_to_dict(task)


def list_all(
    page: int = 1,
    page_size: int = 20,
    capability_code: str | None = None,
    keyword: str | None = None,
    status: str | None = None,
    completed_only: bool = False,
):
    """
    从 MySQL 分页查询任务。
    """

    offset = (page - 1) * page_size

    with SessionLocal() as db:
        query = select(Task)

        if capability_code:
            query = query.where(
                Task.capability_code == capability_code
            )
        if status:
            query = query.where(Task.status == status)
        if completed_only:
            query = query.where(Task.status == "succeeded", Task.progress >= 100)
        if keyword and keyword.strip():
            like = f"%{keyword.strip()}%"
            query = query.where(
                Task.name.like(like) | Task.source_name.like(like)
            )

        # 不按任务名去重：每一次接入都必须作为独立记录，最新提交的任务
        # 才能实时出现在“最近接入任务”里。
        all_tasks = db.scalars(
            query
            .order_by(Task.created_at.desc())
        ).all()

        total = len(all_tasks)
        tasks = all_tasks[offset:offset + page_size]
        # “接入成功”统计的是平台数据库中已完成的数据接入任务数，
        # 不等同于某一任务本次读取到的记录行数。
        succeeded_total = 0
        if capability_code == "data_ingest":
            succeeded_total = db.scalar(
                select(func.count())
                .select_from(Task)
                .where(
                    Task.capability_code == "data_ingest",
                    Task.status == "succeeded",
                    Task.progress >= 100,
                )
            ) or 0

        return {
            "items": [task_summary_to_dict(task) for task in tasks],
            "total": total or 0,
            "page": page,
            "page_size": page_size,
            "total_pages": (
                (total + page_size - 1) // page_size
                if total
                else 0
            ),
            "succeeded_total": succeeded_total,
        }
