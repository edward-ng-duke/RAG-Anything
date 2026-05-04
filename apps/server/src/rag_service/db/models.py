"""Business-table ORM models for the rag_service control plane.

Mirrors the SQL schema in the alpha-product plan §"业务表":
  - tenants
  - documents
  - jobs
  - query_log

Models intentionally carry no business methods or __repr__ — they describe
data shape only. Migration management lives in alembic (next task).
"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Tenant(Base):
    __tablename__ = "tenants"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    storage_quota_mb: Mapped[int | None] = mapped_column(Integer, server_default=text("1024"))
    config_json: Mapped[dict | None] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )


class Document(Base):
    __tablename__ = "documents"

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
    )
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str | None] = mapped_column(Text)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    # status: pending | parsing | indexed | failed | deleted
    status: Mapped[str] = mapped_column(Text, nullable=False)
    uploaded_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    indexed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("tenant_id", "content_hash"),
        Index("documents_tenant_status_idx", "tenant_id", "status"),
    )


class Job(Base):
    __tablename__ = "jobs"

    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.document_id"),
    )
    # job_type: ingest | reindex | delete
    job_type: Mapped[str] = mapped_column(Text, nullable=False)
    # status: queued | running | done | failed
    status: Mapped[str] = mapped_column(Text, nullable=False)
    progress: Mapped[dict | None] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)
    retries: Mapped[int | None] = mapped_column(Integer, server_default=text("0"))

    __table_args__ = (
        Index(
            "jobs_tenant_status_created_idx",
            "tenant_id",
            "status",
            text("created_at DESC"),
        ),
    )


class QueryLog(Base):
    __tablename__ = "query_log"

    id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(always=True),
        primary_key=True,
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[str | None] = mapped_column(Text)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    token_in: Mapped[int | None] = mapped_column(Integer)
    token_out: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    created_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index(
            "query_log_tenant_created_idx",
            "tenant_id",
            text("created_at DESC"),
        ),
    )
