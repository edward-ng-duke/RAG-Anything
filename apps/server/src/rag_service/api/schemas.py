"""Pydantic request/response schemas for the rag_service HTTP API.

This module is the single source of truth for the wire shape of every
``/v1/...`` endpoint. Keeping the schemas in one file (rather than
co-located with each router) makes it trivial for client-generation tools
(openapi → typescript / pydantic) to discover them and for reviewers to
audit the public surface in one read.

Later tasks will append additional models (search, list, status, ...)
alongside :class:`IngestResponse`.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class IngestResponse(BaseModel):
    """Response body for ``POST /v1/ingest``.

    Attributes
    ----------
    job_id:
        The ``jobs.job_id`` of the newly enqueued ingest job. When
        ``deduplicated=True`` no real job is created and a placeholder
        ``uuid4`` is returned so the client always sees a well-formed UUID.
    document_id:
        The ``documents.document_id`` the upload landed on. For dedup hits
        this is the *existing* row's id, not a freshly-allocated one.
    status:
        ``"queued"`` for a fresh upload, ``"dedup"`` when the upload
        collided with an existing document by ``(tenant_id, content_hash)``.
    deduplicated:
        ``True`` iff the upload was suppressed because the same content
        already existed for this tenant.
    """

    job_id: UUID
    document_id: UUID
    status: str
    deduplicated: bool


class JobResponse(BaseModel):
    """Response body for ``GET /v1/jobs/{job_id}``.

    Mirrors the ``jobs`` table columns the client cares about. ``progress``
    is the JSONB blob populated by the worker (``stage``, percentages, ...).
    The model is built from the ORM row directly via ``from_attributes``.
    """

    model_config = ConfigDict(from_attributes=True)

    job_id: UUID
    document_id: UUID | None
    job_type: str
    status: str
    progress: dict
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    retries: int


class DocumentResponse(BaseModel):
    """Response body for a single document row.

    Mirrors the slice of the ``documents`` table the client needs to render
    a list / detail view. Built directly from the ORM row via
    ``from_attributes``; the underlying ``storage_path`` is intentionally
    not exposed — clients only ever address documents by their UUID.
    """

    model_config = ConfigDict(from_attributes=True)

    document_id: UUID
    tenant_id: str
    file_name: str
    file_size: int | None
    content_hash: str
    mime_type: str | None
    status: str
    uploaded_at: datetime
    indexed_at: datetime | None
    error_message: str | None


class DocumentListResponse(BaseModel):
    """Response body for ``GET /v1/documents``.

    ``next_cursor`` is an opaque base64-encoded ``(uploaded_at, document_id)``
    tuple; clients pass it back verbatim in the ``cursor`` query param. A
    ``None`` value means "no more pages".
    """

    items: list[DocumentResponse]
    next_cursor: str | None
