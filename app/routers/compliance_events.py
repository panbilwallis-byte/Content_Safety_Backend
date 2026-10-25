"""训练中合规事件查询接口（GN-018）。

前端契约（src/api/compliance-events.ts）：
  GET /api/v1/compliance-events/overview?training_task_id=xxx
  GET /api/v1/compliance-events?training_task_id=xxx&page=1&page_size=10

汇总口径与前端样例自洽测试一致：
  高风险 = level=high 且状态仍为 recorded；
  已处置 = blocked/handled/ignored，或低风险记录（低风险记录即"已确认无需干预"）。
"""
from math import ceil

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.response import success
from app.models.tables import ComplianceEvent

router = APIRouter()

CLOSED_STATUSES = {"blocked", "handled", "ignored"}


def _row(event: ComplianceEvent) -> dict:
    return {
        "id": str(event.id),
        "time": event.created_at.isoformat(),
        "event_type": event.event_type,
        "level": event.level,
        "status": event.status,
        "detail": event.detail,
    }


def _events(db: Session, training_task_id: str | None) -> list[ComplianceEvent]:
    statement = select(ComplianceEvent).order_by(
        ComplianceEvent.created_at.desc(), ComplianceEvent.id.desc())
    if training_task_id:
        statement = statement.where(ComplianceEvent.training_task_id == training_task_id)
    return list(db.scalars(statement).all())


@router.get("/compliance-events/overview")
def compliance_events_overview(training_task_id: str | None = Query(None),
                               db: Session = Depends(get_db)):
    events = _events(db, training_task_id)
    high_risk = sum(1 for e in events if e.level == "high" and e.status == "recorded")
    handled = sum(1 for e in events
                  if e.status in CLOSED_STATUSES or (e.level == "low" and e.status == "recorded"))
    return success({
        "summary": {"total": len(events), "high_risk": high_risk, "handled": handled},
        "recent_events": [_row(e) for e in events[:5]],
    })


@router.get("/compliance-events")
def list_compliance_events(training_task_id: str | None = Query(None),
                           page: int = Query(1, ge=1),
                           page_size: int = Query(10, ge=1, le=100),
                           db: Session = Depends(get_db)):
    events = _events(db, training_task_id)
    total = len(events)
    start = (page - 1) * page_size
    return success({
        "items": [_row(e) for e in events[start:start + page_size]],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": ceil(total / page_size) if total else 0,
    })
