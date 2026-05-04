"""Pydantic models for /v1/onyx/* endpoints.

Single source of truth for wire shapes of the ONYX service-to-service
surface. ONYX team consumes the OpenAPI export of this surface to
generate their client SDK.

Later tasks (2.2 documents, 2.3 jobs, 2.4 query, 2.5 KG) will append
additional models alongside the KB models below.
"""

from __future__ import annotations
from datetime import datetime
from typing import Annotated
from pydantic import BaseModel, Field, StringConstraints


# --- KB lifecycle (Task 2.1) ---


class CreateKBRequest(BaseModel):
    """Body for POST /v1/onyx/kb."""
    display_name: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    onyx_workspace_id: Annotated[str, StringConstraints(max_length=64)] | None = None
    onyx_owner_user_id: Annotated[str, StringConstraints(max_length=128)] | None = None
    storage_quota_mb: int = Field(default=1024, ge=64, le=102400)


class KBBrief(BaseModel):
    """Item in GET /v1/onyx/kb list response."""
    kb_id: str
    display_name: str
    storage_quota_mb: int | None
    storage_used_mb: float
    document_count: int
    created_at: datetime


class KBListResponse(BaseModel):
    items: list[KBBrief]
    next_cursor: str | None = None


class KBDetail(BaseModel):
    """Response for POST /v1/onyx/kb (201) and GET /v1/onyx/kb/{kb_id} (200)."""
    kb_id: str
    display_name: str
    storage_quota_mb: int | None
    storage_used_mb: float
    document_count: int
    created_at: datetime
    onyx_workspace_id: str | None = None
    onyx_owner_user_id: str | None = None


# --- Documents (Task 2.2) ---


class OnyxDocumentResponse(BaseModel):
    """Response body for ``POST /v1/onyx/documents`` (multipart upload).

    Mirrors the alpha :class:`IngestResponse` shape with a few extra
    convenience fields the ONYX-side client wants (file_name / size /
    hash / mime) so it doesn't have to round-trip a follow-up GET.
    """

    document_id: str
    job_id: str | None
    status: str
    deduplicated: bool
    file_name: str
    file_size: int
    content_hash: str
    mime_type: str | None = None


class OnyxDocumentListItem(BaseModel):
    """One row in the document list / detail view for a KB."""

    document_id: str
    file_name: str
    file_size: int | None
    content_hash: str
    mime_type: str | None
    status: str
    uploaded_at: datetime | None
    indexed_at: datetime | None
    error_message: str | None = None


class OnyxDocumentListResponse(BaseModel):
    """Response body for ``GET /v1/onyx/documents``.

    ``next_cursor`` is an opaque base64-encoded
    ``(uploaded_at, document_id)`` keyset cursor; clients pass it back
    verbatim in the ``cursor`` query param. ``None`` means "no more
    pages".
    """

    items: list[OnyxDocumentListItem]
    next_cursor: str | None = None


# --- Jobs (Task 2.3) ---


class OnyxJobResponse(BaseModel):
    """Mirrors α's JobResponse but renamed to keep the onyx schema namespace."""
    job_id: str
    document_id: str | None
    job_type: str
    status: str
    progress: dict | None = None
    error_message: str | None = None
    created_at: datetime | None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    retries: int = 0


# --- Query (Task 2.4) ---


class OnyxHistoryMessage(BaseModel):
    """One turn of conversation history forwarded by ONYX in the request body.

    ``role`` is conventionally ``"user"`` or ``"assistant"``; we don't
    constrain the value because LightRAG passes the list through verbatim
    and any normalisation belongs at the upstream side.
    """

    role: str  # 'user' | 'assistant'
    content: str


class OnyxQueryRequest(BaseModel):
    """Request body for ``POST /v1/onyx/query`` and ``/v1/onyx/query/sync``.

    ONYX is the source of truth for chat history; we accept it in the
    body and never persist it. ``max_history_turns`` controls how many
    of the trailing messages are forwarded to RAGAnything — defaults to
    five so the prompt budget stays predictable across tenants.
    """

    question: Annotated[str, StringConstraints(min_length=1, max_length=4000)]
    history: list[OnyxHistoryMessage] = Field(default_factory=list, max_length=50)
    mode: str = "hybrid"  # hybrid | local | global | naive | mix
    top_k: int = Field(default=10, ge=1, le=50)
    vlm_enhanced: bool = False
    include_sources: bool = True
    max_history_turns: int = Field(default=5, ge=0, le=20)


class OnyxQuerySource(BaseModel):
    """One retrieved chunk surfaced in the SSE ``done`` event / sync response.

    Mirrors α's :class:`QuerySource` plus ``page`` / ``bbox`` for clients
    that want to render PDF citations. All fields are optional because
    RAGAnything's source shape varies by storage backend.
    """

    document_id: str | None = None
    file_name: str | None = None
    chunk_id: str | None = None
    score: float | None = None
    snippet: str | None = None
    modality: str | None = None
    page: int | None = None
    bbox: list[float] | None = None


class OnyxQuerySyncResponse(BaseModel):
    """Response body for the non-streaming ``/v1/onyx/query/sync`` endpoint."""

    request_id: str
    answer: str
    sources: list[OnyxQuerySource]
    latency_ms: int
    tokens: dict[str, int] | None = None
    warnings: list[str] = Field(default_factory=list)
