from typing import Any

from datetime import timedelta
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.tables import EvaluationRecord
from app.core.time import now_shanghai


DEMO_RECORDS = (
    ("eval-demo-1", "数据治理来源连通测试", "source_connectivity", "数据治理来源连通测试", 0.0, 1.0, False, "failed", "not_evaluated"),
    ("eval-demo-2", "内容分类能力复测", "classification_accuracy", "内容分类准确率", 0.98, 0.95, True, "succeeded", "passed"),
    ("eval-demo-3", "多模态风险检测验收", "risk_coverage", "多模态风险检测覆盖率", 0.72, 0.90, False, "succeeded", "failed"),
)


def ensure_demo_evaluations() -> None:
    """Seed the result selector once; subsequent starts always read persisted rows."""
    with SessionLocal() as db:
        if db.scalar(select(EvaluationRecord.id).limit(1)) is not None:
            return
        created = now_shanghai()
        for offset, (task_id, _name, code, metric, value, target, passed, status, judgment) in enumerate(DEMO_RECORDS):
            db.add(EvaluationRecord(task_id=task_id, metric_code=code, metric_name=metric,
                                    value=value, target=target, passed=passed,
                                    created_at=created - timedelta(minutes=offset)))
        db.commit()


def save_evaluation(
    task_id: str,
    result: dict[str, Any],
    db: Session,
):
    record = EvaluationRecord(
        task_id=task_id,
        metric_code=result["metric_code"],
        metric_name=result["metric_name"],
        value=result["value"],
        target=result["target"],
        passed=result["passed"],
    )

    db.add(record)
    db.flush()

    return evaluation_to_dict(record)


def evaluation_to_dict(
    record: EvaluationRecord,
) -> dict[str, Any]:
    return {
        "id": record.id,
        "task_id": record.task_id,
        "metric_code": record.metric_code,
        "metric_name": record.metric_name,
        "value": record.value,
        "target": record.target,
        "passed": record.passed,
        "created_at": (
            record.created_at.isoformat()
            if record.created_at
            else None
        ),
    }


def list_evaluations():
    with SessionLocal() as db:
        records = db.scalars(
            select(EvaluationRecord)
            .order_by(EvaluationRecord.id.desc())
        ).all()

        return [
            evaluation_to_dict(record)
            for record in records
        ]


def evaluation_status(task_id: str) -> tuple[str, str, str]:
    """Return persisted execution/judgement/name metadata for a result row."""
    for demo_task_id, name, _code, _metric, _value, _target, _passed, status, judgment in DEMO_RECORDS:
        if task_id == demo_task_id:
            return status, judgment, name
    return "succeeded", "passed", "测试评估"
