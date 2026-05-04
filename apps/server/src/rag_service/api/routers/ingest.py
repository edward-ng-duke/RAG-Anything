"""``POST /v1/ingest`` — multipart file upload + queued indexing.

End-to-end flow:

1. Authenticate the request (bearer token) and resolve ``tenant_id`` via
   the ``current_tenant`` dependency.
2. Stream the multipart ``file=`` body to disk under
   ``data_dir/uploads/<tenant>/<doc_uuid>.<ext>`` while computing a SHA-256
   incrementally — we never buffer the full upload in memory.
3. Enforce ``settings.max_upload_mb`` *after* the write (we can't know the
   size up-front for chunked transfers); on overflow, delete the file and
   return ``413``.
4. Validate the MIME type by sniffing the first 512 bytes of the file we
   just wrote. We support PDF / DOC / DOCX / PNG / JPEG / plain text.
   Unrecognised → delete + ``415``.
5. Dedup against ``(tenant_id, content_hash)`` — on hit, delete the new
   file and return ``IngestResponse(deduplicated=True, ...)`` pointing at
   the existing document.
6. Insert ``documents`` (status ``pending``) and ``jobs`` (status
   ``queued``) rows in a single transaction.
7. Enqueue the ``ingest_document`` arq task for the background worker via
   the small :func:`enqueue_ingest` indirection (tests monkey-patch it).
8. Return ``IngestResponse(status="queued", deduplicated=False, ...)``.

The arq pool is created per-request rather than held in app state because
this is the ingest endpoint's only Redis user — keeping it inline avoids a
wider deps-module change. The pool is always closed in a ``finally``.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rag_service.api.deps import current_tenant
from rag_service.api.schemas import IngestResponse
from rag_service.config import settings
from rag_service.core.paths import document_upload_path, tenant_upload_dir
from rag_service.db import models as _models
from rag_service.db.session import get_db_session

router = APIRouter(prefix="/v1", tags=["ingest"])


# ---------------------------------------------------------------------------
# MIME sniffing
# ---------------------------------------------------------------------------


# Allowed MIME types and their canonical extensions. We accept the first
# match — order doesn't matter because each magic signature is unambiguous
# at offset 0.
_MIME_PDF = "application/pdf"
_MIME_PNG = "image/png"
_MIME_JPEG = "image/jpeg"
_MIME_DOC = "application/msword"
_MIME_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_MIME_TEXT = "text/plain"


def _sniff_mime(head: bytes) -> tuple[str | None, str | None]:
    """Detect MIME type + canonical extension from the first 512 bytes.

    Uses ``python-magic`` if importable, else a small magic-byte table. We
    *prefer* ``python-magic`` because it correctly tells DOCX (zip) apart
    from a generic zip archive; without it we fall back to the cruder
    "starts with PK\\x03\\x04" rule and rely on the upload context to be
    a Word document. If sniffing yields a type outside the allow-list,
    ``(None, None)`` is returned so the caller can produce a clean ``415``.

    Returns
    -------
    (mime_type, extension) or (None, None) when unrecognised.
    """
    if not head:
        return (None, None)

    # PDF: ``%PDF-``
    if head.startswith(b"%PDF-"):
        return (_MIME_PDF, "pdf")
    # PNG: 89 50 4E 47 0D 0A 1A 0A
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return (_MIME_PNG, "png")
    # JPEG: FF D8 FF
    if head.startswith(b"\xff\xd8\xff"):
        return (_MIME_JPEG, "jpg")
    # DOC (CFB / OLE2): D0 CF 11 E0 A1 B1 1A E1
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return (_MIME_DOC, "doc")
    # DOCX (zip container): PK\x03\x04. Without python-magic we can't
    # cheaply distinguish generic zip from a DOCX, but the upload context
    # is "office document" so we accept it under the docx mime type.
    if head.startswith(b"PK\x03\x04"):
        # Try python-magic for a precise verdict; fall back to docx.
        try:
            import magic  # type: ignore[import-untyped]

            detected = magic.from_buffer(head, mime=True)
            if detected in (_MIME_DOCX, _MIME_DOC):
                return (detected, "docx" if detected == _MIME_DOCX else "doc")
            # A bare zip archive is not in the allow-list.
            return (None, None)
        except Exception:
            return (_MIME_DOCX, "docx")

    # Plain text heuristic: only ASCII / UTF-8 printable + common whitespace.
    # We reject control bytes (other than \t \n \r) so binary blobs don't
    # slip through as "text".
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        return (None, None)
    bad = sum(
        1
        for b in head
        if b < 0x20 and b not in (0x09, 0x0A, 0x0D)
    )
    if bad == 0:
        return (_MIME_TEXT, "txt")

    return (None, None)


# ---------------------------------------------------------------------------
# Streaming write + hash
# ---------------------------------------------------------------------------


_CHUNK_SIZE = 1024 * 1024  # 1 MiB streaming buffer


async def _stream_to_disk(upload: UploadFile, dest: Path) -> tuple[int, str]:
    """Stream ``upload`` to ``dest`` while computing SHA-256.

    Returns ``(bytes_written, hex_digest)``. The destination's parent dir
    is created if it doesn't exist. The caller owns deletion on error.
    """
    hasher = hashlib.sha256()
    total = 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as f:
        while True:
            chunk = await upload.read(_CHUNK_SIZE)
            if not chunk:
                break
            f.write(chunk)
            hasher.update(chunk)
            total += len(chunk)
    return total, hasher.hexdigest()


def _safe_unlink(p: Path) -> None:
    """``unlink`` ignoring "already gone" — anything else propagates."""
    try:
        p.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Arq enqueue indirection (tests monkey-patch this)
# ---------------------------------------------------------------------------


async def enqueue_ingest(tenant_id: str, document_id: str) -> None:
    """Enqueue the ``ingest_document`` arq task.

    Lifted into a module-level helper so tests can replace it with a stub
    without spinning up Redis or arq. Production opens a fresh arq pool,
    enqueues, and immediately closes — fine for the request-scoped lifetime
    of an ingest call.
    """
    pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    try:
        await pool.enqueue_job("ingest_document", tenant_id, document_id)
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/ingest", response_model=IngestResponse)
async def ingest(
    file: UploadFile = File(...),
    tenant_id: str = Depends(current_tenant),
    db: AsyncSession = Depends(get_db_session),
) -> IngestResponse:
    """Accept a multipart upload, persist + dedupe + enqueue ingest job."""
    # Pick the on-disk extension from the client filename when possible —
    # it's only a hint; the authoritative extension comes from the magic
    # bytes we sniff after writing. A missing/odd extension defaults to
    # ``"bin"`` which still passes the ``[a-z0-9]{1,8}`` validator.
    raw_name = file.filename or "upload.bin"
    initial_ext = (
        os.path.splitext(raw_name)[1].lstrip(".").lower() or "bin"
    )
    if not initial_ext.isalnum() or len(initial_ext) > 8:
        initial_ext = "bin"

    doc_id = uuid.uuid4()
    dest = document_upload_path(tenant_id, doc_id, initial_ext)

    # 1) Stream → disk + compute hash.
    try:
        size, content_hash = await _stream_to_disk(file, dest)
    except Exception:
        _safe_unlink(dest)
        raise

    # 2) Size cap. Catches both genuinely huge uploads and clients that
    # ignore Content-Length: we only know the true size after the body is
    # drained, so the check has to live here.
    max_bytes = int(settings.max_upload_mb) * 1024 * 1024
    if size > max_bytes:
        _safe_unlink(dest)
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"file exceeds max_upload_mb={settings.max_upload_mb}",
        )

    # 3) MIME validation — sniff the head we just wrote. We re-open the
    # file rather than peek the UploadFile buffer because the upload has
    # already been fully drained by ``_stream_to_disk``.
    with dest.open("rb") as f:
        head = f.read(512)
    mime_type, sniffed_ext = _sniff_mime(head)
    if mime_type is None:
        _safe_unlink(dest)
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="unsupported file type",
        )

    # If the sniffed extension differs from the on-disk one, rename so the
    # storage layout always reflects the true content type. This keeps
    # downstream parser routing trivial (it dispatches on the suffix).
    if sniffed_ext and sniffed_ext != initial_ext:
        new_dest = document_upload_path(tenant_id, doc_id, sniffed_ext)
        if new_dest != dest:
            try:
                dest.rename(new_dest)
                dest = new_dest
            except OSError:
                # Rename can fail across filesystems; not fatal — we still
                # have a valid file at the original path.
                pass

    # 4) Dedup by (tenant_id, content_hash). On a hit we discard the
    # newly-written file and surface the existing document_id.
    existing_stmt = select(_models.Document.document_id).where(
        _models.Document.tenant_id == tenant_id,
        _models.Document.content_hash == content_hash,
    )
    existing = (await db.execute(existing_stmt)).first()
    if existing is not None:
        _safe_unlink(dest)
        return IngestResponse(
            job_id=uuid.uuid4(),
            document_id=existing[0],
            status="dedup",
            deduplicated=True,
        )

    # 5) Insert document + job rows. The session dependency commits on
    # successful return; a raise rolls back.
    document = _models.Document(
        document_id=doc_id,
        tenant_id=tenant_id,
        file_name=raw_name,
        file_size=size,
        content_hash=content_hash,
        mime_type=mime_type,
        storage_path=str(dest),
        status="pending",
    )
    db.add(document)
    await db.flush()

    job = _models.Job(
        tenant_id=tenant_id,
        document_id=doc_id,
        job_type="ingest",
        status="queued",
    )
    db.add(job)
    await db.flush()
    job_id = job.job_id

    # Make the rows visible to the worker before we enqueue — otherwise
    # the worker can race the request and SELECT zero rows.
    await db.commit()

    # 6) Enqueue. If this fails we still keep the rows: a follow-up task
    # (or manual re-enqueue) can pick them up. Enqueue errors propagate as
    # 500 so the client knows to retry.
    await enqueue_ingest(tenant_id, str(doc_id))

    # ``ensure tenant upload dir exists`` is implicit via document_upload_path.
    # Also ensures we don't leak the unused name on success.
    _ = tenant_upload_dir  # imported for potential future inspection / tests

    return IngestResponse(
        job_id=job_id,
        document_id=doc_id,
        status="queued",
        deduplicated=False,
    )
