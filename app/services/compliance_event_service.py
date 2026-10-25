"""训练/推理环节合规事件的统一写入入口（GN-018）。

事件只追加不修改：人工处置等后续动作应作为新事件记录，
原始留痕不被覆盖，符合验收"缺失不得显示为已核验"的口径。
"""
from sqlalchemy.orm import Session

from app.core.time import now_shanghai
from app.models.tables import ComplianceEvent


def add_event(db: Session, *, subject_type: str, subject_id: str, event_type: str,
              level: str = "low", detail: str = "", training_task_id: str | None = None,
              trace_id: str | None = None) -> ComplianceEvent:
    event = ComplianceEvent(
        training_task_id=training_task_id,
        subject_type=subject_type,
        subject_id=str(subject_id),
        event_type=event_type,
        level=level,
        status="recorded",
        detail=detail,
        trace_id=trace_id,
        created_at=now_shanghai(),
    )
    db.add(event)
    return event
