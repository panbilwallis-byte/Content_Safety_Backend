from fastapi import APIRouter, Query
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.core.response import success
from app.core.database import SessionLocal
from app.models.tables import Dataset, Model, Task


router = APIRouter()

# 数据处理页只统计这个能力编码，不能把价值分析、异常治理等其他任务混入。
PROCESS_CAPABILITY = "data_process"
VALUE_CAPABILITIES = ("value_score", "high_value_detect")
ANOMALY_CAPABILITIES = ("anomaly_detect", "anomaly_repair")
RISK_CAPABILITIES = ("data_risk_weight", "semantic_risk")


def _capability_tasks(capabilities: tuple[str, ...]) -> list[Task]:
    """读取指定能力的任务；页面指标之间不共享任务口径。"""
    with SessionLocal() as db:
        return list(
            db.scalars(
                select(Task)
                .where(Task.capability_code.in_(capabilities))
                .order_by(Task.created_at.asc())
            ).all()
        )


def _result(task: Task) -> dict:
    return task.result if isinstance(task.result, dict) else {}


def _number(result: dict, *keys: str) -> float:
    for key in keys:
        value = result.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return 0.0


DEFAULT_RESOURCE_STORAGE_GB = 6266.8


def _resource_storage_gb() -> float:
    """与数据资源页共用 datasets.metadata_json.storage_gb 的统计口径。"""
    try:
        with SessionLocal() as db:
            datasets = db.scalars(select(Dataset)).all()
    except SQLAlchemyError:
        return DEFAULT_RESOURCE_STORAGE_GB

    storage_gb = sum(
        float((item.metadata_json or {}).get("storage_gb", 0) or 0)
        for item in datasets
    )
    return round(storage_gb, 3) if storage_gb > 0 else DEFAULT_RESOURCE_STORAGE_GB


def _managed_model_count() -> int:
    """Use the same database model population as the model workbench."""
    with SessionLocal() as db:
        return len(db.scalars(select(Model)).all())


def _process_task_count() -> int:
    """首页与数据处理页共用的治理任务统计口径。"""
    with SessionLocal() as db:
        return int(
            db.scalar(
                select(func.count())
                .select_from(Task)
                .where(Task.capability_code == PROCESS_CAPABILITY)
            )
            or 0
        )


# 首页卡片的兜底展示值由后端维护；资源、模型和治理任务按数据库实时汇总。
DASHBOARD_KPIS = [
    {
        "id": "dashboard-1",
        "label": "数据资源总量",
        "value": DEFAULT_RESOURCE_STORAGE_GB,
        "unit": "GB",
        "change_rate": 12,
        "icon": "Coin",
    },
    {
        "id": "dashboard-2",
        "label": "高价值语料数量",
        "value": 2318,
        "unit": "万条",
        "change_rate": 12,
        "icon": "Document",
    },
    {
        "id": "dashboard-3",
        "label": "风险数据数量",
        "value": 86.5,
        "unit": "万条",
        "change_rate": -26,
        "icon": "WarningFilled",
    },
    {
        "id": "dashboard-4",
        "label": "在管模型数量",
        "value": 42,
        "unit": "个",
        "change_rate": 12,
        "icon": "Box",
    },
    {
        "id": "dashboard-5",
        "label": "累计治理任务",
        "value": 0,
        "unit": "个",
        "change_rate": 12,
        "icon": "CircleCheckFilled",
    },
    {
        "id": "dashboard-6",
        "label": "测试完成率",
        "value": 92.3,
        "unit": "%",
        "change_rate": 12,
        "icon": "PieChart",
    },
]


@router.get("/kpis")
def get_kpis(
    kind: str = Query(default="dashboard"),
):
    """
    返回页面顶部 KPI 指标卡片数据。
    当前仅实现首页 dashboard。
    """
    if kind == "dashboard":
        kpis = [dict(item) for item in DASHBOARD_KPIS]
        kpis[0].update({"value": _resource_storage_gb(), "unit": "GB"})
        # The model workbench renders one row per Model record. Keep the
        # dashboard KPI on that exact source and counting rule as well.
        kpis[3].update({"value": _managed_model_count(), "unit": "个"})
        # Must stay identical to governance-process / process-0.
        kpis[4].update({"value": _process_task_count(), "unit": "个", "change_rate": 0})
        return success(
            data=kpis,
            message="首页指标查询成功",
        )

    if kind == "governance-process":
        # 处理任务指标直接从数据库 tasks 表聚合，避免前端维护一份独立快照。
        with SessionLocal() as db:
            tasks = list(
                db.scalars(
                    select(Task)
                    .where(Task.capability_code == PROCESS_CAPABILITY)
                    .order_by(Task.created_at.asc())
                ).all()
            )

        completed = [task for task in tasks if task.status in {"succeeded", "failed"}]
        running = sum(1 for task in tasks if task.status in {"pending", "running"})
        succeeded = sum(1 for task in completed if task.status == "succeeded")
        processed = sum(int(task.success_count or 0) for task in tasks)
        success_rate = round(succeeded * 100 / len(completed), 1) if completed else 0

        kpis = [
            {"id": "process-0", "label": "处理任务总数", "value": len(tasks), "unit": "个", "change_rate": 0, "icon": "Coin"},
            {"id": "process-1", "label": "正在运行", "value": running, "unit": "个", "change_rate": 0, "icon": "VideoPlay"},
            {"id": "process-2", "label": "累计处理量", "value": processed, "unit": "条次", "change_rate": 0, "icon": "Document"},
            {"id": "process-3", "label": "已结束任务成功率", "value": success_rate, "unit": "%", "change_rate": 0, "icon": "Shield", "succeeded_count": succeeded, "completed_count": len(completed)},
        ]
        return success(data=kpis, message="数据处理指标查询成功")

    if kind == "governance-value":
        tasks = _capability_tasks(VALUE_CAPABILITIES)
        results = [_result(task) for task in tasks]
        scores = [_number(item, "overall_score", "score") for item in results]
        analyzed = sum(_number(item, "valid_count", "sample_count", "analyzed_count") for item in results)
        high = sum(_number(item, "high_value_count", "high_count") for item in results)
        languages = {
            str((task.input_data or {}).get("language"))
            for task in tasks
            if (task.input_data or {}).get("language")
        }
        return success(
            data=[
                {"id": "value-0", "label": "综合价值评分", "value": round(sum(scores) / len(scores), 1) if scores else 0, "unit": "分", "change_rate": 0, "icon": "Trophy"},
                {"id": "value-1", "label": "高价值语料占比", "value": round(high * 100 / analyzed, 1) if analyzed else 0, "unit": "%", "change_rate": 0, "icon": "Document"},
                {"id": "value-2", "label": "已分析语料", "value": int(analyzed), "unit": "条", "change_rate": 0, "icon": "Coin"},
                {"id": "value-3", "label": "覆盖语种", "value": len(languages), "unit": "种", "change_rate": 0, "icon": "Position"},
            ],
            message="数据价值分析指标查询成功",
        )

    return success(
        data=[],
        message=f"暂未配置 {kind} 页面指标",
    )
