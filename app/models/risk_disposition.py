from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.time import now_shanghai


class RiskDisposition(Base):
    __tablename__ = "risk_dispositions"

    result_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    record_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    input_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    original_level: Mapped[str] = mapped_column(String(16), nullable=False)
    original_category: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    decision: Mapped[str | None] = mapped_column(String(16))
    reviewed_level: Mapped[str | None] = mapped_column(String(16))
    treatment: Mapped[str | None] = mapped_column(String(16))
    replacement_text: Mapped[str | None] = mapped_column(Text)
    events: Mapped[list[dict]] = mapped_column(JSON, default=list, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_shanghai, nullable=False)
    published_version_id: Mapped[str | None] = mapped_column(String(64))
    publication_token: Mapped[str | None] = mapped_column(String(64))
