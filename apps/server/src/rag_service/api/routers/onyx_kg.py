"""``/v1/onyx/kg`` — KG inspection endpoints for the ONYX integration.

Mirrors α's ``/v1/kg`` surface 1:1, except the per-request ``tenant_id``
is sourced from ``X-Onyx-KB-Id`` (via :func:`onyx_service_auth`) instead
of the JWT. Wraps the same :mod:`rag_service.kg.repository` and
:mod:`rag_service.kg.graph` modules and reuses the existing α schemas
unchanged so the OpenAPI export round-trips cleanly.

Endpoints
---------
``GET /v1/onyx/kg/entities``
    Cursor-paginated list with optional ``type`` / ``search`` filters.
``GET /v1/onyx/kg/entities/{entity_id}``
    Single entity by id; 404 on miss / cross-KB.
``GET /v1/onyx/kg/entities/{entity_id}/neighbors``
    k-hop neighbourhood (``depth`` ∈ [1, 3]).
``GET /v1/onyx/kg/relations``
    Cursor-paginated list with optional ``source`` / ``target`` /
    ``type`` filters.
``GET /v1/onyx/kg/chunks/{chunk_id}``
    Single chunk by id; 404 on miss / cross-KB.
``GET /v1/onyx/kg/stats``
    Per-KB ``{entities, relations, chunks}`` counts.
``GET /v1/onyx/kg/subgraph``
    Multi-source traversal rooted at comma-separated ``entities``
    (1-50 ids; ``depth`` ∈ [1, 3]).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from rag_service.api.auth_onyx import OnyxCallContext, onyx_service_auth
from rag_service.api.deps import get_db_session
from rag_service.api.schemas import (
    KGChunk,
    KGEdge,
    KGEntity,
    KGEntityListResponse,
    KGNode,
    KGRelation,
    KGRelationListResponse,
    KGStats,
    KGSubgraphResponse,
)
from rag_service.kg import graph as graph_mod
from rag_service.kg import repository

router = APIRouter(prefix="/v1/onyx/kg", tags=["onyx-kg"])


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


@router.get("/entities", response_model=KGEntityListResponse)
async def list_entities(
    type: str | None = Query(default=None),  # noqa: A002 — kept "type" for client API
    search: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    ctx: OnyxCallContext = Depends(onyx_service_auth),
    db: AsyncSession = Depends(get_db_session),
) -> KGEntityListResponse:
    """List entities owned by ``ctx.kb_id`` (alias of α ``list_entities``)."""
    result = await repository.list_entities(
        db,
        ctx.kb_id,
        type=type,
        search=search,
        cursor=cursor,
        limit=limit,
    )
    return KGEntityListResponse(
        items=[KGEntity(**_normalize_entity(r)) for r in result["items"]],
        next_cursor=result["next_cursor"],
    )


@router.get("/entities/{entity_id}", response_model=KGEntity)
async def get_entity(
    entity_id: str,
    ctx: OnyxCallContext = Depends(onyx_service_auth),
    db: AsyncSession = Depends(get_db_session),
) -> KGEntity:
    """Return one entity row scoped to ``ctx.kb_id``; 404 on miss / cross-KB."""
    row = await repository.get_entity(db, ctx.kb_id, entity_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "entity not found")
    return KGEntity(**_normalize_entity(row))


# ---------------------------------------------------------------------------
# Relations
# ---------------------------------------------------------------------------


@router.get("/relations", response_model=KGRelationListResponse)
async def list_relations(
    source: str | None = Query(default=None),
    target: str | None = Query(default=None),
    type: str | None = Query(default=None),  # noqa: A002 — kept "type" for client API
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    ctx: OnyxCallContext = Depends(onyx_service_auth),
    db: AsyncSession = Depends(get_db_session),
) -> KGRelationListResponse:
    """List relations owned by ``ctx.kb_id`` (alias of α ``list_relations``)."""
    result = await repository.list_relations(
        db,
        ctx.kb_id,
        source=source,
        target=target,
        type=type,
        cursor=cursor,
        limit=limit,
    )
    return KGRelationListResponse(
        items=[KGRelation(**_normalize_relation(r)) for r in result["items"]],
        next_cursor=result["next_cursor"],
    )


# ---------------------------------------------------------------------------
# Chunks
# ---------------------------------------------------------------------------


@router.get("/chunks/{chunk_id}", response_model=KGChunk)
async def get_chunk(
    chunk_id: str,
    ctx: OnyxCallContext = Depends(onyx_service_auth),
    db: AsyncSession = Depends(get_db_session),
) -> KGChunk:
    """Return one chunk row scoped to ``ctx.kb_id``; 404 on miss."""
    row = await repository.get_chunk(db, ctx.kb_id, chunk_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "chunk not found")
    return KGChunk(**row)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


@router.get("/stats", response_model=KGStats)
async def get_stats(
    ctx: OnyxCallContext = Depends(onyx_service_auth),
    db: AsyncSession = Depends(get_db_session),
) -> KGStats:
    """Return ``{entities, relations, chunks}`` counts for ``ctx.kb_id``."""
    return KGStats(**(await repository.stats(db, ctx.kb_id)))


# ---------------------------------------------------------------------------
# Row → wire-shape adapters (verbatim from α kg.py)
# ---------------------------------------------------------------------------


def _normalize_entity(row: dict) -> dict:
    """Project a repo row into the :class:`KGEntity` field set."""
    return {
        "id": str(row["id"]),
        "entity_name": row.get("entity_name"),
        "entity_type": row.get("entity_type"),
        "content": row.get("content"),
        "file_path": row.get("file_path"),
    }


def _normalize_relation(row: dict) -> dict:
    """Project a repo row into the :class:`KGRelation` field set."""
    return {
        "id": str(row["id"]),
        "source_id": row.get("source_id"),
        "target_id": row.get("target_id"),
        "type": row.get("type"),
        "content": row.get("content"),
        "file_path": row.get("file_path"),
    }


# ---------------------------------------------------------------------------
# Graph traversal — k-hop neighbours / multi-source subgraph
# ---------------------------------------------------------------------------


@router.get("/entities/{entity_id}/neighbors", response_model=KGSubgraphResponse)
async def get_neighbors(
    entity_id: str,
    depth: int = Query(default=1, ge=1, le=3),
    ctx: OnyxCallContext = Depends(onyx_service_auth),
    db: AsyncSession = Depends(get_db_session),
) -> KGSubgraphResponse:
    """k-hop neighbourhood of ``entity_id`` for ``ctx.kb_id``."""
    result = await graph_mod.neighbors(db, ctx.kb_id, entity_id, depth=depth)
    return KGSubgraphResponse(
        nodes=[KGNode(**n) for n in result["nodes"]],
        edges=[KGEdge(**e) for e in result["edges"]],
    )


@router.get("/subgraph", response_model=KGSubgraphResponse)
async def get_subgraph(
    entities: str = Query(..., description="Comma-separated entity IDs"),
    depth: int = Query(default=2, ge=1, le=3),
    ctx: OnyxCallContext = Depends(onyx_service_auth),
    db: AsyncSession = Depends(get_db_session),
) -> KGSubgraphResponse:
    """Multi-source subgraph rooted at comma-separated ``entities``.

    Empty input and lists longer than 50 IDs are explicitly rejected
    with a 400 — same shape as α's ``/v1/kg/subgraph``.
    """
    eids = [e.strip() for e in entities.split(",") if e.strip()]
    if not eids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no entity ids provided")
    if len(eids) > 50:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "max 50 entity ids")
    result = await graph_mod.subgraph(db, ctx.kb_id, eids, depth=depth)
    return KGSubgraphResponse(
        nodes=[KGNode(**n) for n in result["nodes"]],
        edges=[KGEdge(**e) for e in result["edges"]],
    )
