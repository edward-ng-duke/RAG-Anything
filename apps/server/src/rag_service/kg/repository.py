"""Async repository over LightRAG's flat KG tables.

LightRAG persists entities/relations/chunks in tables that are not part of
our ORM (``LIGHTRAG_VDB_ENTITY``, ``LIGHTRAG_VDB_RELATION``,
``LIGHTRAG_DOC_CHUNKS``). This module exposes read-only helpers built on
SQLAlchemy ``text()`` so the ``/v1/kg/*`` routes can list and inspect a
tenant's graph without forcing those tables into ``db.models``.

Tenant isolation
----------------
Every query filters on ``workspace = :ws`` where ``:ws`` is the caller's
``tenant_id``. There is no code path that issues a query without that
predicate — cross-tenant rows are unreachable.

Pagination
----------
``list_*`` helpers use opaque base64-encoded cursors carrying the last
``id`` of the previous page. We fetch ``limit + 1`` rows so we can decide
whether a next page exists without a second ``COUNT`` round-trip.

Schema portability
------------------
LightRAG places its tables in the ``lightrag`` schema in production. SQLite
(used by the unit tests) does not support cross-schema references the same
way, so the schema prefix is exposed as the module-level :data:`TABLE_PREFIX`
which tests monkeypatch to ``""``.

Deviations from the design doc
------------------------------
* ``LIGHTRAG_VDB_ENTITY`` has no ``entity_type`` column (LightRAG only
  stores ``entity_type`` on AGE graph nodes, not on the VDB row). The
  ``list_entities`` helper therefore does **not** accept a ``type`` filter.
  Type-aware queries belong in the AGE/cypher layer (Task 3.2).
"""

from __future__ import annotations

import base64
import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# Schema prefix — tests override to "" so unqualified SQLite tables work.
# ---------------------------------------------------------------------------

TABLE_PREFIX = "lightrag."


def _t(name: str) -> str:
    """Return the fully-qualified table name for the current ``TABLE_PREFIX``.

    Resolved at call time (not import time) so tests that monkeypatch
    :data:`TABLE_PREFIX` see the new value without re-importing the module.
    """
    return f"{TABLE_PREFIX}{name}"


# ---------------------------------------------------------------------------
# Cursor helpers
# ---------------------------------------------------------------------------


def _encode_cursor(payload: dict[str, Any]) -> str:
    """Pack ``payload`` into an opaque URL-safe base64 cursor.

    JSON-in-base64 keeps the wire shape stable while letting us extend the
    payload (e.g. add a sort direction) without breaking older clients.
    """
    return base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> dict[str, Any]:
    """Inverse of :func:`_encode_cursor`. Returns ``{}`` for malformed input.

    A malformed cursor degrades to "no cursor" instead of 400 so a stale
    bookmark from an old client just restarts from the first page rather
    than hard-failing the request.
    """
    try:
        decoded = base64.urlsafe_b64decode(cursor.encode("ascii"))
        data = json.loads(decoded.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — opaque cursor; any failure means "ignore"
        return {}


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


async def list_entities(
    db: AsyncSession,
    tenant_id: str,
    *,
    search: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Return ``{items, next_cursor}`` for entities owned by ``tenant_id``.

    ``search`` does a case-insensitive substring match on ``entity_name``
    using ``lower(col) LIKE lower(:p)`` so it works on both Postgres and
    SQLite (Postgres' native ``ILIKE`` would fail on SQLite).
    """
    limit = max(1, min(limit, 200))
    where = ["workspace = :ws"]
    params: dict[str, Any] = {"ws": tenant_id, "lim": limit + 1}

    if search:
        where.append("lower(entity_name) LIKE lower(:search)")
        params["search"] = f"%{search}%"

    if cursor:
        c = _decode_cursor(cursor)
        if "id" in c:
            where.append("id > :after_id")
            params["after_id"] = c["id"]

    sql = (
        "SELECT id, entity_name, content, file_path "
        f"FROM {_t('lightrag_vdb_entity')} "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY id ASC LIMIT :lim"
    )
    result = await db.execute(text(sql), params)
    rows = list(result.mappings().all())

    next_cursor: str | None = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = _encode_cursor({"id": rows[-1]["id"]})

    return {
        "items": [dict(r) for r in rows],
        "next_cursor": next_cursor,
    }


async def get_entity(
    db: AsyncSession,
    tenant_id: str,
    entity_id: str,
) -> dict[str, Any] | None:
    """Return one entity row or ``None`` if absent / cross-tenant.

    ``content_vector`` is included so callers can expose embeddings to
    debug/inspection tooling. Trim it at the route layer if the caller
    doesn't need it on the wire.
    """
    sql = (
        "SELECT id, entity_name, content, file_path, content_vector "
        f"FROM {_t('lightrag_vdb_entity')} "
        "WHERE workspace = :ws AND id = :id"
    )
    result = await db.execute(text(sql), {"ws": tenant_id, "id": entity_id})
    row = result.mappings().first()
    return dict(row) if row else None


async def get_entity_names_by_ids(
    db: AsyncSession,
    tenant_id: str,
    entity_ids: list[str],
) -> dict[str, str]:
    """Map ``ent-…`` surrogate ids to lightrag's ``entity_name`` strings.

    The KG VDB table (``lightrag_vdb_entity``) stores entities under
    sha-derived ``ent-<hash>`` surrogate ids in ``id`` and the
    human-readable name in ``entity_name``. The AGE graph, however,
    keys vertices on the *entity_name* in the ``entity_id`` property.
    Callers (graph traversal) need the name; clients carry surrogate
    ids. This helper does the small lookup.

    Missing ids are silently dropped from the result — the caller
    decides whether to 404 or fall back. Empty input short-circuits.
    """
    if not entity_ids:
        return {}
    sql = (
        "SELECT id, entity_name "
        f"FROM {_t('lightrag_vdb_entity')} "
        "WHERE workspace = :ws AND id = ANY(:ids)"
    )
    result = await db.execute(
        text(sql), {"ws": tenant_id, "ids": list(entity_ids)}
    )
    return {
        r["id"]: r["entity_name"]
        for r in result.mappings().all()
        if r["entity_name"]
    }


# ---------------------------------------------------------------------------
# Relations
# ---------------------------------------------------------------------------


async def list_relations(
    db: AsyncSession,
    tenant_id: str,
    *,
    source: str | None = None,
    target: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Return ``{items, next_cursor}`` for relations owned by ``tenant_id``.

    ``source`` / ``target`` are exact-match on ``source_id`` / ``target_id``
    (LightRAG stores entity *names* in those columns, not surrogate keys).
    """
    limit = max(1, min(limit, 200))
    where = ["workspace = :ws"]
    params: dict[str, Any] = {"ws": tenant_id, "lim": limit + 1}

    if source:
        where.append("source_id = :src")
        params["src"] = source
    if target:
        where.append("target_id = :tgt")
        params["tgt"] = target

    if cursor:
        c = _decode_cursor(cursor)
        if "id" in c:
            where.append("id > :after_id")
            params["after_id"] = c["id"]

    sql = (
        "SELECT id, source_id, target_id, content, file_path "
        f"FROM {_t('lightrag_vdb_relation')} "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY id ASC LIMIT :lim"
    )
    result = await db.execute(text(sql), params)
    rows = list(result.mappings().all())

    next_cursor: str | None = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = _encode_cursor({"id": rows[-1]["id"]})

    return {
        "items": [dict(r) for r in rows],
        "next_cursor": next_cursor,
    }


# ---------------------------------------------------------------------------
# Chunks
# ---------------------------------------------------------------------------


async def get_chunk(
    db: AsyncSession,
    tenant_id: str,
    chunk_id: str,
) -> dict[str, Any] | None:
    """Return one chunk row or ``None`` if absent / cross-tenant."""
    sql = (
        "SELECT id, content, full_doc_id, chunk_order_index, tokens, file_path "
        f"FROM {_t('lightrag_doc_chunks')} "
        "WHERE workspace = :ws AND id = :id"
    )
    result = await db.execute(text(sql), {"ws": tenant_id, "id": chunk_id})
    row = result.mappings().first()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


async def stats(db: AsyncSession, tenant_id: str) -> dict[str, int]:
    """Return per-tenant counts ``{entities, relations, chunks}``.

    A missing table (e.g. tenant has never ingested, so LightRAG never ran
    its bootstrap DDL) collapses to ``0`` instead of bubbling a 500. The
    failing query also rolls back the surrounding transaction on Postgres,
    so we issue each ``COUNT`` in its own ``begin_nested`` savepoint to
    keep one missing table from poisoning the others.
    """
    counts: dict[str, int] = {}
    for label, table in (
        ("entities", _t("lightrag_vdb_entity")),
        ("relations", _t("lightrag_vdb_relation")),
        ("chunks", _t("lightrag_doc_chunks")),
    ):
        try:
            async with db.begin_nested():
                result = await db.execute(
                    text(f"SELECT COUNT(*) AS c FROM {table} WHERE workspace = :ws"),
                    {"ws": tenant_id},
                )
                row = result.mappings().first()
            counts[label] = int(row["c"]) if row else 0
        except Exception:  # noqa: BLE001 — table-missing is the expected failure
            counts[label] = 0
    return counts
