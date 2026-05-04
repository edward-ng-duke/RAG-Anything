"""Shared multipart-upload / dedup / enqueue pipeline.

Both the alpha ``/v1/ingest`` router and the ONYX-facing
``/v1/onyx/documents`` router drive the same workflow:

1. Pre-check the tenant's storage quota using the request's
   ``Content-Length`` as a lower bound for the incoming body.
2. Stream the multipart upload to disk under
   ``data_dir/uploads/<tenant>/<doc_uuid>.<ext>`` while computing a
   SHA-256 incrementally.
3. Enforce ``settings.max_upload_mb`` *after* the write (chunked
   transfers don't expose a true size up-front); on overflow, delete
   the file and raise ``413``.
4. Sniff MIME from the head of the on-disk file. Unrecognised → 415
   plus on-disk cleanup.
5. Dedup against ``(tenant_id, content_hash)``; on hit return the
   existing document_id and a fresh placeholder job_id.
6. Insert ``documents`` (status ``pending``) + ``jobs`` (status
   ``queued``) in one transaction, commit, then enqueue the
   ``ingest_document`` arq task.

The two routers differ only in how ``tenant_id`` is sourced (JWT for
alpha, ``X-Onyx-KB-Id`` header for ONYX) — everything from quota check
onwards is identical, so it lives here.

Tests monkey-patch :func:`enqueue_ingest` on this module to avoid
spinning up Redis / arq.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import HTTPException, Request, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from rag_service.config import settings
from rag_service.core.paths import document_upload_path, tenant_upload_dir
from rag_service.db import models as _models


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UploadResult:
    """Outcome of a successful (or dedup-suppressed) upload.

    ``status`` is ``"queued"`` for a fresh upload and ``"dedup"`` when
    the upload collided with an existing document by
    ``(tenant_id, content_hash)``. ``deduplicated`` mirrors the same
    distinction as a boolean for clients that prefer it that way.
    ``job_id`` is always populated — on dedup it's a fresh placeholder
    UUID so the client always sees a well-formed UUID even though no
    real job row was inserted.
    """

    document_id: uuid.UUID
    job_id: uuid.UUID
    status: str  # "queued" | "dedup"
    deduplicated: bool
    file_name: str
    file_size: int
    content_hash: str
    mime_type: str | None


# ---------------------------------------------------------------------------
# MIME sniffing
# ---------------------------------------------------------------------------


# Allowed MIME types and their canonical extensions. Order is irrelevant
# because each magic signature is unambiguous at offset 0.
_MIME_PDF = "application/pdf"
_MIME_PNG = "image/png"
_MIME_JPEG = "image/jpeg"
_MIME_DOC = "application/msword"
_MIME_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_MIME_TEXT = "text/plain"


def _sniff_mime(head: bytes) -> tuple[str | None, str | None]:
    """Detect MIME type + canonical extension from the first 512 bytes.

    Uses ``python-magic`` if importable, else a small magic-byte table.
    We *prefer* ``python-magic`` because it correctly tells DOCX (zip)
    apart from a generic zip archive; without it we fall back to the
    cruder "starts with PK\\x03\\x04" rule. A type outside the allow-list
    yields ``(None, None)`` so the caller can produce a clean ``415``.

    Returns
    -------
    (mime_type, extension) or (None, None) when unrecognised.
    """
    if not head:
        return (None, None)

    if head.startswith(b"%PDF-"):
        return (_MIME_PDF, "pdf")
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return (_MIME_PNG, "png")
    if head.startswith(b"\xff\xd8\xff"):
        return (_MIME_JPEG, "jpg")
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return (_MIME_DOC, "doc")
    if head.startswith(b"PK\x03\x04"):
        try:
            import magic  # type: ignore[import-untyped]

            detected = magic.from_buffer(head, mime=True)
            if detected in (_MIME_DOCX, _MIME_DOC):
                return (detected, "docx" if detected == _MIME_DOCX else "doc")
            return (None, None)
        except Exception:
            return (_MIME_DOCX, "docx")

    # Plain text heuristic: ASCII / UTF-8 printable + common whitespace
    # only. Reject control bytes (other than \t \n \r) so binary blobs
    # don't slip through as "text".
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

    Returns ``(bytes_written, hex_digest)``. The destination's parent
    directory is created if it doesn't exist. The caller owns deletion
    on error.
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

    Lifted into a module-level helper so tests can replace it with a
    stub without spinning up Redis or arq. Production opens a fresh arq
    pool, enqueues, and immediately closes — fine for the
    request-scoped lifetime of an ingest call.
    """
    pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    try:
        await pool.enqueue_job("ingest_document", tenant_id, document_id)
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# Quota pre-check
# ---------------------------------------------------------------------------


async def _enforce_storage_quota(
    db: AsyncSession,
    tenant_id: str,
    incoming_bytes: int,
) -> None:
    """Raise 413 if ``used + incoming_bytes`` would exceed the tenant's quota.

    The quota is sourced from ``tenants.storage_quota_mb`` with a 1 GiB
    default for tenants where the column happens to be NULL (matches
    the schema's server_default). The ``used`` aggregate excludes rows
    in the ``"deleted"`` status so soft-deleted documents don't count
    against the live quota — same predicate ``GET /v1/tenants/me`` uses.

    ``incoming_bytes`` is the client-declared ``Content-Length``; it
    can be a lower bound (chunked uploads omit it) but it's the only
    signal we have *before* draining the body. The post-write
    ``max_upload_mb`` cap stays in place as a hard ceiling for clients
    that lie or stream.
    """
    quota_mb = (
        await db.execute(
            select(_models.Tenant.storage_quota_mb).where(
                _models.Tenant.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()
    if quota_mb is None:
        quota_mb = 1024
    quota_bytes = quota_mb * 1024 * 1024

    used = (
        await db.execute(
            select(func.coalesce(func.sum(_models.Document.file_size), 0)).where(
                _models.Document.tenant_id == tenant_id,
                _models.Document.status != "deleted",
            )
        )
    ).scalar_one()

    if used + incoming_bytes > quota_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"storage quota exceeded ({quota_mb} MB)",
        )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def perform_upload(
    *,
    tenant_id: str,
    file: UploadFile,
    db: AsyncSession,
    request: Request | None = None,
    enqueue_fn: Callable[[str, str], Awaitable[None]] | None = None,
) -> UploadResult:
    """Run the multipart-upload pipeline end-to-end for ``tenant_id``.

    Quota pre-check → stream-to-disk → size cap → MIME sniff → dedup →
    insert document + job rows → enqueue arq task → return
    :class:`UploadResult`.

    ``request`` is optional purely so unit tests can synthesise a call
    without constructing a Starlette request; when present, its
    ``Content-Length`` header is used as the quota pre-check signal.
    A missing or non-numeric header is treated as zero (no signal),
    in which case the post-write size cap remains the only ceiling.

    ``enqueue_fn`` is the coroutine invoked after a successful insert
    to schedule the background ``ingest_document`` task. Defaults to
    :func:`enqueue_ingest` on this module. The alpha router passes its
    own module-level :func:`enqueue_ingest` re-export so existing
    monkey-patch-based tests continue to work without reaching across
    modules.
    """
    # 0) Quota pre-check before draining the body to disk.
    incoming = 0
    if request is not None:
        try:
            incoming = int(request.headers.get("content-length") or 0)
        except ValueError:
            incoming = 0
    await _enforce_storage_quota(db, tenant_id, incoming)

    # 1) Determine the on-disk extension from the client filename.
    # The authoritative extension comes from the magic bytes after we
    # write the file; the initial name is just a hint. Anything odd
    # falls back to ``"bin"`` which still passes the path validator.
    raw_name = file.filename or "upload.bin"
    initial_ext = (
        os.path.splitext(raw_name)[1].lstrip(".").lower() or "bin"
    )
    if not initial_ext.isalnum() or len(initial_ext) > 8:
        initial_ext = "bin"

    doc_id = uuid.uuid4()
    dest = document_upload_path(tenant_id, doc_id, initial_ext)

    # 2) Stream → disk + compute hash.
    try:
        size, content_hash = await _stream_to_disk(file, dest)
    except Exception:
        _safe_unlink(dest)
        raise

    # 3) Size cap. Catches huge uploads and clients that ignore
    # Content-Length: we only know the true size after the body is
    # drained, so the check has to live here.
    max_bytes = int(settings.max_upload_mb) * 1024 * 1024
    if size > max_bytes:
        _safe_unlink(dest)
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"file exceeds max_upload_mb={settings.max_upload_mb}",
        )

    # 4) MIME validation — sniff the head we just wrote. Re-open from
    # disk because the upload buffer was fully drained by
    # :func:`_stream_to_disk`.
    with dest.open("rb") as f:
        head = f.read(512)
    mime_type, sniffed_ext = _sniff_mime(head)
    if mime_type is None:
        _safe_unlink(dest)
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="unsupported file type",
        )

    # If the sniffed extension differs from the on-disk one, rename so
    # storage layout always reflects the true content type. Keeps
    # downstream parser routing trivial (it dispatches on the suffix).
    if sniffed_ext and sniffed_ext != initial_ext:
        new_dest = document_upload_path(tenant_id, doc_id, sniffed_ext)
        if new_dest != dest:
            try:
                dest.rename(new_dest)
                dest = new_dest
            except OSError:
                # Cross-filesystem rename can fail; not fatal — the
                # original path still has a valid file.
                pass

    # 5) Dedup by (tenant_id, content_hash). On a hit we discard the
    # newly-written file and surface the existing document_id.
    existing_stmt = select(_models.Document.document_id).where(
        _models.Document.tenant_id == tenant_id,
        _models.Document.content_hash == content_hash,
    )
    existing = (await db.execute(existing_stmt)).first()
    if existing is not None:
        _safe_unlink(dest)
        return UploadResult(
            document_id=existing[0],
            job_id=uuid.uuid4(),
            status="dedup",
            deduplicated=True,
            file_name=raw_name,
            file_size=size,
            content_hash=content_hash,
            mime_type=mime_type,
        )

    # 6) Insert document + job rows.
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

    # 7) Enqueue. If this fails we still keep the rows: a follow-up
    # task (or manual re-enqueue) can pick them up. Enqueue errors
    # propagate as 500 so the client knows to retry.
    enqueue = enqueue_fn or enqueue_ingest
    await enqueue(tenant_id, str(doc_id))

    # ``ensure tenant upload dir exists`` is implicit via
    # document_upload_path. Reference here keeps the import alive for
    # potential future inspection / tests.
    _ = tenant_upload_dir

    return UploadResult(
        document_id=doc_id,
        job_id=job_id,
        status="queued",
        deduplicated=False,
        file_name=raw_name,
        file_size=size,
        content_hash=content_hash,
        mime_type=mime_type,
    )
