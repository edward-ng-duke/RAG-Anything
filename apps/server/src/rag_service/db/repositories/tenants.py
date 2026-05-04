"""Tenant repository — onyx-flavoured KB CRUD + cascade delete.

The ``/v1/onyx/*`` surface treats ``tenants`` rows whose ``config_json``
carries ``source == "onyx"`` as "knowledge bases" (KBs). These rows
co-exist with regular user-owned tenants in the same table; the
``source`` discriminator is the only way to tell them apart.

This module concentrates four operations the routers need:

* :func:`create_onyx_kb`  — insert a fresh KB row with an ``onyx-<uuid4>``
  primary key and an opinionated ``config_json`` blob.
* :func:`list_onyx_kbs`   — paginate over KB rows, optionally filtering
  by the originating ONYX workspace / owner.
* :func:`get_onyx_kb`     — return a single KB enriched with live storage
  + document-count aggregates (excludes soft-deleted documents).
* :func:`delete_kb_cascade` — wipe the KB across every sink: business
  tables, the 11 ``lightrag.*`` tables, and the on-disk upload + working
  directories.

Filtering on ``config_json`` is performed in Python rather than via
JSON-path SQL so the same code path works against PostgreSQL (JSONB) and
SQLite (JSON1) test environments. The pagination cursor is opaque
base64-JSON over ``(created_at, tenant_id)``; callers must not crack it
open.

Callers are responsible for committing the SQLAlchemy transaction. The
on-disk ``rmtree`` calls in :func:`delete_kb_cascade` happen *after* the
SQL deletes and use ``ignore_errors=True`` so a partial filesystem
state doesn't poison the transaction.
"""

from __future__ import annotations

import base64
import datetime as _dt
import json
import logging
import shutil
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from rag_service.db.models import Document, Job, QueryLog, Tenant


logger = logging.getLogger(__name__)


# The 11 LightRAG tables live under the ``lightrag`` PG schema and aren't
# mirrored as ORM models — LightRAG owns its schema and we only need to
# remove rows on KB deletion. Listing them as plain strings is the
# pragmatic option; if LightRAG ever adds another physical table we
# extend this tuple by hand.
LIGHTRAG_TABLES: tuple[str, ...] = (
    "lightrag_doc_full",
    "lightrag_doc_chunks",
    "lightrag_doc_status",
    "lightrag_entity_chunks",
    "lightrag_full_entities",
    "lightrag_full_relations",
    "lightrag_llm_cache",
    "lightrag_relation_chunks",
    "lightrag_vdb_chunks",
    "lightrag_vdb_entity",
    "lightrag_vdb_relation",
)


_LIST_LIMIT_MIN = 1
_LIST_LIMIT_MAX = 200


# ---------------------------------------------------------------------------
# Cursor codec
# ---------------------------------------------------------------------------


def _encode_cursor(created_at: _dt.datetime | None, tenant_id: str) -> str:
    """Base64-encode the ``(created_at, tenant_id)`` pair for next-page lookup.

    ``created_at`` may legitimately be ``None`` for rows that haven't yet
    rolled through the ``DEFAULT now()`` server-side default (test
    seeding); we serialize that as JSON ``null`` so the decoder round-trips
    it cleanly.
    """
    payload = {
        "created_at": created_at.isoformat() if created_at is not None else None,
        "tenant_id": tenant_id,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[_dt.datetime | None, str]:
    """Inverse of :func:`_encode_cursor`. Raises ``ValueError`` on malformed input."""
    raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
    payload = json.loads(raw.decode("utf-8"))
    ca_iso = payload.get("created_at")
    ca = _dt.datetime.fromisoformat(ca_iso) if ca_iso is not None else None
    return ca, payload["tenant_id"]


# ---------------------------------------------------------------------------
# create_onyx_kb
# ---------------------------------------------------------------------------


async def create_onyx_kb(
    db: AsyncSession,
    *,
    display_name: str,
    onyx_workspace_id: str | None = None,
    onyx_owner_user_id: str | None = None,
    storage_quota_mb: int = 1024,
) -> Tenant:
    """Insert and return a freshly minted ONYX KB ``Tenant`` row.

    The primary key is ``onyx-<uuid4>`` (full 36-char UUID4 with hyphens)
    so log scrapers can recognise ONYX-owned tenants at a glance. The
    ``config_json`` payload always carries ``source == "onyx"``; the
    optional workspace + owner fields are stored alongside in the same
    blob so a single fetch reconstitutes everything the auth layer
    + routers need.

    The caller is responsible for committing — letting the caller batch
    multiple inserts in one transaction is a deliberate choice.
    """
    tenant_id = f"onyx-{uuid.uuid4()}"
    config_json: dict[str, Any] = {"source": "onyx"}
    if onyx_workspace_id is not None:
        config_json["onyx_workspace_id"] = onyx_workspace_id
    if onyx_owner_user_id is not None:
        config_json["onyx_owner_user_id"] = onyx_owner_user_id

    tenant = Tenant(
        tenant_id=tenant_id,
        display_name=display_name,
        storage_quota_mb=storage_quota_mb,
        config_json=config_json,
    )
    db.add(tenant)
    await db.flush()
    return tenant


# ---------------------------------------------------------------------------
# list_onyx_kbs
# ---------------------------------------------------------------------------


def _is_onyx_kb(
    cfg: Any,
    *,
    workspace: str | None,
    owner: str | None,
) -> bool:
    """Return True iff ``cfg`` marks this tenant as an ONYX KB AND matches
    the optional workspace / owner filters."""
    if not isinstance(cfg, dict):
        return False
    if cfg.get("source") != "onyx":
        return False
    if workspace is not None and cfg.get("onyx_workspace_id") != workspace:
        return False
    if owner is not None and cfg.get("onyx_owner_user_id") != owner:
        return False
    return True


async def list_onyx_kbs(
    db: AsyncSession,
    *,
    cursor: str | None = None,
    limit: int = 50,
    onyx_workspace_id: str | None = None,
    onyx_owner_user_id: str | None = None,
) -> tuple[list[Tenant], str | None]:
    """Paginate ONYX KB rows ordered by ``(created_at DESC, tenant_id DESC)``.

    Filtering on ``config_json`` happens in Python rather than via JSON
    path expressions so the implementation stays portable across
    PostgreSQL (real env) and SQLite (test env). To compensate, the
    ``LIMIT`` is applied *after* the post-fetch filter — we over-fetch a
    moderate window keyed on a SQL-side cursor, drop non-onyx rows in
    Python, then trim to ``limit``. Over-fetching by ``limit*4`` is a
    pragmatic balance for the expected case where the great majority of
    rows in the table actually are ONYX-owned (this whole repo function
    only exists because the auth layer treats them differently).

    The returned ``next_cursor`` is opaque base64-JSON; ``None`` means
    "no more rows".
    """
    limit = max(_LIST_LIMIT_MIN, min(_LIST_LIMIT_MAX, int(limit)))

    # Resolve the SQL-side starting position from the cursor (if any).
    after_created_at: _dt.datetime | None = None
    after_tenant_id: str | None = None
    if cursor is not None:
        after_created_at, after_tenant_id = _decode_cursor(cursor)

    out: list[Tenant] = []
    # Loop in case post-filter drops most rows — keep paging through SQL
    # until we either fill the page or exhaust the table.
    sql_batch = max(limit * 4, 50)
    while len(out) < limit:
        stmt = select(Tenant).order_by(
            Tenant.created_at.desc(), Tenant.tenant_id.desc()
        ).limit(sql_batch)

        if after_created_at is not None or after_tenant_id is not None:
            # Strict tuple comparison: rows STRICTLY after the cursor in
            # the (created_at DESC, tenant_id DESC) ordering.
            stmt = stmt.where(
                (Tenant.created_at < after_created_at)
                | (
                    (Tenant.created_at == after_created_at)
                    & (Tenant.tenant_id < after_tenant_id)
                )
            )

        rows = (await db.execute(stmt)).scalars().all()
        if not rows:
            break

        for row in rows:
            if _is_onyx_kb(
                row.config_json,
                workspace=onyx_workspace_id,
                owner=onyx_owner_user_id,
            ):
                out.append(row)
                if len(out) >= limit:
                    break

        # Advance the SQL cursor regardless of post-filter outcome —
        # otherwise we'd loop forever on a batch full of non-onyx rows.
        last = rows[-1]
        after_created_at = last.created_at
        after_tenant_id = last.tenant_id

        if len(rows) < sql_batch:
            # SQL side exhausted; we've seen everything.
            break

    # Compute next_cursor: only meaningful if we filled the page AND
    # there might be more rows behind it. We probe with a 1-row lookahead.
    next_cursor: str | None = None
    if len(out) >= limit:
        last = out[-1]
        probe_stmt = (
            select(Tenant)
            .order_by(Tenant.created_at.desc(), Tenant.tenant_id.desc())
            .where(
                (Tenant.created_at < last.created_at)
                | (
                    (Tenant.created_at == last.created_at)
                    & (Tenant.tenant_id < last.tenant_id)
                )
            )
            .limit(1)
        )
        # We only emit a cursor if at least one further onyx-matching
        # row exists — but that requires another scan. Cheap approximation:
        # emit when ANY row exists past the boundary; the next call will
        # return ``([], None)`` if everything past the boundary fails the
        # source filter.
        probe = (await db.execute(probe_stmt)).scalars().first()
        if probe is not None:
            next_cursor = _encode_cursor(last.created_at, last.tenant_id)

    return out, next_cursor


# ---------------------------------------------------------------------------
# get_onyx_kb
# ---------------------------------------------------------------------------


async def get_onyx_kb(db: AsyncSession, kb_id: str) -> dict | None:
    """Return a flat info dict for the given KB, or ``None`` if no such KB.

    "No such KB" includes both genuinely missing rows AND rows whose
    ``config_json`` doesn't mark them as ONYX-owned — same cloak the
    auth dep uses, so non-onyx tenants don't leak through this surface
    either.

    The returned dict bundles the static columns with live aggregates
    (``storage_used_mb``, ``document_count``) computed over rows in the
    ``documents`` table. Both aggregates exclude soft-deleted rows
    (``status == "deleted"``) so the displayed numbers line up with the
    user-facing ``/v1/tenants/me`` endpoint.
    """
    row = (
        await db.execute(select(Tenant).where(Tenant.tenant_id == kb_id))
    ).scalar_one_or_none()
    if row is None:
        return None
    cfg = row.config_json
    if not isinstance(cfg, dict) or cfg.get("source") != "onyx":
        return None

    sum_size = (
        await db.execute(
            select(func.coalesce(func.sum(Document.file_size), 0)).where(
                Document.tenant_id == kb_id,
                Document.status != "deleted",
            )
        )
    ).scalar_one()
    count = (
        await db.execute(
            select(func.count(Document.document_id)).where(
                Document.tenant_id == kb_id,
                Document.status != "deleted",
            )
        )
    ).scalar_one()

    return {
        "tenant_id": row.tenant_id,
        "display_name": row.display_name,
        "storage_quota_mb": row.storage_quota_mb,
        "created_at": row.created_at,
        "onyx_workspace_id": cfg.get("onyx_workspace_id"),
        "onyx_owner_user_id": cfg.get("onyx_owner_user_id"),
        "storage_used_mb": round((sum_size or 0) / (1024 * 1024), 2),
        "document_count": count,
    }


# ---------------------------------------------------------------------------
# delete_kb_cascade
# ---------------------------------------------------------------------------


async def delete_kb_cascade(
    db: AsyncSession,
    kb_id: str,
    *,
    data_dir: Path,
) -> bool:
    """Best-effort delete of every artefact tied to ``kb_id``.

    Order of operations:

    1. ``documents``  — wipe before the parent so we can run with PG's
       FK either CASCADE-on-tenant or RESTRICT (the explicit DELETE
       works in either case).
    2. ``jobs``       — no FK on ``tenant_id``; plain DELETE.
    3. ``query_log``  — same; the table may not exist in some test envs
       so we tolerate a missing relation.
    4. ``lightrag.*`` (11 tables) — each DELETE is wrapped in a SAVEPOINT
       (via ``begin_nested``) so a missing table on SQLite doesn't poison
       the outer transaction. Real PG environments run all 11 cleanly.
    5. ``tenants``    — the parent row itself.
    6. Filesystem     — ``shutil.rmtree`` the upload + working directories.
       This happens AFTER the SQL deletes succeed locally; if the caller
       later rolls back, the files are gone but we accept that as a
       lesser evil than orphaned rows pointing at orphaned files. The
       ``ignore_errors=True`` flag absorbs the common "directory never
       existed" case.

    Returns ``True`` if a tenant row existed and was removed, ``False``
    otherwise. Caller commits the surrounding SQL transaction.
    """
    # Existence check FIRST so we have a clean signal for the return
    # value. We check the row only — non-onyx tenants are still removed
    # by this function if their id is passed; route-level guards (the
    # auth dep + verify_onyx_kb) are responsible for refusing to call
    # ``delete_kb_cascade`` on a non-onyx tenant.
    existing = (
        await db.execute(select(Tenant).where(Tenant.tenant_id == kb_id))
    ).scalar_one_or_none()
    if existing is None:
        return False

    # 1. documents
    await db.execute(delete(Document).where(Document.tenant_id == kb_id))

    # 2. jobs
    await db.execute(delete(Job).where(Job.tenant_id == kb_id))

    # 3. query_log — best-effort; the table may genuinely not exist in
    # SQLite test envs that didn't bring up the full schema.
    try:
        async with db.begin_nested():
            await db.execute(
                delete(QueryLog).where(QueryLog.tenant_id == kb_id)
            )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug("query_log delete skipped for %s: %s", kb_id, exc)

    # 4. lightrag.* — each table goes in its own SAVEPOINT so a missing
    # relation in SQLite (where the ``lightrag`` schema doesn't exist)
    # rolls back ONLY that statement, leaving the outer transaction
    # intact. In PG with the schema present, all 11 succeed.
    for tbl in LIGHTRAG_TABLES:
        try:
            async with db.begin_nested():
                await db.execute(
                    text(f'DELETE FROM lightrag."{tbl}" WHERE workspace = :ws'),
                    {"ws": kb_id},
                )
        except Exception as exc:  # noqa: BLE001 — defensive
            # ProgrammingError ("schema does not exist") in SQLite test
            # envs; OperationalError on stale schemas; both go to debug.
            logger.debug(
                "lightrag.%s delete skipped for %s: %s", tbl, kb_id, exc
            )

    # 5. tenants
    await db.execute(delete(Tenant).where(Tenant.tenant_id == kb_id))

    # 6. filesystem — unconditional, post-SQL, never raises.
    shutil.rmtree(Path(data_dir) / "uploads" / kb_id, ignore_errors=True)
    shutil.rmtree(
        Path(data_dir) / "working_dirs" / kb_id, ignore_errors=True
    )

    return True
