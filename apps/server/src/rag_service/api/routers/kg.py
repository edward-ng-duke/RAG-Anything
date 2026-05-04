"""``/v1/kg`` — flat entities / relations / chunks / stats over LightRAG.

Read-only inspection endpoints over LightRAG's flat tables
(``lightrag_vdb_entity``, ``lightrag_vdb_relation``, ``lightrag_doc_chunks``)
scoped to the caller's tenant. Every call delegates to
:mod:`rag_service.kg.repository`, which enforces the ``workspace = tenant_id``
predicate. Cross-tenant rows are unreachable.

Endpoints
---------
``GET /v1/kg/entities``
    Cursor-paginated list. Optional ``search`` substring filter on
    ``entity_name``. ``type`` is accepted for forward-compat with the AGE
    graph layer (Task 3.4); the flat VDB row does not carry an
    ``entity_type``, so the underlying repository ignores it today.

``GET /v1/kg/entities/{entity_id}``
    Single entity by id. 404 on miss / cross-tenant.

``GET /v1/kg/relations``
    Cursor-paginated list. Optional ``source`` / ``target`` exact-match
    filters on the entity *name* columns. ``type`` is accepted for
    forward-compat (see above).

``GET /v1/kg/chunks/{chunk_id}``
    Single chunk by id. 404 on miss / cross-tenant.

``GET /v1/kg/stats``
    Per-tenant ``{entities, relations, chunks}`` counts.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from rag_service.api.deps import current_tenant, get_db_session
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

router = APIRouter(prefix="/v1/kg", tags=["kg"])


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


@router.get("/entities", response_model=KGEntityListResponse)
async def list_entities(
    type: str | None = Query(default=None),  # noqa: A002 — kept "type" for client API
    search: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    tenant_id: str = Depends(current_tenant),
    db: AsyncSession = Depends(get_db_session),
) -> KGEntityListResponse:
    """List entities owned by ``tenant_id``.

    The ``type`` filter is forwarded for forward-compat — the flat VDB
    row has no ``entity_type`` column today, so the repository layer
    silently drops it. Type-aware queries land in the AGE/cypher layer.
    """
    result = await repository.list_entities(
        db,
        tenant_id,
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
    tenant_id: str = Depends(current_tenant),
    db: AsyncSession = Depends(get_db_session),
) -> KGEntity:
    """Return one entity row scoped to ``tenant_id``.

    Raises ``404`` when the row is absent *or* belongs to another
    tenant — we never leak existence across tenants.
    """
    row = await repository.get_entity(db, tenant_id, entity_id)
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
    tenant_id: str = Depends(current_tenant),
    db: AsyncSession = Depends(get_db_session),
) -> KGRelationListResponse:
    """List relations owned by ``tenant_id``.

    ``source`` / ``target`` are exact-match on the entity *name* columns
    LightRAG persists. ``type`` is accepted for forward-compat with the
    AGE layer (Task 3.4).
    """
    result = await repository.list_relations(
        db,
        tenant_id,
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
    tenant_id: str = Depends(current_tenant),
    db: AsyncSession = Depends(get_db_session),
) -> KGChunk:
    """Return one chunk row scoped to ``tenant_id``. 404 on miss."""
    row = await repository.get_chunk(db, tenant_id, chunk_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "chunk not found")
    return KGChunk(**row)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


@router.get("/stats", response_model=KGStats)
async def get_stats(
    tenant_id: str = Depends(current_tenant),
    db: AsyncSession = Depends(get_db_session),
) -> KGStats:
    """Return ``{entities, relations, chunks}`` counts for the tenant."""
    return KGStats(**(await repository.stats(db, tenant_id)))


# ---------------------------------------------------------------------------
# Row → wire-shape adapters
# ---------------------------------------------------------------------------


def _normalize_entity(row: dict) -> dict:
    """Project a repo row into the :class:`KGEntity` field set.

    ``id`` is coerced to ``str`` so callers see a stable string regardless
    of the underlying column type (Postgres ``text`` vs. SQLite ``TEXT``).
    Missing optional fields collapse to ``None`` rather than KeyError.
    """
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
# Graph traversal (Task 3.4) — k-hop neighbours / multi-source subgraph
# ---------------------------------------------------------------------------


@router.get("/entities/{entity_id}/neighbors", response_model=KGSubgraphResponse)
async def get_neighbors(
    entity_id: str,
    depth: int = Query(default=1, ge=1, le=3),
    tenant_id: str = Depends(current_tenant),
    db: AsyncSession = Depends(get_db_session),
) -> KGSubgraphResponse:
    """Return the k-hop neighbourhood of ``entity_id`` as ``{nodes, edges}``.

    ``depth`` is bounded to ``[1, 3]`` — wider traversals on a real KG
    explode quadratically and we'd rather force the caller to paginate
    via repeated 1-hop lookups than ship a footgun.
    """
    result = await graph_mod.neighbors(db, tenant_id, entity_id, depth=depth)
    return KGSubgraphResponse(
        nodes=[KGNode(**n) for n in result["nodes"]],
        edges=[KGEdge(**e) for e in result["edges"]],
    )


@router.get("/subgraph", response_model=KGSubgraphResponse)
async def get_subgraph(
    entities: str = Query(..., description="Comma-separated entity IDs"),
    depth: int = Query(default=2, ge=1, le=3),
    tenant_id: str = Depends(current_tenant),
    db: AsyncSession = Depends(get_db_session),
) -> KGSubgraphResponse:
    """Multi-source subgraph rooted at the comma-separated ``entities``.

    Reject empty input and lists longer than 50 IDs explicitly with a
    400 — the cypher layer would otherwise silently short-circuit or
    issue an unbounded query.
    """
    eids = [e.strip() for e in entities.split(",") if e.strip()]
    if not eids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no entity ids provided")
    if len(eids) > 50:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "max 50 entity ids")
    result = await graph_mod.subgraph(db, tenant_id, eids, depth=depth)
    return KGSubgraphResponse(
        nodes=[KGNode(**n) for n in result["nodes"]],
        edges=[KGEdge(**e) for e in result["edges"]],
    )
