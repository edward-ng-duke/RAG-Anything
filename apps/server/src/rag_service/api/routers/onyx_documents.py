"""``/v1/onyx/documents`` — document CRUD for the ONYX integration.

Mirrors the alpha ``/v1/documents`` + ``/v1/ingest`` endpoints with a
few targeted differences:

* ``tenant_id`` comes from the ``X-Onyx-KB-Id`` header via
  :func:`onyx_service_auth` (which also enforces the internal token,
  IP allowlist, and KB existence + ``source=onyx`` ownership). Alpha
  routers source it from the JWT.
* ``POST`` is a multipart upload (matches ``/v1/ingest``) and replies
  ``202 Accepted`` because the actual indexing work happens in the
  worker.
* ``DELETE`` is a soft-delete + ``rebuild_index`` enqueue, identical
  in spirit to the alpha behaviour.

The upload pipeline itself lives in
:func:`rag_service.services.ingest.perform_upload`; this router is a
thin caller. The list endpoint reuses the same keyset cursor format
as alpha (``base64(json({uploaded_at, document_id}))``) so client SDKs
can share cursor-handling code if they want to.
"""

from __future__ import annotations

import base64
import binascii
import datetime as _dt
import json
from uuid import UUID

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from rag_service.api.auth_onyx import OnyxCallContext, onyx_service_auth
from rag_service.api.deps import get_db_session
from rag_service.api.onyx_schemas import (
    OnyxDocumentListItem,
    OnyxDocumentListResponse,
    OnyxDocumentResponse,
)
from rag_service.config import settings
from rag_service.db.models import Document
from rag_service.services.ingest import perform_upload

router = APIRouter(prefix="/v1/onyx/documents", tags=["onyx-documents"])


# ---------------------------------------------------------------------------
# Cursor helpers
# ---------------------------------------------------------------------------


def _encode_cursor(uploaded_at: _dt.datetime, document_id: UUID) -> str:
    """Encode the keyset cursor as URL-safe base64-of-JSON."""
    payload = json.dumps(
        {
            "uploaded_at": uploaded_at.isoformat(),
            "document_id": str(document_id),
        }
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[_dt.datetime, UUID]:
    """Inverse of :func:`_encode_cursor`. 400 on malformed input."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
        uploaded_at = _dt.datetime.fromisoformat(data["uploaded_at"])
        document_id = UUID(data["document_id"])
    except (binascii.Error, ValueError, KeyError, json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid cursor")
    return uploaded_at, document_id


# ---------------------------------------------------------------------------
# Arq enqueue indirection (tests monkey-patch this)
# ---------------------------------------------------------------------------


async def enqueue_rebuild(tenant_id: str) -> None:
    """Enqueue ``rebuild_index`` for a KB.

    Lifted into a module-level helper so tests can replace it with a
    stub without spinning up Redis or arq. Production opens a fresh
    arq pool, enqueues, and immediately closes — fine for the
    request-scoped lifetime of a delete call.
    """
    pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    try:
        await pool.enqueue_job("rebuild_index", tenant_id)
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# POST /v1/onyx/documents — multipart upload
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=OnyxDocumentResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    ctx: OnyxCallContext = Depends(onyx_service_auth),
    db: AsyncSession = Depends(get_db_session),
) -> OnyxDocumentResponse:
    """Accept a multipart upload for the KB identified by ``X-Onyx-KB-Id``.

    Returns 202 once the document row is committed and the
    ``ingest_document`` arq task is enqueued. Quota / size / MIME /
    dedup decisions all happen inside
    :func:`rag_service.services.ingest.perform_upload`.

    ``ctx.kb_id`` is guaranteed non-null by
    :func:`onyx_service_auth` (the dep raises 400 / 404 otherwise),
    so the explicit ``str(...)`` guard below is purely defensive
    against future refactors.
    """
    assert ctx.kb_id is not None  # nosec — dep guarantees this
    result = await perform_upload(
        tenant_id=ctx.kb_id,
        file=file,
        db=db,
        request=request,
    )
    return OnyxDocumentResponse(
        document_id=str(result.document_id),
        job_id=str(result.job_id) if result.job_id else None,
        status=result.status,
        deduplicated=result.deduplicated,
        file_name=result.file_name,
        file_size=result.file_size,
        content_hash=result.content_hash,
        mime_type=result.mime_type,
    )


# ---------------------------------------------------------------------------
# GET /v1/onyx/documents — cursor-paginated list
# ---------------------------------------------------------------------------


@router.get("", response_model=OnyxDocumentListResponse)
async def list_documents(
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    status_filter: str | None = Query(default=None, alias="status"),
    ctx: OnyxCallContext = Depends(onyx_service_auth),
    db: AsyncSession = Depends(get_db_session),
) -> OnyxDocumentListResponse:
    """List documents in this KB.

    Pagination is keyset on ``(uploaded_at DESC, document_id DESC)``.
    The client passes the previous page's ``next_cursor`` back
    verbatim; the cursor encodes the ``(uploaded_at, document_id)``
    pair of the last row of that page so we can use a strict less-than
    predicate. This avoids the OFFSET pitfalls (skipped / duplicated
    rows) that LIMIT/OFFSET would have under concurrent inserts.
    """
    assert ctx.kb_id is not None
    stmt = select(Document).where(Document.tenant_id == ctx.kb_id)
    if status_filter is not None:
        stmt = stmt.where(Document.status == status_filter)

    if cursor is not None:
        last_uploaded_at, last_doc_id = _decode_cursor(cursor)
        stmt = stmt.where(
            or_(
                Document.uploaded_at < last_uploaded_at,
                and_(
                    Document.uploaded_at == last_uploaded_at,
                    Document.document_id < last_doc_id,
                ),
            )
        )

    # Fetch ``limit + 1`` so we can detect a next page without a
    # second COUNT query.
    stmt = stmt.order_by(
        Document.uploaded_at.desc(),
        Document.document_id.desc(),
    ).limit(limit + 1)

    rows = list((await db.execute(stmt)).scalars().all())
    next_cursor: str | None = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        if last.uploaded_at is not None:
            next_cursor = _encode_cursor(last.uploaded_at, last.document_id)

    items = [
        OnyxDocumentListItem(
            document_id=str(d.document_id),
            file_name=d.file_name,
            file_size=d.file_size,
            content_hash=d.content_hash,
            mime_type=d.mime_type,
            status=d.status,
            uploaded_at=d.uploaded_at,
            indexed_at=d.indexed_at,
            error_message=d.error_message,
        )
        for d in rows
    ]
    return OnyxDocumentListResponse(items=items, next_cursor=next_cursor)


# ---------------------------------------------------------------------------
# GET /v1/onyx/documents/{document_id}
# ---------------------------------------------------------------------------


@router.get("/{document_id}", response_model=OnyxDocumentListItem)
async def get_document(
    document_id: UUID,
    ctx: OnyxCallContext = Depends(onyx_service_auth),
    db: AsyncSession = Depends(get_db_session),
) -> OnyxDocumentListItem:
    """Fetch a single document row owned by the caller's KB.

    Raises 404 when the row does not exist *or* belongs to another
    KB — we never leak the existence of another KB's document.
    """
    assert ctx.kb_id is not None
    row = (
        await db.execute(
            select(Document).where(
                Document.document_id == document_id,
                Document.tenant_id == ctx.kb_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    return OnyxDocumentListItem(
        document_id=str(row.document_id),
        file_name=row.file_name,
        file_size=row.file_size,
        content_hash=row.content_hash,
        mime_type=row.mime_type,
        status=row.status,
        uploaded_at=row.uploaded_at,
        indexed_at=row.indexed_at,
        error_message=row.error_message,
    )


# ---------------------------------------------------------------------------
# DELETE /v1/onyx/documents/{document_id}
# ---------------------------------------------------------------------------


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: UUID,
    ctx: OnyxCallContext = Depends(onyx_service_auth),
    db: AsyncSession = Depends(get_db_session),
) -> Response:
    """Soft-delete the document and enqueue a KB rebuild.

    The actual physical purge (LightRAG working dir, vector store,
    etc.) is the ``rebuild_index`` worker's job — we just flip
    ``status`` to ``"deleted"`` and signal the worker. 204 on
    success, 404 when the row doesn't exist for this KB.
    """
    assert ctx.kb_id is not None
    result = await db.execute(
        update(Document)
        .where(
            Document.document_id == document_id,
            Document.tenant_id == ctx.kb_id,
        )
        .values(status="deleted")
        .returning(Document.document_id)
    )
    matched = result.scalar_one_or_none()
    if matched is None:
        # Row missing or belongs to another KB; both collapse to 404
        # so we never leak cross-KB existence.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")

    # Commit *before* enqueue so the worker doesn't race the request
    # and observe a still-pending status.
    await db.commit()

    # Enqueue is best-effort but important: surface a 500 on failure
    # so the caller knows to retry. Tests monkey-patch
    # :func:`enqueue_rebuild`.
    try:
        await enqueue_rebuild(ctx.kb_id)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "failed to enqueue rebuild",
        )

    return Response(status_code=status.HTTP_204_NO_CONTENT)
