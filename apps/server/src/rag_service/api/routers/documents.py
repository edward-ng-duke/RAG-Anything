"""``/v1/documents`` — list / inspect / soft-delete tenant documents.

Read-mostly inspection endpoints scoped to the caller's tenant. Every
query filters on ``tenant_id`` so a client can never observe another
tenant's row — cross-tenant lookups are indistinguishable from a missing
row (both 404), which is the behaviour we want for tenant isolation.

Endpoints
---------
``GET /v1/documents``
    Cursor-paginated list. Optional ``status`` filter. Cursor is an
    opaque base64-encoded ``(uploaded_at, document_id)`` tuple from the
    last item of the previous page; we use it for keyset pagination so
    paging stays correct under concurrent writes.

``GET /v1/documents/{document_id}``
    Single document by id. 404 on miss / cross-tenant.

``DELETE /v1/documents/{document_id}``
    Soft-delete: flips ``status`` to ``"deleted"`` and enqueues the
    ``rebuild_index`` arq task so the worker can purge the document from
    the LightRAG working dir on its next pass. The actual disk + vector
    cleanup is the rebuild's job; the API just marks intent. 204 on
    success, 404 if the document doesn't exist for this tenant.
"""

from __future__ import annotations

import base64
import binascii
import datetime as _dt
import json
import uuid
from typing import Any

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from rag_service.api.deps import current_tenant, get_db_session
from rag_service.api.schemas import DocumentListResponse, DocumentResponse
from rag_service.config import settings
from rag_service.db.models import Document

router = APIRouter(prefix="/v1/documents", tags=["documents"])


# ---------------------------------------------------------------------------
# Cursor encode / decode
# ---------------------------------------------------------------------------


def _encode_cursor(uploaded_at: _dt.datetime, document_id: uuid.UUID) -> str:
    """Encode ``(uploaded_at, document_id)`` as an opaque base64 string.

    JSON inside base64 keeps the cursor URL-safe and trivially extensible
    (e.g. to add a sort direction later) without forcing a binary format.
    """
    payload = json.dumps(
        {
            "uploaded_at": uploaded_at.isoformat(),
            "document_id": str(document_id),
        }
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[_dt.datetime, uuid.UUID]:
    """Inverse of :func:`_encode_cursor`. Raises ``HTTPException(400)`` on bad input."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
        uploaded_at = _dt.datetime.fromisoformat(data["uploaded_at"])
        document_id = uuid.UUID(data["document_id"])
    except (binascii.Error, ValueError, KeyError, json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid cursor")
    return uploaded_at, document_id


# ---------------------------------------------------------------------------
# Arq enqueue indirection (tests monkey-patch this)
# ---------------------------------------------------------------------------


async def enqueue_rebuild(tenant_id: str) -> None:
    """Enqueue the ``rebuild_index`` arq task for ``tenant_id``.

    Lifted into a module-level helper so tests can replace it with a stub
    without spinning up Redis or arq. Production opens a fresh arq pool,
    enqueues, and immediately closes — fine for the request-scoped
    lifetime of a delete call.
    """
    pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    try:
        await pool.enqueue_job("rebuild_index", tenant_id)
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# GET /v1/documents
# ---------------------------------------------------------------------------


@router.get("", response_model=DocumentListResponse)
async def list_documents(
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    status_filter: str | None = Query(default=None, alias="status"),
    tenant_id: str = Depends(current_tenant),
    db: AsyncSession = Depends(get_db_session),
) -> DocumentListResponse:
    """List documents owned by ``tenant_id``, ordered by ``uploaded_at DESC``.

    Pagination is keyset: we order by ``(uploaded_at DESC, document_id DESC)``
    and use the last item of the previous page as a strict upper bound.
    This avoids the OFFSET pitfalls (skipped/duplicated rows under concurrent
    inserts) that LIMIT/OFFSET would have.
    """
    stmt = select(Document).where(Document.tenant_id == tenant_id)
    if status_filter is not None:
        stmt = stmt.where(Document.status == status_filter)

    if cursor is not None:
        last_uploaded_at, last_doc_id = _decode_cursor(cursor)
        # Strict keyset: rows strictly older than the cursor row, OR rows
        # with the same uploaded_at but a smaller document_id (stable
        # tie-breaker so duplicate timestamps don't drop rows).
        stmt = stmt.where(
            or_(
                Document.uploaded_at < last_uploaded_at,
                and_(
                    Document.uploaded_at == last_uploaded_at,
                    Document.document_id < last_doc_id,
                ),
            )
        )

    # Fetch ``limit + 1`` so we know whether a next page exists without a
    # second COUNT query.
    stmt = stmt.order_by(
        Document.uploaded_at.desc(),
        Document.document_id.desc(),
    ).limit(limit + 1)

    result = await db.execute(stmt)
    rows: list[Document] = list(result.scalars().all())

    next_cursor: str | None = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        # ``uploaded_at`` is server-defaulted but should always be populated
        # by the time the row is observable to a SELECT; guard anyway.
        if last.uploaded_at is not None:
            next_cursor = _encode_cursor(last.uploaded_at, last.document_id)

    items = [DocumentResponse.model_validate(r) for r in rows]
    return DocumentListResponse(items=items, next_cursor=next_cursor)


# ---------------------------------------------------------------------------
# GET /v1/documents/{document_id}
# ---------------------------------------------------------------------------


@router.get("/{document_id}", response_model=DocumentResponse)
async def get_document(
    document_id: uuid.UUID,
    tenant_id: str = Depends(current_tenant),
    db: AsyncSession = Depends(get_db_session),
) -> Document:
    """Return the document row owned by ``tenant_id`` with id ``document_id``.

    Raises ``404`` if the row does not exist *or* belongs to another
    tenant — we never leak the existence of another tenant's document.
    """
    result = await db.execute(
        select(Document).where(
            Document.document_id == document_id,
            Document.tenant_id == tenant_id,
        )
    )
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    return doc


# ---------------------------------------------------------------------------
# DELETE /v1/documents/{document_id}
# ---------------------------------------------------------------------------


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: uuid.UUID,
    tenant_id: str = Depends(current_tenant),
    db: AsyncSession = Depends(get_db_session),
) -> Response:
    """Soft-delete the document and enqueue a tenant rebuild.

    The actual physical purge (LightRAG working dir, vector store, etc.)
    is the ``rebuild_index`` worker's job. We just flip ``status`` to
    ``"deleted"`` and signal the worker.
    """
    stmt = (
        update(Document)
        .where(
            Document.document_id == document_id,
            Document.tenant_id == tenant_id,
        )
        .values(status="deleted")
        .returning(Document.document_id)
    )
    result = await db.execute(stmt)
    updated: Any = result.scalar_one_or_none()
    if updated is None:
        # Either the row doesn't exist or belongs to another tenant; both
        # collapse to 404 to avoid leaking existence.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")

    # Commit *before* enqueue so the worker doesn't race the request and
    # observe a still-pending row.
    await db.commit()

    await enqueue_rebuild(tenant_id)

    return Response(status_code=status.HTTP_204_NO_CONTENT)
