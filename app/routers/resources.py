from app.services.resource_service import create_new_dataset

from fastapi import APIRouter, HTTPException, Query
from app.repositories.user_repository import list_users
from app.repositories.role_repository import list_roles
from app.repositories.resource_repository import (
    create_dataset,
    create_model,
    find_resource,
    list_resources,
)
from app.services.resource_display import risk_alert_display_name

from fastapi import APIRouter, HTTPException

from app.domain.schemas import (
    CreateDatasetRequest,
    CreateModelRequest,
    UpdateDatasetRequest,
)

from sqlalchemy import func, select

from app.core.database import SessionLocal
from app.models.tables import Dataset, Model, ModelVersion, Task
from app.core.response import success
from app.repositories.audit_repository import add_log
from app.repositories.resource_repository import (
    create_dataset,
    create_model,
    find_resource,
    list_resources,
    update_dataset,
)


# 创建资源路由对象
router = APIRouter()


RISK_CATEGORY_LABELS = {
    "content_safety": "内容安全",
    "privacy": "隐私保护",
    "fraud": "网络诈骗",
    "unknown": "未分类",
}
RISK_ALERT_CAPABILITIES = ("semantic_risk", "anomaly_detect")


@router.get("/alerts")
def get_risk_alerts(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """
    返回“风险识别与分级”卡片所需的数据。

    数据来源：已执行的 semantic_risk 任务及其 result JSON。
    """
    condition = Task.capability_code.in_(RISK_ALERT_CAPABILITIES)

    with SessionLocal() as db:
        tasks = db.scalars(
            select(Task)
            .where(condition)
            .order_by(Task.created_at.desc())
        ).all()

        # 风险识别首页按任务名称展示，重复执行同名任务只保留最新一条。
        unique_tasks = []
        seen_names: set[str] = set()
        for task in tasks:
            task_name = risk_alert_display_name(task.name or "未命名任务")
            if task_name in seen_names:
                continue
            seen_names.add(task_name)
            unique_tasks.append(task)

        total = len(unique_tasks)
        tasks = unique_tasks[(page - 1) * page_size : page * page_size]

        items = []

        for task in tasks:
            result = task.result or {}
            risk_level = result.get("risk_level", "unknown")
            risk_category = result.get("risk_category", "content_safety")

            items.append(
                {
                    # 前端 ResourceTable 用到的字段
                    "id": task.task_id,
                    "name": risk_alert_display_name(task.name or "未命名任务"),
                    "category": (
                        "异常数据治理"
                        if task.capability_code == "anomaly_detect"
                        else RISK_CATEGORY_LABELS.get(risk_category, risk_category)
                    ),
                    "version": task.dataset_version or task.model_version or "v1.0.0",

                    # semantic_risk 已执行完成的任务展示为正常；
                    # 如任务还未完成，则显示“待处理”
                    "status": (
                        "normal"
                        if task.status == "succeeded"
                        else "pending"
                    ),

                    # 详情页和后续扩展可继续使用的字段
                    "task_id": task.task_id,
                    "risk_level": risk_level,
                    "risk_category": risk_category,
                    "confidence": result.get("confidence"),
                    "reason": result.get("reason", "暂无风险说明"),
                    "content": (task.input_data or {}).get("content", ""),
                    "created_at": (
                        task.created_at.isoformat()
                        if task.created_at
                        else None
                    ),
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
        message="风险识别记录查询成功",
    )

# 创建一个新的模拟数据集
@router.post("/datasets")
def create_dataset_api(request: CreateDatasetRequest):
    """
    路由只接收请求，并把业务处理交给服务层。
    """
    dataset = create_new_dataset(request)

    return success(
        data=dataset,
        message="数据集创建成功",
    )


# 查询全部数据集
@router.get("/datasets")
def get_datasets(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=100),
    keyword: str | None = None,
    modality: str | None = None,
    language: str | None = None,
    source_type: str | None = None,
    quality_status: str | None = None,
):
    datasets = list_resources("datasets")

    # 名称搜索
    if keyword:
        datasets = [
            item
            for item in datasets
            if keyword.lower() in item["name"].lower()
        ]

    # 数据类型筛选
    if modality:
        datasets = [
            item
            for item in datasets
            if modality in item.get("modalities", [])
        ]

    # 语言筛选
    if language:
        datasets = [
            item
            for item in datasets
            if language in item.get("languages", [])
        ]

    # 数据来源筛选
    if source_type:
        datasets = [
            item
            for item in datasets
            if item.get("source_type") == source_type
        ]

    # 质量状态筛选
    if quality_status:
        datasets = [
            item
            for item in datasets
            if item.get("quality_status") == quality_status
        ]

    total = len(datasets)
    start = (page - 1) * page_size
    end = start + page_size
    items = datasets[start:end]

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
        message="数据集列表查询成功",
    )


# 查询单个数据集详情
@router.get("/datasets/{dataset_id}")
def get_dataset_detail(dataset_id: int):
    dataset = find_resource("datasets", dataset_id)

    if dataset is None:
        raise HTTPException(status_code=404, detail="数据集不存在")

    return success(
        data=dataset,
        message="数据集详情查询成功",
    )


@router.get("/datasets/{dataset_id}/versions")
def get_dataset_versions(dataset_id: int):
    dataset = find_resource("datasets", dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="数据集不存在")
    return success(
        data={
            "items": [{
                "id": dataset["version_id"],
                "label": dataset["version_id"],
                "languages": dataset.get("languages", []),
            }],
            "total": 1,
            "page": 1,
            "page_size": 1,
            "total_pages": 1,
        },
        message="数据集版本查询成功",
    )


# 查询全部模型
@router.get("/models")
def get_models(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """
    查询模型列表，并按前端 PageResult 结构返回。
    """
    models = list_resources("models")

    total = len(models)
    start = (page - 1) * page_size
    end = start + page_size

    return success(
        data={
            "items": models[start:end],
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (
                (total + page_size - 1) // page_size
                if total
                else 0
            ),
        },
        message="模型列表查询成功",
    )

# 查询全部测试指标
@router.get("/metrics")
def get_metrics(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    metrics = list_resources("metrics")

    total = len(metrics)
    start = (page - 1) * page_size
    end = start + page_size

    return success(
        data={
            "items": metrics[start:end],
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (
                (total + page_size - 1) // page_size
                if total
                else 0
            ),
        },
        message="指标列表查询成功",
    )


@router.post("/models")
def create_model_api(
    request: CreateModelRequest,
):
    model = create_model(
        request.model_dump()
    )

    return success(
        data=model,
        message="模型注册成功",
    )

@router.get("/models/{model_id}")
def get_model_detail(model_id: int):
    model = find_resource(
        "models",
        model_id,
    )

    if model is None:
        raise HTTPException(
            status_code=404,
            detail="模型不存在",
        )

    with SessionLocal() as db:
        db_model = db.get(Model, model_id)
        versions = list(db.scalars(select(ModelVersion).where(ModelVersion.model_id == model_id).order_by(ModelVersion.created_at.desc())).all())
    if db_model is not None:
        model.update({"model_type": db_model.model_type, "source": db_model.source, "creator": db_model.creator, "service_url": db_model.service_url, "versions": [{"version": item.version, "created_at": item.created_at.isoformat(), "description": item.description, "task_id": item.task_id} for item in versions]})
    return success(
        data=model,
        message="模型详情查询成功",
    )

@router.patch("/datasets/{dataset_id}")
def update_dataset_api(
    dataset_id: int,
    request: UpdateDatasetRequest,
):
    update_data = request.model_dump(exclude_unset=True)

    dataset = update_dataset(
        dataset_id=dataset_id,
        data=update_data,
    )

    if dataset is None:
        raise HTTPException(
            status_code=404,
            detail="数据集不存在",
        )

    add_log(
        task_id=None,
        event_type="dataset_updated",
        request_data={
            "dataset_id": dataset_id,
            **update_data,
        },
        response_data=dataset,
    )

    return success(
        data=dataset,
        message="数据集更新成功",
    )


@router.delete("/datasets/{dataset_id}")
def delete_dataset_api(dataset_id: int):
    """Physically remove a dataset after the UI's explicit confirmation.

    Task and training history is intentionally retained for auditability.  Any
    historical lineage that referenced the removed dataset will surface as a
    missing source instead of silently rewriting history.
    """
    with SessionLocal() as db:
        dataset = db.get(Dataset, dataset_id)
        if dataset is None:
            raise HTTPException(status_code=404, detail="数据集不存在或已删除")
        deleted = {
            "id": dataset.id,
            "name": dataset.name,
            "version": dataset.version,
        }
        db.delete(dataset)
        add_log(
            task_id=None,
            event_type="dataset_deleted",
            request_data={"dataset_id": dataset_id},
            response_data=deleted,
            endpoint=f"/datasets/{dataset_id}",
            db=db,
        )
        db.commit()

    return success(data=None, message="数据集已删除")

@router.get("/users")
def get_users(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    keyword: str | None = None,
):
    result = list_users(
        page=page,
        page_size=page_size,
        keyword=keyword,
    )

    return success(
        data=result,
        message="用户列表查询成功",
    )
@router.get("/roles")
def get_roles(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    keyword: str | None = None,
):
    """
    角色权限列表。

    数据来自 MySQL 的 system_roles 表。
    """
    result = list_roles(
        page=page,
        page_size=page_size,
        keyword=keyword,
    )

    return success(
        data=result,
        message="角色权限列表查询成功",
    )


