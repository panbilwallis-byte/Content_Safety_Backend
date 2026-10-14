from fastapi import APIRouter, Query

from app.core.response import success
from app.core.database import SessionLocal
from app.models.tables import Task
from app.repositories.audit_repository import (
    list_all,
    list_by_task_id,
)


# 创建审计日志路由对象
router = APIRouter()
EVENT_TYPE_LABELS = {
    "task_started": "任务开始",
    "task_finished": "任务完成",
    "task_failed": "任务失败",
    "dataset_updated": "数据集更新",
}
CAPABILITY_LABELS = {
    "data_ingest": "数据接入任务",
    "semantic_risk": "语义风险识别任务",
    "anomaly_detect": "异常数据检测任务",
    "evaluation": "测试评估任务",
}


def _display_name(log: dict, task_names: dict[str, str]) -> str:
    """用真实任务名称替换固定的“任务开始/任务完成”文案。"""
    event_type = log.get("event_type", "")
    action = {
        "task_started": "执行开始",
        "task_finished": "执行完成",
        "task_failed": "执行失败",
        "dataset_updated": "数据集更新",
    }.get(event_type, event_type or "系统事件")
    task_name = task_names.get(log.get("task_id"))
    if task_name:
        return f"{task_name}·{action}"
    response = log.get("response") or {}
    request = log.get("request") or {}
    resource_name = response.get("name") or request.get("name")
    if isinstance(resource_name, str) and resource_name.strip() and not resource_name.isdigit():
        return f"{resource_name.strip()}·{action}"
    capability_name = CAPABILITY_LABELS.get(log.get("capability_code"), "系统任务")
    return f"{capability_name}·{action}"


def _with_display_names(logs: list[dict]) -> list[dict]:
    task_ids = {log.get("task_id") for log in logs if log.get("task_id")}
    if task_ids:
        with SessionLocal() as db:
            tasks = db.query(Task).filter(Task.task_id.in_(task_ids)).all()
        task_names = {task.task_id: task.name for task in tasks}
    else:
        task_names = {}
    return [{**log, "display_name": _display_name(log, task_names)} for log in logs]

# 查询全部审计日志
@router.get("/audit/logs")
def get_audit_logs():
    logs = _with_display_names(list_all())

    return success(
        data=logs,
        message="审计日志查询成功",
    )


# 根据任务 ID 查询该任务的审计日志
@router.get("/audit/logs/{task_id}")
def get_task_audit_logs(task_id: str):
    logs = _with_display_names(list_by_task_id(task_id))

    return success(
        data=logs,
        message="任务审计日志查询成功",
    )

@router.get("/logs")
def get_system_logs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    keyword: str | None = None,
):
    """
    系统管理页面的“系统日志”列表。

    前端 ResourceTable 会请求 GET /api/v1/logs。
    数据来自 MySQL 中已有的 audit_logs 表。
    """
    logs = _with_display_names(list_all())

    # 搜索：事件名称、能力编码、任务 ID、接口地址、追踪 ID
    if keyword:
        text = keyword.strip().lower()

        def matched(log: dict) -> bool:
            values = [
                log.get("event_type", ""),
                log.get("capability_code", ""),
                log.get("task_id", ""),
                log.get("endpoint", ""),
                log.get("trace_id", ""),
            ]

            return any(
                text in str(value).lower()
                for value in values
                if value is not None
            )

        logs = [
            log
            for log in logs
            if matched(log)
        ]

    total = len(logs)
    start = (page - 1) * page_size
    end = start + page_size

    items = []

    for log in logs[start:end]:
        event_type = log.get("event_type", "")
        is_failed = event_type == "task_failed"

        items.append(
            {
                # ResourceTable 固定使用的字段
                "id": log["log_id"],
                "name": log.get("display_name") or EVENT_TYPE_LABELS.get(event_type, event_type or "系统事件"),
                "category": (
                    log.get("capability_code")
                    or "系统管理"
                ),
                "version": (
                    log.get("created_at")
                    or "—"
                ),
                "status": (
                    "pending"
                    if is_failed
                    else "normal"
                ),

                # 点击“查看”后可显示详细信息
                "description": (
                    f"事件类型：{event_type or '—'}\n"
                    f"任务 ID：{log.get('task_id') or '—'}\n"
                    f"调用接口：{log.get('endpoint') or '—'}\n"
                    f"追踪 ID：{log.get('trace_id') or '—'}\n"
                    f"执行耗时："
                    f"{str(log.get('duration_ms')) + ' ms' if log.get('duration_ms') is not None else '—'}"
                ),

                # 额外保留，后续可做专用日志页面
                "task_id": log.get("task_id"),
                "capability_code": log.get("capability_code"),
                "event_type": event_type,
                "endpoint": log.get("endpoint"),
                "trace_id": log.get("trace_id"),
                "duration_ms": log.get("duration_ms"),
                "request": log.get("request"),
                "response": log.get("response"),
                "created_at": log.get("created_at"),
            }
        )

    return success(
        data={
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (
                (total + page_size - 1) // page_size
                if total
                else 0
            ),
        },
        message="系统日志查询成功",
    )
