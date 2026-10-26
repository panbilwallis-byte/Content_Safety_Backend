"""谱系边台账（#13-1.6）：动作真实发生时，把"谁生了谁"追加进 compliance_lineage_edges。

边主键由端点与关系共同决定，同一关系重复登记只留一条——重启补登、任务重跑都不会翻倍。
谱系页仍按活数据现算校验状态（缺口要当场判），这张表记的是"当时确实登记过"，
供验收（XN-008）按 起点→关系→终点 三元组计数与导出。
端点未在资源库登记时不补造边：缺就让它缺，由谱系页报缺口，而不是在台账里假装接上。
"""
from __future__ import annotations

from hashlib import sha1

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.tables import (
    ComplianceLineageEdge,
    Dataset,
    ModelCall,
    ModelVersion,
    TrainingTask,
)
from app.services.dataset_version_service import resolve_version

RELATION_INGEST = "接入产出"
RELATION_PROCESS_INPUT = "输入数据引用"
RELATION_PROCESS_OUTPUT = "输出版本登记"
RELATION_DERIVED_FROM = "清洗派生"
RELATION_TRAINING_INPUT = "训练数据绑定"
RELATION_TRAINING_OUTPUT = "训练产物登记"
RELATION_MODEL_CALL = "模型调用"

RELATIONS = (
    RELATION_INGEST,
    RELATION_PROCESS_INPUT,
    RELATION_PROCESS_OUTPUT,
    RELATION_DERIVED_FROM,
    RELATION_TRAINING_INPUT,
    RELATION_TRAINING_OUTPUT,
    RELATION_MODEL_CALL,
)


def edge_id(from_type: str, from_id: object, to_type: str, to_id: object, relation: str) -> str:
    key = "{}:{}>{}:{}#{}".format(from_type, from_id, to_type, to_id, relation)
    return "edge_" + sha1(key.encode("utf-8")).hexdigest()[:16]


def record_edge(
    db: Session,
    from_type: str,
    from_id: object,
    to_type: str,
    to_id: object,
    relation: str,
    payload: dict | None = None,
) -> ComplianceLineageEdge | None:
    """幂等写入一条谱系边；任一端点为空时返回 None（不写半截边）。"""
    if from_id in (None, "") or to_id in (None, ""):
        return None
    identifier = edge_id(from_type, from_id, to_type, to_id, relation)
    row = db.get(ComplianceLineageEdge, identifier)
    if row is not None:
        return row
    row = ComplianceLineageEdge(
        id=identifier,
        from_type=from_type,
        from_id=str(from_id),
        to_type=to_type,
        to_id=str(to_id),
        payload={"relation": relation, **(payload or {})},
    )
    db.add(row)
    db.flush()
    return row


def dataset_version_ref(db: Session, dataset_id: object, version_ref: object) -> str | None:
    """把数据集版本引用（dsv 主键或版本标签）解析成清单卡主键；未登记返回 None。"""
    if dataset_id is None or not version_ref:
        return None
    try:
        key = int(dataset_id)
    except (TypeError, ValueError):
        return None
    version = resolve_version(db, key, str(version_ref))
    return version.version_id if version is not None else None


def model_version_ref(db: Session, model_id: object, version: object) -> str | None:
    """按 (模型, 版本号) 找到已登记的模型版本行主键；未登记返回 None。"""
    if model_id is None or not version:
        return None
    try:
        key = int(model_id)
    except (TypeError, ValueError):
        return None
    row = db.scalar(select(ModelVersion).where(
        ModelVersion.model_id == key, ModelVersion.version == str(version)))
    return str(row.id) if row is not None else None


def record_ingest_edge(db: Session, *, task_id: str, dataset: Dataset) -> None:
    """接入成功：任务 → 数据集，以及任务 → 滚动后的原始清单快照。"""
    record_edge(db, "task", task_id, "dataset", dataset.id, RELATION_INGEST,
                payload={"dataset_name": dataset.name, "version": dataset.version})
    version = resolve_version(db, dataset.id, dataset.version or "v1.0.0")
    if version is not None and version.source_version_id is None:
        record_edge(db, "task", task_id, "dataset_version", version.version_id,
                    RELATION_PROCESS_OUTPUT,
                    payload={"sample_count": version.sample_count,
                             "manifest_hash": version.manifest_hash,
                             "label": version.label})


def record_process_edges(db: Session, *, task_id: str, source_version_id: str | None,
                         output_version_id: str, sample_count: int, manifest_hash: str,
                         change_summary: str = "") -> None:
    """清洗任务成功登记输出版本后的三条边。"""
    if source_version_id:
        record_edge(db, "dataset_version", source_version_id, "task", task_id,
                    RELATION_PROCESS_INPUT)
        record_edge(db, "dataset_version", source_version_id, "dataset_version",
                    output_version_id, RELATION_DERIVED_FROM,
                    payload={"task_id": task_id, "change_summary": change_summary})
    record_edge(db, "task", task_id, "dataset_version", output_version_id,
                RELATION_PROCESS_OUTPUT,
                payload={"sample_count": sample_count, "manifest_hash": manifest_hash})


def record_training_edges(db: Session, task: TrainingTask, version: ModelVersion) -> None:
    """训练成功：绑定的数据清单 → 训练任务 → 产出的模型版本。"""
    version_ref = dataset_version_ref(db, task.dataset_id, task.dataset_version)
    if version_ref is not None:
        record_edge(db, "dataset_version", version_ref, "training_task", task.id,
                    RELATION_TRAINING_INPUT,
                    payload={"dataset_id": task.dataset_id,
                             "dataset_version": task.dataset_version})
    record_edge(db, "training_task", task.id, "model_version", version.id,
                RELATION_TRAINING_OUTPUT,
                payload={"model_id": version.model_id, "version": version.version})


def record_call_edge(db: Session, call: ModelCall) -> None:
    """真实推理调用：产出它的模型版本 → 这条调用流水。"""
    record_edge(db, "model_version",
                model_version_ref(db, call.model_id, call.version),
                "model_call", call.id, RELATION_MODEL_CALL,
                payload={"model_id": call.model_id, "version": call.version,
                         "risk_level": call.risk_level, "trace_id": call.trace_id})


def ensure_lineage_edges_backfill() -> None:
    """启动补登：把库里已经登记过的真实关系写成边（record_edge 幂等，重启不翻倍）。"""
    from app.core.database import SessionLocal
    from app.models.tables import Dataset, DatasetVersion, Task

    with SessionLocal() as db:
        for task in db.scalars(select(Task).where(Task.capability_code == "data_ingest")).all():
            if task.status != "succeeded":
                continue
            data = task.input_data or {}
            result = task.result or {}
            dataset_id = (result.get("dataset_id") or result.get("datasetId")
                          or data.get("dataset_id") or data.get("datasetId"))
            try:
                dataset = db.get(Dataset, int(dataset_id)) if dataset_id is not None else None
            except (TypeError, ValueError):
                dataset = None
            if dataset is not None:
                record_ingest_edge(db, task_id=task.task_id, dataset=dataset)

        for output_version in db.scalars(select(DatasetVersion).where(
                DatasetVersion.producing_task_id.isnot(None))):
            task = db.scalar(select(Task).where(Task.task_id == output_version.producing_task_id))
            if task is None or task.status != "succeeded":
                continue
            if task.capability_code == "data_ingest":
                continue  # 接入任务已由 record_ingest_edge 登记，避免同一版本重复挂两条产出边
            record_process_edges(
                db, task_id=task.task_id,
                source_version_id=output_version.source_version_id,
                output_version_id=output_version.version_id,
                sample_count=output_version.sample_count,
                manifest_hash=output_version.manifest_hash,
                change_summary=output_version.change_summary)

        training_tasks = {task.id: task for task in db.scalars(select(TrainingTask)).all()}
        for version in db.scalars(select(ModelVersion).where(ModelVersion.task_id.isnot(None))):
            task = training_tasks.get(version.task_id)
            if task is None or task.status != "succeeded":
                continue
            record_training_edges(db, task, version)

        for call in db.scalars(select(ModelCall)).all():
            record_call_edge(db, call)
        db.commit()


def ledger_summary(db: Session) -> dict:
    """按关系聚合三元组计数，供验收核对与页面自查。"""
    counts: dict[str, int] = {relation: 0 for relation in RELATIONS}
    total = 0
    for (payload,) in db.execute(select(ComplianceLineageEdge.payload)).all():
        total += 1
        relation = (payload or {}).get("relation")
        if relation in counts:
            counts[relation] += 1
    return {"total": total, "byRelation": counts}
