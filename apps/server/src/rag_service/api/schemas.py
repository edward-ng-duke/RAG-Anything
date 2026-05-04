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


# ---------------------------------------------------------------------------
# Knowledge graph (Task 3.3) — flat entities / relations / chunks / stats
# ---------------------------------------------------------------------------


class KGEntity(BaseModel):
    """One entity row from LightRAG's flat ``lightrag_vdb_entity`` table.

    Mirrors the slice the client cares about. ``properties`` is reserved
    for future enrichment from the AGE graph layer (Task 3.4) — the flat
    VDB row alone doesn't carry typed properties, so it is ``None`` today.
    """

    id: str
    entity_name: str | None = None
    entity_type: str | None = None
    content: str | None = None
    file_path: str | None = None
    properties: dict | None = None


class KGEntityListResponse(BaseModel):
    """Cursor-paginated list of entities scoped to the caller's tenant."""

    items: list[KGEntity]
    next_cursor: str | None = None


class KGRelation(BaseModel):
    """One relation row from LightRAG's flat ``lightrag_vdb_relation`` table.

    LightRAG stores entity *names* (not surrogate keys) in
    ``source_id`` / ``target_id``; the names are surfaced verbatim.
    """

    id: str
    source_id: str | None = None
    target_id: str | None = None
    type: str | None = None
    content: str | None = None
    file_path: str | None = None


class KGRelationListResponse(BaseModel):
    """Cursor-paginated list of relations scoped to the caller's tenant."""

    items: list[KGRelation]
    next_cursor: str | None = None


class KGChunk(BaseModel):
    """One chunk row from LightRAG's flat ``lightrag_doc_chunks`` table."""

    id: str
    content: str | None = None
    full_doc_id: str | None = None
    chunk_order_index: int | None = None
    tokens: int | None = None
    file_path: str | None = None


class KGStats(BaseModel):
    """Per-tenant counts surfaced by ``GET /v1/kg/stats``."""

    entities: int
    relations: int
    chunks: int


# ---------------------------------------------------------------------------
# Knowledge graph (Task 3.4) — neighbours / subgraph traversal
# ---------------------------------------------------------------------------


class KGNode(BaseModel):
    """One node in a graph traversal result.

    ``label`` is AGE's vertex label (e.g. ``base``) or the LightRAG
    ``entity_type`` when available; ``properties`` is the verbatim
    ``properties`` dict the cypher layer surfaces. Both are ``None`` when
    the underlying agtype payload didn't carry them.
    """

    id: str
    label: str | None = None
    properties: dict | None = None


class KGEdge(BaseModel):
    """One edge in a graph traversal result.

    ``source`` / ``target`` are the LightRAG entity-ID-shaped endpoints
    when available (``properties.source_id`` / ``properties.target_id``)
    and fall back to AGE's numeric ``start_id`` / ``end_id`` otherwise.
    ``type`` mirrors the cypher edge label.
    """

    source: str | None = None
    target: str | None = None
    type: str | None = None
    properties: dict | None = None


class KGSubgraphResponse(BaseModel):
    """Response body for ``/v1/kg/entities/{id}/neighbors`` and ``/subgraph``.

    Nodes are de-duplicated by id; edges by ``(source, target, type)``.
    Both lists may be empty when the underlying graph is missing or AGE
    is not installed — see :func:`rag_service.kg.graph._run_traversal`.
    """

    nodes: list[KGNode]
    edges: list[KGEdge]


# ---------------------------------------------------------------------------
# Conversations (Task 4.4) — list / create / get / delete + SSE messages
# ---------------------------------------------------------------------------


class ConversationCreate(BaseModel):
    """Request body for ``POST /v1/conversations``.

    ``title`` is optional; when omitted the row's ``title`` is left ``NULL``
    and the client renders a friendly placeholder ("New chat").
    """

    title: str | None = None


class ConversationBrief(BaseModel):
    """One conversation row, used both in the list endpoint and as the
    "header" portion of the detail endpoint.

    Built directly from the ORM row via ``from_attributes`` so the router
    never has to manually unpack each column.
    """

    model_config = ConfigDict(from_attributes=True)

    conversation_id: UUID
    title: str | None
    created_at: datetime
    updated_at: datetime


class ConversationListResponse(BaseModel):
    """Response body for ``GET /v1/conversations``."""

    items: list[ConversationBrief]


class MessageResponse(BaseModel):
    """One message row in chronological order.

    ``sources`` is the JSON blob persisted by the orchestrator on the
    ``done`` event — typically ``{"sources": [...]}`` — and is ``None``
    for user-authored messages.
    """

    model_config = ConfigDict(from_attributes=True)

    message_id: UUID
    role: str
    content: str
    sources: dict | None
    created_at: datetime


class ConversationDetailResponse(BaseModel):
    """Response body for ``GET /v1/conversations/{conversation_id}``.

    Bundles the conversation header with its full ordered message list
    so a client can populate a chat view in one round-trip.
    """

    conversation: ConversationBrief
    messages: list[MessageResponse]


class SendMessageRequest(BaseModel):
    """Request body for ``POST /v1/conversations/{conversation_id}/messages``.

    Mirrors the retrieval knobs from :class:`QueryRequest` so the chat
    surface and one-shot ``/v1/query`` share a vocabulary.
    """

    content: str
    mode: str = "hybrid"
    top_k: int = 10
    vlm_enhanced: bool = False
