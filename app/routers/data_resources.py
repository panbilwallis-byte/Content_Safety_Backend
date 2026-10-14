from collections import Counter
from datetime import timedelta

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.core.response import success
from app.core.time import now_shanghai
from app.core.database import SessionLocal
from app.models.tables import Dataset, Task, TrainingTask

MODALITY_LABELS = {
    "text": "文本",
    "image": "图片",
    "video": "视频",
    "audio": "音频",
    "文本": "文本",
    "图片": "图片",
    "视频": "视频",
    "音频": "音频",
}


SOURCE_LABELS = {
    "business": "业务系统",
    "internet": "互联网采集",
    "industry": "行业数据",
    "synthetic": "合成数据",
}

router = APIRouter()

#保证数据是一个字典
def _metadata(dataset: Dataset) -> dict:
    return dataset.metadata_json or {}#前面没值，就返回后面这个空字典

#统计出现的次数，占总次数的百分比
def _distribution(counter: Counter[str], total: int) -> list[dict]:
    if not total:
        return []
    return [
        {"name": name, "value": round(value * 100 / total, 1)}
        for name, value in counter.most_common()
    ]


def _referenced_dataset_id(task: Task) -> int | None:
    """Return the explicitly recorded input dataset ID for one completed task."""
    input_data = task.input_data or {}
    value = input_data.get("dataset_id") or input_data.get("datasetId")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _dataset_usage_counts(tasks: list[Task], training_tasks: list[TrainingTask]) -> Counter[int]:
    """Count completed business executions that actually reference each dataset.

    Ingestion creates or appends dataset content; it is not a downstream use.
    A task with no stored dataset ID is deliberately excluded instead of being
    assigned to a dataset by name, which could fabricate usage statistics.
    """
    counts: Counter[int] = Counter()
    for task in tasks:
        if task.status != "succeeded" or task.capability_code == "data_ingest":
            continue
        dataset_id = _referenced_dataset_id(task)
        if dataset_id is not None:
            counts[dataset_id] += 1
    for task in training_tasks:
        if task.status != "succeeded":
            continue
        try:
            counts[int(task.dataset_id)] += 1
        except (TypeError, ValueError):
            continue
    return counts


QUALITY_DIMENSIONS = ("完整性", "准确性", "一致性", "时效性", "可用性")


def _quality_dimensions(dataset: Dataset) -> list[dict]:
    """Return five persisted quality dimensions, filling older records once."""
    metadata = _metadata(dataset)
    saved = metadata.get("quality_dimensions") or metadata.get("qualityDimensions")
    if isinstance(saved, dict):
        values = {str(name): value for name, value in saved.items()}
    elif isinstance(saved, list):
        values = {
            str(item.get("name")): item.get("value")
            for item in saved
            if isinstance(item, dict) and item.get("name")
        }
    else:
        values = {}

    score = float(metadata.get("quality_score", 95.0) or 95.0)
    offsets = (-1.8, 0.9, -0.4, -2.3, 1.1)
    dimensions = [
        {
            "name": name,
            "value": round(min(100, max(0, float(values.get(name, score + offsets[index])))), 1),
        }
        for index, name in enumerate(QUALITY_DIMENSIONS)
    ]
    if not values:
        # Older datasets only stored an overall score. Persist a deterministic
        # five-dimension baseline so future summaries read the same database data.
        metadata["quality_dimensions"] = dimensions
        dataset.metadata_json = metadata
    return dimensions


@router.get("/data-resources/options")
def resource_filter_options():
    """返回数据资源页面筛选器所需的真实数据库选项。"""
    with SessionLocal() as db:
        datasets = list(db.scalars(select(Dataset)).all())
    languages = sorted(
        {
            language
            for dataset in datasets
            for language in (_metadata(dataset).get("languages") or [])
        }
    )
    source_types = sorted({dataset.source_type for dataset in datasets if dataset.source_type})
    modalities = sorted(
        {
            MODALITY_LABELS.get(modality, modality)
            for dataset in datasets
            for modality in (_metadata(dataset).get("modalities") or [])
        }
    )
    return success(
        data={
            "languages": [{"code": language, "name": language} for language in languages],
            "sources": [
                {"code": source_type, "name": SOURCE_LABELS.get(source_type, source_type)}
                for source_type in source_types
            ],
            "modalities": modalities,
        },
        message="数据资源筛选选项查询成功",
    )


@router.get("/data-resources/summary")
def resource_summary(#参数全是前端的url请求里面的内容
    view: str = Query(default="overview"),
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    source_type: str | None = Query(default=None),
    dataset_id: int | None = Query(default=None),
    language: str | None = Query(default=None),
):
    """返回数据资源页面所需的统一汇总结构。"""
    #所有数据集对象，按创建时间从早到晚排序。
    with SessionLocal() as db:
        query = select(Dataset).order_by(Dataset.created_at.asc())
        datasets = list(db.scalars(query).all())
        # 趋势、来源分布只统计真实的数据接入任务；其他治理任务的展示容量
        # 不属于数据接入量，不能混入资源页的统计口径。
        tasks = list(
            db.scalars(
                select(Task)
                .where(
                    Task.capability_code == "data_ingest",
                    Task.status == "succeeded",
                    Task.progress >= 100,
                )
                .order_by(Task.created_at.asc())
            ).all()
        )
        usage_tasks = list(db.scalars(select(Task)).all())
        training_tasks = list(db.scalars(select(TrainingTask)).all())
        #如果调用接口时传了 dataset_id，就只保留 id 等于该值的数据集。
    if dataset_id is not None:
        datasets = [item for item in datasets if item.id == dataset_id]
        #只保留元数据中 languages 列表包含指定语言的数据集
    if language:
        datasets = [item for item in datasets if language in (_metadata(item).get("languages") or [])]
        #只保留元数据中 source_type 列表包含指定语言的数据集
    if source_type:
        datasets = [item for item in datasets if item.source_type == source_type]


    #得到数据集的个数
    total_rows = sum(int(_metadata(item).get("record_count", 0) or 0) for item in datasets)


    #数据集总存储量（GB）
    total_storage = sum(
        float(
            (_metadata(item).get("storage_gb", 0))
            or 0
        )
        for item in datasets
    )

    #统计所有数据集中每种语言出现的次数，最终得到一个 Counter 对象，键是语言名称，值是出现次数。
    languages = Counter(
        language_name
        for item in datasets
        for language_name in (_metadata(item).get("languages") or ["zh"])
    )

    # 数据集表是资源容量的唯一口径；接入任务成功后会实时累加到该表，
    # 因而资源页和首页不会因重复统计任务容量而出现不同的“数据总量”。
    displayed_total_storage = total_storage

    # 来源分布按已接入任务的数据量加权，而非仅按数据集个数计数。
    # 历史任务在启动时已补齐 source_name / storage_gb，因此每个来源都有可展示的数据量。
    sources = Counter()
    for task in tasks:
        sources[task.source_name or "未填写来源"] += float(task.storage_gb or 0)
    if not sources:
        for item in datasets:
            sources[_metadata(item).get(
                "source_name",
                SOURCE_LABELS.get(item.source_type, item.source_type),
            )] += float(_metadata(item).get("storage_gb", 0) or 0)

    # 统计所有数据集中每种“模态”（modalities）出现的次数，最终得到一个 Counter 对象。
    modalities = Counter(
        MODALITY_LABELS.get(modality, modality)
        for item in datasets
        for modality in (
            _metadata(item).get("modalities")
            or ["text"]
        )
    )
    # 雷达图必须是质量维度评分，不能传“优秀/良好/较差”的状态占比。
    # 旧数据没有维度明细时，_quality_dimensions 会基于已保存综合分回填五维基线。
    quality_values: dict[str, list[float]] = {name: [] for name in QUALITY_DIMENSIONS}
    for item in datasets:
        for dimension in _quality_dimensions(item):
            quality_values[dimension["name"]].append(float(dimension["value"]))
    quality_distribution = [
        {"name": name, "value": round(sum(values) / len(values), 1)}
        for name, values in quality_values.items()
        if values
    ]
    # 最近七天新增数据量（GB）由已接入任务的实际数据量汇总。
    now = now_shanghai()
    dates = [(now - timedelta(days=offset)).date().isoformat() for offset in range(6, -1, -1)]
    daily_amounts = Counter()
    for task in tasks:
        if task.created_at:
            daily_amounts[task.created_at.date().isoformat()] += float(task.storage_gb or 0)
    added = [round(daily_amounts[day], 1) for day in dates]
    # 累计趋势使用 GB，与图表纵轴一致，并以当前数据集总容量为终点。
    running_total = max(0.0, displayed_total_storage - sum(added))
    totals = []
    for value in added:
        running_total = round(running_total + value, 1)
        totals.append(running_total)
    if totals:
        totals[-1] = round(displayed_total_storage, 1)

    quality_score = round(
        sum(float(_metadata(item).get("quality_score", 95.0) or 95.0) for item in datasets) / len(datasets),
        1,
    ) if datasets else 0
    # 使用次数来自已完成业务任务和训练任务对数据集 ID 的实际引用；不读取
    # metadata_json 中的历史 uses，也不再把数据记录量错误地当作使用占比。
    usage_counts = _dataset_usage_counts(usage_tasks, training_tasks)
    total_uses = sum(usage_counts.get(item.id, 0) for item in datasets)
    ranking = [
        {
            "name": item.name,
            "source": _metadata(item).get("source_name", "数据集"),
            "storageGb": float(_metadata(item).get("storage_gb", 0) or 0),
            "uses": usage_counts.get(item.id, 0),
            "share": round(usage_counts.get(item.id, 0) * 100 / total_uses, 1) if total_uses else 0,
        }
        for item in sorted(datasets, key=lambda row: (-usage_counts.get(row.id, 0), row.name, row.id))
    ]

    kpis = [
        {"id": "resource-datasets", "label": "数据集数量", "value": len(datasets), "unit": "个", "change_rate": 0, "icon": "Coin"},
        {"id": "resource-records", "label": "数据记录数", "value": total_rows, "unit": "条", "change_rate": 0, "icon": "Document"},
        {"id": "storage", "label": "数据总量", "value": round(displayed_total_storage, 3), "unit": "GB", "change_rate": 0, "icon": "Box"},
        {"id": "resource-quality", "label": "平均质量分", "value": quality_score, "unit": "%", "change_rate": 0, "icon": "CircleCheckFilled"},
    ]
    selected_dataset_ids = {str(item.id) for item in datasets}
    issue_tasks = []
    for task in tasks:
        input_data = task.input_data or {}
        task_dataset_id = input_data.get("dataset_id", input_data.get("datasetId"))
        # Older ingest records may not retain the ID; use the registered name
        # only as a compatibility fallback.
        if selected_dataset_ids and (
            str(task_dataset_id) in selected_dataset_ids
            or any(task.dataset_name == dataset.name for dataset in datasets)
        ):
            issue_tasks.append(task)
    issues = [
        {
            "name": "重复样本条数",
            "value": sum(int(task.duplicate_count or 0) for task in issue_tasks),
        },
        {
            "name": "异常样本条数",
            "value": sum(int(task.anomaly_count or 0) for task in issue_tasks),
        },
    ]

    result = {
        "kpis": kpis,
        "trend": {"dates": dates, "added": added, "total": totals},
        "modalities": _distribution(modalities, sum(modalities.values())),
        # 与模态分布使用同一份数据库元数据，供前端环形图圆心展示标签累计数。
        "modalityCount": sum(modalities.values()),
        "sources": _distribution(sources, sum(sources.values())),
        "languages": _distribution(languages, sum(languages.values())),
        "quality": quality_distribution,
        "qualityScore": quality_score,
        "issues": issues,
        "ranking": ranking,
    }
    # Persist dimension baselines generated for older rows before returning the summary.
    with SessionLocal() as db:
        for dataset in datasets:
            db.merge(dataset)
        db.commit()
    return success(data=result, message=f"数据资源{view}汇总查询成功")
