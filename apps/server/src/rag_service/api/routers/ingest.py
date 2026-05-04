"""``POST /v1/ingest`` — multipart file upload + queued indexing.

End-to-end flow:

1. Authenticate the request (bearer token) and resolve ``tenant_id`` via
   the ``current_tenant`` dependency.
2. Hand the upload off to
   :func:`rag_service.services.ingest.perform_upload`, which:

   * pre-checks the tenant's storage quota using the request's
     ``Content-Length`` as a lower bound;
   * streams the multipart body to disk under
     ``data_dir/uploads/<tenant>/<doc_uuid>.<ext>`` while computing a
     SHA-256 incrementally — the full upload is never buffered in
     memory;
   * enforces ``settings.max_upload_mb`` *after* the write (we can't
     know the size up-front for chunked transfers); on overflow,
     deletes the file and raises ``413``;
   * validates MIME by sniffing the first 512 bytes of the file we
     just wrote (PDF / DOC / DOCX / PNG / JPEG / plain text). An
     unrecognised type → delete + ``415``;
   * dedups against ``(tenant_id, content_hash)`` — on hit, deletes
     the new file and returns the existing document_id;
   * inserts ``documents`` + ``jobs`` rows in one transaction and
     enqueues the ``ingest_document`` arq task.

3. Returns :class:`IngestResponse` with the resulting ids and status.

The arq pool is created per-request rather than held in app state
because this is the ingest endpoint's only Redis user — keeping it
inline avoids a wider deps-module change. The pool is always closed
in a ``finally``.

The router itself is thin: it owns auth + response shaping, the
service module owns the workflow.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from rag_service.api.deps import current_tenant
from rag_service.api.schemas import IngestResponse
from rag_service.db.session import get_db_session
from rag_service.services.ingest import (
    enqueue_ingest as _service_enqueue_ingest,
    perform_upload,
)

router = APIRouter(prefix="/v1", tags=["ingest"])


# Re-export the enqueue helper at the router-module level so the
# existing alpha test suite — which patches
# ``rag_service.api.routers.ingest.enqueue_ingest`` directly — keeps
# working unchanged. The endpoint passes this name (looked up via the
# module globals on every call) into ``perform_upload`` so any
# monkey-patch is honoured. Production code (and the new ONYX router)
# should import ``enqueue_ingest`` from
# :mod:`rag_service.services.ingest` directly.
enqueue_ingest = _service_enqueue_ingest


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/ingest", response_model=IngestResponse)
async def ingest(
    request: Request,
    file: UploadFile = File(...),
    tenant_id: str = Depends(current_tenant),
    db: AsyncSession = Depends(get_db_session),
) -> IngestResponse:
    """Accept a multipart upload, persist + dedupe + enqueue ingest job."""

    # Pass a closure that resolves ``enqueue_ingest`` against this
    # module's globals on every call. Alpha tests monkey-patch the
    # router-module symbol; the closure ensures those patches take
    # effect even though :func:`perform_upload` lives elsewhere.
    async def _enqueue(tid: str, did: str) -> None:
        await enqueue_ingest(tid, did)

    result = await perform_upload(
        tenant_id=tenant_id,
        file=file,
        db=db,
        request=request,
        enqueue_fn=_enqueue,
    )
    return IngestResponse(
        job_id=result.job_id,
        document_id=result.document_id,
        status=result.status,
        deduplicated=result.deduplicated,
    )
