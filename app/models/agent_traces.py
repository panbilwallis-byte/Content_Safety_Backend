"""Persisted value-analysis audit projections. Source results are never rewritten."""
from datetime import datetime

from sqlalchemy import JSON, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.time import now_shanghai


class TraceBatch(Base):
    __tablename__ = "agent_trace_batches"
    batch_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_system: Mapped[str] = mapped_column(String(32))
    source_task_id: Mapped[str] = mapped_column(String(64))
    source_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    display_id: Mapped[str] = mapped_column(String(64))
    active_snapshot_id: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_shanghai, index=True)
    __table_args__ = (UniqueConstraint("source_system", "source_task_id", name="uq_trace_source"),)


class TraceSnapshot(Base):
    __tablename__ = "agent_trace_snapshots"
    snapshot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSON)


class TraceProjection(Base):
    __tablename__ = "agent_trace_projections"
    projection_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("snapshot_id", "version", name="uq_trace_projection"),)


class TraceSample(Base):
    __tablename__ = "agent_trace_samples"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(64))
    trace_id: Mapped[str] = mapped_column(String(64))
    sample_id: Mapped[str] = mapped_column(String(255))
    display_id: Mapped[str] = mapped_column(String(64))
    ingest_order: Mapped[int] = mapped_column(Integer)
    language: Mapped[str] = mapped_column(String(32))
    # Frozen original value output and a normalized display summary remain separate.
    result: Mapped[dict] = mapped_column(JSON)
    payload: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (
        UniqueConstraint("snapshot_id", "trace_id", name="uq_trace_sample"),
        Index("ix_trace_sample_order", "snapshot_id", "ingest_order", "trace_id"),
        Index("ix_trace_sample_source", "snapshot_id", "sample_id"),
    )


class TraceEvent(Base):
    __tablename__ = "agent_trace_events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(64))
    trace_id: Mapped[str] = mapped_column(String(64))
    event_id: Mapped[str] = mapped_column(String(64))
    ordinal: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (
        UniqueConstraint("snapshot_id", "trace_id", "event_id", name="uq_trace_event"),
        Index("ix_trace_event_order", "snapshot_id", "trace_id", "ordinal"),
    )


class TraceCall(Base):
    __tablename__ = "agent_trace_calls"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(64))
    trace_id: Mapped[str] = mapped_column(String(64))
    call_id: Mapped[str] = mapped_column(String(64))
    ordinal: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (
        UniqueConstraint("snapshot_id", "trace_id", "call_id", name="uq_trace_call"),
        Index("ix_trace_call_order", "snapshot_id", "trace_id", "ordinal"),
    )


class TraceEvidence(Base):
    __tablename__ = "agent_trace_evidence"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(64), index=True)
    projection_version: Mapped[str] = mapped_column(String(32))
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
    event_id: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)


class TraceRelation(Base):
    __tablename__ = "agent_trace_relations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(64), index=True)
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSON)


class TraceIngestCheckpoint(Base):
    __tablename__ = "agent_trace_ingest_checkpoints"
    source_key: Mapped[str] = mapped_column(String(100), primary_key=True)
    status: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_shanghai)


class TraceArtifact(Base):
    __tablename__ = "agent_trace_artifacts"
    artifact_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), index=True)
    owner_id: Mapped[str] = mapped_column(String(64))
    batch_id: Mapped[str] = mapped_column(String(64))
    snapshot_id: Mapped[str] = mapped_column(String(64))
    storage_name: Mapped[str] = mapped_column(String(100))
    filename: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(100))
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class TraceIdempotency(Base):
    __tablename__ = "agent_trace_idempotency"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload_hash: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class TraceAsset(Base):
    __tablename__ = "agent_trace_assets"
    file_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64))
    filename: Mapped[str] = mapped_column(String(255))
    storage_path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_shanghai)


class TraceBinding(Base):
    __tablename__ = "agent_trace_bindings"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(64), index=True)
    file_id: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("batch_id", "file_id", name="uq_trace_binding"),)


class TraceStatisticsSample(Base):
    __tablename__ = "agent_trace_statistics_samples"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(64))
    trace_id: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("snapshot_id", "trace_id", name="uq_trace_stats_sample"),)
