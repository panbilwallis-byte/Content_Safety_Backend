"""Execution state is separate from the immutable audit publications."""
from sqlalchemy import Boolean, DateTime, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base
from app.core.time import now_shanghai


class InternalAgentRun(Base):
    __tablename__ = "internal_agent_runs"
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[int] = mapped_column(Integer, index=True)
    request_key: Mapped[str] = mapped_column(String(100))
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    stop_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    config: Mapped[dict] = mapped_column(JSON)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[object] = mapped_column(DateTime, default=now_shanghai)
    finished_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (UniqueConstraint("owner_id", "request_key", name="uq_internal_agent_request"),)
