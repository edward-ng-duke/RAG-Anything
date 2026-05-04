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


class QueryRequest(BaseModel):
    """Request body for ``POST /v1/query``.

    ``mode`` selects LightRAG's retrieval strategy
    (``hybrid`` | ``local`` | ``global`` | ``naive`` | ``mix``); the router
    forwards the value verbatim and lets RAGAnything reject anything it does
    not recognise. ``vlm_enhanced`` toggles the multimodal-aware variant
    (``aquery_vlm_enhanced``) for tenants that have a VLM configured.
    """

    question: str
    mode: str = "hybrid"  # hybrid|local|global|naive|mix
    top_k: int = 10
    vlm_enhanced: bool = False


class QuerySource(BaseModel):
    """One retrieved chunk that contributed to the answer.

    Every field is optional because RAGAnything's source format varies by
    storage backend and retrieval mode — we surface what is present and
    leave gaps as ``None`` rather than synthesising values. ``modality``
    is one of ``text`` / ``image`` / ``table`` / ``equation`` for clients
    that want to render multimodal sources differently.
    """

    document_id: str | None = None
    file_name: str | None = None
    chunk_id: str | None = None
    score: float | None = None
    snippet: str | None = None
    modality: str | None = None  # text|image|table|equation


class QueryResponse(BaseModel):
    """Response body for ``POST /v1/query``.

    ``latency_ms`` measures wall-clock time spent in the RAG call (not the
    full request round-trip). ``tokens`` is whatever RAGAnything's result
    surfaced — typically ``{"in": int, "out": int, "cost_usd": float}`` —
    or ``None`` when the underlying provider didn't return usage info.
    """

    answer: str
    sources: list[QuerySource]
    latency_ms: int
    tokens: dict | None = None  # {"in": int, "out": int, "cost_usd": float}


class TenantInfoResponse(BaseModel):
    """Response body for ``GET /v1/tenants/me``.

    Surface a tenant's identity plus live storage accounting derived from
    the ``documents`` table. ``storage_used_mb`` sums ``file_size`` over
    non-deleted rows and is rounded to 2 decimal places — clients use it
    to render a quota bar without doing the conversion themselves.
    ``document_count`` likewise excludes soft-deleted rows.
    """

    tenant_id: str
    display_name: str
    storage_quota_mb: int
    storage_used_mb: float
    document_count: int


# ---------------------------------------------------------------------------
# Auth (Task 2.4) — signup / login / me
# ---------------------------------------------------------------------------


class SignupRequest(BaseModel):
    """Request body for ``POST /v1/auth/signup``.

    ``display_name`` is optional; when omitted the auto-provisioned tenant
    falls back to the local-part of the email.
    """

    email: str
    password: str
    display_name: str | None = None


class LoginRequest(BaseModel):
    """Request body for ``POST /v1/auth/login``."""

    email: str
    password: str


class UserInfo(BaseModel):
    """Public projection of a ``users`` row.

    ``password_hash`` and other internal columns are intentionally omitted.
    """

    model_config = ConfigDict(from_attributes=True)

    user_id: UUID
    email: str
    display_name: str | None


class TenantBrief(BaseModel):
    """One tenant the caller is a member of, plus their role on it."""

    tenant_id: str
    display_name: str
    role: str


class AuthTokens(BaseModel):
    """Response body for ``POST /v1/auth/signup`` and ``POST /v1/auth/login``.

    The access token is short-lived; the refresh token rotates on use.
    ``tenants`` is the list of memberships so the client can populate the
    tenant-switcher without a follow-up ``/v1/auth/me`` round-trip.
    """

    access_token: str
    refresh_token: str
    user: UserInfo
    tenants: list[TenantBrief]


class MeResponse(BaseModel):
    """Response body for ``GET /v1/auth/me``."""

    user: UserInfo
    tenants: list[TenantBrief]


# ---------------------------------------------------------------------------
# Auth (Task 2.5) — refresh / logout / select_tenant
# ---------------------------------------------------------------------------


class RefreshRequest(BaseModel):
    """Request body for ``POST /v1/auth/refresh``.

    The refresh token is sent in the body (rather than a header) so it can
    travel inside an HTTPS body and never leak via an access log.
    """

    refresh_token: str


class RefreshResponse(BaseModel):
    """Response body for ``POST /v1/auth/refresh``.

    Only the access token is rotated — the caller keeps using the same
    refresh token until it expires or the user logs out.
    """

    access_token: str


class SelectTenantRequest(BaseModel):
    """Request body for ``POST /v1/auth/select_tenant``.

    Switches the *active* tenant for the caller's session by minting a new
    access token with the chosen ``tenant`` claim. The caller must be a
    member of the requested tenant; non-members get a 403.
    """

    tenant_id: str


class SelectTenantResponse(BaseModel):
    """Response body for ``POST /v1/auth/select_tenant``.

    The new access token carries ``tenant=<tenant_id>`` so subsequent
    requests are scoped to the selected tenant.
    """

    access_token: str
    tenant_id: str
