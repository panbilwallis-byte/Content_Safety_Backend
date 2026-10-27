"""数据处理任务的真规则执行（#13-1.4）。

四条规则里三条现在真算：
- deduplicate：按内容指纹分组只留首条——纯清单筛选，不产生新行；
- normalize_encoding / normalize_text：NFKC 归一 + 空白收敛，被改的行派生新行（source_record_id 指回原件）；
- complete_fields：补 language/topic_label 默认值，同样派生新行。
输出版本经 register_version 落 dataset_versions，样本内容与清单全部可查；
文本级语义清洗仍属课题组算法范围，本模块不做假装有。
"""
from __future__ import annotations

import re
import unicodedata
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.tables import Dataset, DatasetRecord, DatasetVersionItem, Task
from app.services.dataset_version_service import register_version, resolve_version
from app.services.lineage_service import record_process_edges

TRANSFORM_RULES = {"normalize_text", "normalize_encoding"}


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip()


def _input_rows(db: Session, dataset: Dataset, version_ref, task_id: str):
    """输入清单：优先按解析出的版本 items；无版本记录时退回全部原件行。"""
    version = resolve_version(db, dataset.id, str(version_ref)) if version_ref else None
    if version is not None:
        record_ids = list(db.scalars(select(DatasetVersionItem.record_id)
                                     .where(DatasetVersionItem.version_id == version.version_id)
                                     .order_by(DatasetVersionItem.id)).all())
        rows = list(db.scalars(select(DatasetRecord).where(DatasetRecord.id.in_(record_ids))
                               .order_by(DatasetRecord.id)).all()) if record_ids else []
    else:
        rows = list(db.scalars(select(DatasetRecord)
                               .where(DatasetRecord.dataset_id == dataset.id,
                                      DatasetRecord.source_record_id.is_(None))
                               .order_by(DatasetRecord.id)).all())
    return version, rows


def apply_process_rules(db: Session, task: Task) -> dict | None:
    """执行清洗规则并登记输出版本；无法解析数据集或无输入样本时返回 None（不阻断任务）。"""
    input_data = task.input_data or {}
    dataset_id = input_data.get("dataset_id") or input_data.get("datasetId")
    try:
        dataset = db.get(Dataset, int(dataset_id))
    except (TypeError, ValueError):
        return None
    if dataset is None:
        return None
    version_ref = (input_data.get("version_id") or input_data.get("versionId")
                   or input_data.get("dataset_version_id") or input_data.get("datasetVersionId"))
    source_version, rows = _input_rows(db, dataset, version_ref, task.task_id)
    if not rows:
        return None

    rules = set(input_data.get("rules") or [])
    kept_rows = rows
    dropped = 0
    if "deduplicate" in rules:
        seen: set[str] = set()
        deduped = []
        for row in kept_rows:
            key = row.content_hash or str((row.payload or {}).get("text") or "")
            if key in seen:
                dropped += 1
                continue
            seen.add(key)
            deduped.append(row)
        kept_rows = deduped

    comparisons = []
    final_ids: list[int] = []
    derived_count = 0
    for row in kept_rows:
        payload = dict(row.payload or {})
        original_text = str(payload.get("text") or "")
        new_text = _normalize(original_text) if rules & TRANSFORM_RULES else original_text
        fields = []
        actions = []
        if new_text != original_text:
            fields.append({"name": "text", "before": original_text, "after": new_text})
            actions.append("编码与空白规范化")
        if "complete_fields" in rules:
            if not payload.get("language"):
                payload["language"] = "zh"
                fields.append({"name": "language", "before": "（缺失）", "after": "zh"})
                actions.append("补全语言字段")
            if not payload.get("topic_label"):
                payload["topic_label"] = "综合"
                fields.append({"name": "topic_label", "before": "（缺失）", "after": "综合"})
                actions.append("补全主题标签")
        if actions:
            derived = DatasetRecord(
                dataset_id=row.dataset_id, task_id=task.task_id,
                content_hash=sha256(new_text.encode("utf-8")).hexdigest(),
                source_record_id=row.id,
                payload={**payload, "text": new_text})
            db.add(derived)
            db.flush()
            final_ids.append(derived.id)
            derived_count += 1
            comparisons.append({
                # 前端会按该 ID 回查版本清单并逐字比对原文，必须使用真实记录号与完整文本。
                "id": str(row.id),
                "original": original_text,
                "processed": new_text,
                "actions": actions,
                "fields": fields,
            })
        else:
            final_ids.append(row.id)

    result = dict(task.result or {})
    output_label = result.get("pending_output_version") or result.get("output_version")
    version_info = None
    if output_label:
        summary = "去重 {}→{}".format(len(rows), len(kept_rows))
        if derived_count:
            summary += "；派生修正 {} 条".format(derived_count)
        out_version = register_version(
            db, dataset, str(output_label), final_ids,
            source_version_id=source_version.version_id if source_version else None,
            producing_task_id=task.task_id, change_summary=summary)
        record_process_edges(
            db, task_id=task.task_id,
            source_version_id=out_version.source_version_id,
            output_version_id=out_version.version_id,
            sample_count=out_version.sample_count,
            manifest_hash=out_version.manifest_hash,
            change_summary=summary)
        version_info = {"output_version_id": out_version.version_id,
                        "output_sample_count": out_version.sample_count}
    return {
        "comparisons": comparisons,
        "deduplicated_count": dropped,
        "derived_count": derived_count,
        "input_count": len(rows),
        **(version_info or {}),
    }
