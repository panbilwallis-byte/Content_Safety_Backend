"""数据集版本清单服务（#13）：版本=对样本行的不可变引用集合，内容永不复制。

对外版本引用一律经 resolve_version 解析，同时接受 dsv_xxxxxx（主键）与
v1.0.0（标签），根治此前"dsv id 对上版本字符串"导致治理任务直接失败的漂移。
"""
from __future__ import annotations

import hashlib
import re

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.models.tables import (
    ComplianceLineageEdge,
    Dataset,
    DatasetRecord,
    DatasetVersion,
    DatasetVersionItem,
)


def next_version_id(db: Session) -> str:
    """清单卡主键：现有 dsv_ 编号的最大值 +1。

    不能用行数 +1——中间删过一条就会把已存在的编号再发一次，撞主键。
    """
    largest = max((int(row[4:]) for row in db.scalars(select(DatasetVersion.version_id)).all()
                   if row.startswith("dsv_") and row[4:].isdigit()), default=0)
    return "dsv_{:06d}".format(largest + 1)


def manifest_hash(pairs) -> str:
    """对 (record_id, content_hash) 有序集合求摘要：清单被篡改则哈希对不上。"""
    digest = hashlib.sha256()
    for record_id, content_hash in sorted(pairs):
        digest.update("{}:{}\n".format(record_id, content_hash or "").encode("utf-8"))
    return digest.hexdigest()


def register_version(db: Session, dataset: Dataset, label: str, record_ids=None, *,
                     source_version_id: str | None = None,
                     producing_task_id: str | None = None,
                     change_summary: str = "") -> DatasetVersion:
    """登记一个版本快照；(dataset_id,label) 已存在时原样返回（幂等）。

    record_ids=None 表示数据集当前全部行；给出列表则必须是本数据集的行，
    混入他集 ID 直接抛错——清单宁可失败也不静默丢样本。
    """
    existing = db.scalar(select(DatasetVersion).where(
        DatasetVersion.dataset_id == dataset.id, DatasetVersion.label == label))
    if existing is not None:
        return existing
    if record_ids is None:
        rows = db.execute(select(DatasetRecord.id, DatasetRecord.content_hash)
                          .where(DatasetRecord.dataset_id == dataset.id)
                          .order_by(DatasetRecord.id)).all()
    else:
        wanted = list(dict.fromkeys(record_ids))
        rows = db.execute(select(DatasetRecord.id, DatasetRecord.content_hash)
                          .where(DatasetRecord.dataset_id == dataset.id,
                                 DatasetRecord.id.in_(wanted))
                          .order_by(DatasetRecord.id)).all()
        if len(rows) != len(wanted):
            raise ValueError("版本清单包含不属于该数据集或不存在记录")
    version = DatasetVersion(
        version_id=next_version_id(db), dataset_id=dataset.id, label=label,
        source_version_id=source_version_id, producing_task_id=producing_task_id,
        change_summary=change_summary, sample_count=len(rows),
        manifest_hash=manifest_hash((row.id, row.content_hash) for row in rows))
    db.add(version)
    db.flush()
    db.add_all([DatasetVersionItem(version_id=version.version_id, record_id=row.id,
                                   content_hash=row.content_hash) for row in rows])
    db.flush()
    return version


def resolve_version(db: Session, dataset_id: int, version_ref: str) -> DatasetVersion | None:
    """按 dsv 主键或版本标签解析；找不到返回 None，由调用方决定报错口径。"""
    return db.scalar(select(DatasetVersion).where(
        DatasetVersion.dataset_id == dataset_id,
        (DatasetVersion.version_id == version_ref) | (DatasetVersion.label == version_ref)))


def create_ingest_version(db: Session, dataset: Dataset, task_id: str | None = None,
                          note: str | None = None) -> DatasetVersion:
    """为本次成功接入创建不可变的完整原始快照版本。

    改之前：每次接入都重写同一个原始版本的样本数、清单哈希和记录引用；旧版本
    会跟着新增数据变化，无法还原它在接入前的内容。
    改之后：保留已有版本不动，另建一个新原始版本；新版本的清单包含该数据集
    所有原始记录，所以 10 条旧记录加 1 条新记录会形成 11 条样本的完整快照。
    dataset_records 中的正文只保存一份，新版本通过 dataset_version_items 引用
    这些记录 ID 和内容指纹，不会复制正文。新版本号取最近原始版本末尾数字 +1；
    清洗任务产生的派生版本不参与这个编号。用户填写的说明保存在版本说明字段。
    """
    rows = db.execute(select(DatasetRecord.id, DatasetRecord.content_hash)
                      .where(DatasetRecord.dataset_id == dataset.id,
                             DatasetRecord.source_record_id.is_(None))
                      .order_by(DatasetRecord.id)).all()
    raw_versions = db.scalars(select(DatasetVersion).where(
        DatasetVersion.dataset_id == dataset.id,
        DatasetVersion.source_version_id.is_(None)
    ).order_by(DatasetVersion.created_at.desc(), DatasetVersion.version_id.desc())).all()
    if raw_versions:
        previous_label = raw_versions[0].label
    else:
        # 新建数据集尚无快照时，以数据集初始版本作为首个原始版本。
        previous_label = dataset.version or "v1.0.0"

    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", previous_label)
    if match:
        major, minor, patch = map(int, match.groups())
        label = "v{}.{}.{}".format(major, minor, patch + 1) if raw_versions else previous_label
    else:
        label = "v1.0.0"

    # 不能与同数据集已有派生版本重号；若冲突，继续递增末尾数字。
    used_labels = set(db.scalars(select(DatasetVersion.label).where(
        DatasetVersion.dataset_id == dataset.id)).all())
    while label in used_labels:
        match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", label)
        if not match:
            raise ValueError("无法为数据集生成不冲突的版本号")
        major, minor, patch = map(int, match.groups())
        label = "v{}.{}.{}".format(major, minor, patch + 1)

    summary = (note or "").strip() or ("接入任务 {}".format(task_id) if task_id else "原始接入快照")
    version = register_version(
        db, dataset, label, [row.id for row in rows],
        producing_task_id=task_id, change_summary=summary[:500])
    dataset.version = label
    return version


def purge_dataset_ledger(db: Session, dataset_id: int) -> None:
    """覆盖式重导前清空该数据集的版本清单与相关谱系边——当作从未接入过，不留指向空集的黑户。"""
    version_ids = [row.version_id for row in db.scalars(
        select(DatasetVersion).where(DatasetVersion.dataset_id == dataset_id)).all()]
    if version_ids:
        db.execute(delete(DatasetVersionItem).where(DatasetVersionItem.version_id.in_(version_ids)))
        db.execute(delete(ComplianceLineageEdge).where(or_(
            (ComplianceLineageEdge.from_type == "dataset_version")
            & ComplianceLineageEdge.from_id.in_(version_ids),
            (ComplianceLineageEdge.to_type == "dataset_version")
            & ComplianceLineageEdge.to_id.in_(version_ids))))
        db.execute(delete(DatasetVersion).where(DatasetVersion.version_id.in_(version_ids)))
    db.execute(delete(ComplianceLineageEdge).where(or_(
        (ComplianceLineageEdge.from_type == "dataset") & (ComplianceLineageEdge.from_id == str(dataset_id)),
        (ComplianceLineageEdge.to_type == "dataset") & (ComplianceLineageEdge.to_id == str(dataset_id)))))


def ensure_dataset_versions_backfill() -> None:
    """启动补登：为每个尚无任何版本行的数据集按当前版本标签登记原件快照。"""
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        changed = False
        for dataset in db.scalars(select(Dataset)).all():
            exists = db.scalar(select(DatasetVersion)
                               .where(DatasetVersion.dataset_id == dataset.id).limit(1))
            if exists is None:
                # 与 refresh_raw_version 同口径：启动补登只收原件行，清洗派生行不进原始快照 (#13)。
                raw_ids = list(db.scalars(select(DatasetRecord.id)
                               .where(DatasetRecord.dataset_id == dataset.id,
                                      DatasetRecord.source_record_id.is_(None))
                               .order_by(DatasetRecord.id)).all())
                register_version(db, dataset, dataset.version or "v1.0.0", raw_ids,
                                 change_summary="原始接入快照（启动补登）")
                changed = True
        if changed:
            db.commit()
