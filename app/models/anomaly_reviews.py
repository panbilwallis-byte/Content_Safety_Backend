from datetime import datetime

from sqlalchemy import JSON, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.time import now_shanghai


class AnomalyReview(Base):
    __tablename__ = "anomaly_reviews"
    __table_args__ = (
        Index("ix_anomaly_review_scope", "dataset_id", "version_id", "owner_digest", "scheme_id", "language"),
    )

    candidate_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    result_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    owner_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_id: Mapped[int] = mapped_column(Integer, nullable=False)
    version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scheme_id: Mapped[str] = mapped_column(String(64), nullable=False)
    language: Mapped[str] = mapped_column(String(24), nullable=False)
    record_id: Mapped[int] = mapped_column(Integer, nullable=False)
    input_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    before_label: Mapped[str] = mapped_column(String(255), nullable=False)
    after_label: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    events: Mapped[list[dict]] = mapped_column(JSON, default=list, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_shanghai, nullable=False)
    published_version_id: Mapped[str | None] = mapped_column(String(64))
    publication_token: Mapped[str | None] = mapped_column(String(64))
