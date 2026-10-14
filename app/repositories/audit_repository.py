# from datetime import datetime


# # 用列表模拟数据库中的 audit_logs 表
# AUDIT_LOGS = []


# def add_log(
#     task_id: str,
#     event_type: str,
#     request_data: dict | None = None,
#     response_data: dict | None = None,
# ):
#     """
#     保存一条日志。

#     event_type 例如：
#     task_started：任务开始
#     task_finished：任务完成
#     task_failed：任务失败
#     """

#     log = {
#         "log_id": len(AUDIT_LOGS) + 1,
#         "task_id": task_id,
#         "event_type": event_type,
#         "request": request_data,
#         "response": response_data,
#         "created_at": datetime.now().isoformat(),
#     }

#     AUDIT_LOGS.append(log)

#     return log


# def list_all():
#     """
#     查询所有审计日志。
#     """
#     return AUDIT_LOGS


# def list_by_task_id(task_id: str):
#     """
#     查询某一个任务的全部日志。
#     """
#     return [
#         log
#         for log in AUDIT_LOGS
#         if log["task_id"] == task_id
#     ]





# from datetime import datetime


# # 用列表模拟数据库中的 audit_logs 表
# AUDIT_LOGS = []


# def add_log(
#     task_id: str | None,
#     event_type: str,
#     request_data: dict | None = None,
#     response_data: dict | None = None,
# ):
#     """
#     保存一条日志。

#     event_type 例如：
#     task_started：任务开始
#     task_finished：任务完成
#     task_failed：任务失败
#     """

#     log = {
#         "log_id": len(AUDIT_LOGS) + 1,
#         "task_id": task_id,
#         "event_type": event_type,
#         "request": request_data,
#         "response": response_data,
#         "created_at": datetime.now().isoformat(),
#     }

#     AUDIT_LOGS.append(log)

#     return log


# def list_all():
#     """
#     查询所有审计日志。
#     """
#     return AUDIT_LOGS


# def list_by_task_id(task_id: str):
#     """
#     查询某一个任务的全部日志。
#     """
#     return [
#         log
#         for log in AUDIT_LOGS
#         if log["task_id"] == task_id
#     ]





from sqlalchemy.orm import Session
from typing import Any

from sqlalchemy import select

from app.core.database import SessionLocal
from app.models.tables import AuditLog


def audit_log_to_dict(log: AuditLog) -> dict[str, Any]:
    """
    把数据库中的审计日志对象转换为接口返回字典。
    """

    return {
        "log_id": log.id,
        "task_id": log.task_id,
        "capability_code": log.capability_code,
        "event_type": log.event_type,
        "endpoint": log.endpoint,
        "trace_id": log.trace_id,
        "request": log.request_data,
        "response": log.response_data,
        "duration_ms": log.duration_ms,
        "created_at": (
            log.created_at.isoformat()
            if log.created_at
            else None
        ),
    }


def add_log(
    task_id: str | None,
    event_type: str,
    request_data: dict[str, Any] | None = None,
    response_data: dict[str, Any] | None = None,
    capability_code: str | None = None,
    endpoint: str | None = None,
    trace_id: str | None = None,
    duration_ms: int | None = None,
    db: Session | None = None,
):
    """
    创建一条审计日志并保存到 MySQL。
    """

    log = AuditLog(
        task_id=task_id,
        capability_code=capability_code,
        event_type=event_type,
        endpoint=endpoint,
        trace_id=trace_id,
        request_data=request_data,
        response_data=response_data,
        duration_ms=duration_ms,
    )

    if db is not None:
        db.add(log)
        db.flush()
        return audit_log_to_dict(log)

    # 兼容数据集创建等暂时没有统一事务的调用
    with SessionLocal() as own_db:
        own_db.add(log)
        own_db.commit()
        own_db.refresh(log)
        return audit_log_to_dict(log)
  


def list_all():
    """
    查询全部审计日志，最新日志排在前面。
    """

    with SessionLocal() as db:
        logs = db.scalars(
            select(AuditLog)
            .order_by(AuditLog.created_at.desc())
        ).all()

        return [
            audit_log_to_dict(log)
            for log in logs
        ]


def list_by_task_id(task_id: str):
    """
    查询某一个任务的全部审计日志。
    """

    with SessionLocal() as db:
        logs = db.scalars(
            select(AuditLog)
            .where(AuditLog.task_id == task_id)
            .order_by(AuditLog.created_at.asc())
        ).all()

        return [
            audit_log_to_dict(log)
            for log in logs
        ]