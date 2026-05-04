"""Arq worker tasks for RAG-Anything ingestion.

This module hosts the long-running, retryable jobs the API enqueues onto
arq. Each task:

* Acquires a per-tenant Redis lock (``tenant_ingest_lock``) so two ingest
  jobs for the same tenant cannot interleave a LightRAG write.
* Mutates ``documents``/``jobs`` rows transactionally to drive the public
  status state machine (``queued`` -> ``running`` -> ``done``/``failed``).
* Distinguishes *retryable* failures (parser/MinerU transient errors) from
  *fail-fast* errors (LLM 5xx / auth) — the former re-raise so arq schedules
  another attempt, the latter mark the job ``failed`` and return.
* Publishes ``tenant_reload:{tenant_id}`` on success so any cached
  ``RAGAnything`` instance in another process can refresh its view of the
  underlying storages.

The DB layer is wrapped in tiny ``_load_document`` / ``_update_*`` helpers
so tests can monkeypatch them without standing up a real database.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

import httpx
import redis.asyncio as aioredis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from rag_service.core import paths as _paths
from rag_service.core import rag_factory
from rag_service.db import models as _models
from rag_service.db import session as _db_session
from rag_service.parsers.mineru_cloud import MineruCloudTimeoutError
from rag_service.worker.locks import LockBusy, tenant_ingest_lock


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


def _is_parser_error(exc: BaseException) -> bool:
    """True if ``exc`` looks like a transient parser/MinerU failure.

    We intentionally cast a wide net using the qualified name of the
    exception class: any class whose ``__qualname__`` (lowercased) contains
    ``"parser"`` or ``"mineru"`` qualifies. This catches future MinerU
    subclasses without us having to enumerate them here.
    """
    if isinstance(exc, MineruCloudTimeoutError):
        return True
    qualname = (type(exc).__qualname__ or "").lower()
    module = (type(exc).__module__ or "").lower()
    needle = qualname + " " + module
    return "parser" in needle or "mineru" in needle


def _is_llm_error(exc: BaseException) -> bool:
    """True if ``exc`` is an LLM/embedding failure we should NOT retry.

    LLM errors are usually structural (auth, model id, prompt format) —
    retrying just burns budget without changing the outcome. Detected via:

    * ``httpx.HTTPStatusError`` with a 4xx or 5xx response (we treat the
      whole HTTP-error family as "fail fast" because we cannot distinguish
      a transient 503 from a permanent 401 without provider knowledge).
    * Any exception class whose qualified name (lowercased) mentions
      ``llm`` / ``embedding`` / ``openai``.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return True
    qualname = (type(exc).__qualname__ or "").lower()
    module = (type(exc).__module__ or "").lower()
    needle = qualname + " " + module
    return any(tok in needle for tok in ("llm", "embedding", "openai"))


# ---------------------------------------------------------------------------
# DB helpers (small surface so tests can monkeypatch)
# ---------------------------------------------------------------------------


async def _load_document(
    session: AsyncSession, tenant_id: str, document_id: str
) -> dict[str, Any] | None:
    """Return ``{"document_id", "tenant_id", "storage_path"}`` or ``None``.

    A thin wrapper around ``SELECT storage_path FROM documents WHERE ...``
    so test code can stub the entire DB layer without monkeypatching SQL.
    """
    stmt = select(_models.Document.document_id, _models.Document.storage_path).where(
        _models.Document.document_id == document_id,
        _models.Document.tenant_id == tenant_id,
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        return None
    return {
        "document_id": str(row[0]),
        "tenant_id": tenant_id,
        "storage_path": row[1],
    }


async def _mark_job_running(
    session: AsyncSession, tenant_id: str, document_id: str
) -> None:
    """Flip the queued ingest job for ``(tenant, document)`` to ``running``."""
    now = _dt.datetime.now(_dt.timezone.utc)
    stmt = (
        update(_models.Job)
        .where(
            _models.Job.tenant_id == tenant_id,
            _models.Job.document_id == document_id,
            _models.Job.status == "queued",
        )
        .values(status="running", started_at=now)
    )
    await session.execute(stmt)


async def _mark_job_done(
    session: AsyncSession, tenant_id: str, document_id: str
) -> None:
    """Flip the running ingest job for ``(tenant, document)`` to ``done``."""
    now = _dt.datetime.now(_dt.timezone.utc)
    stmt = (
        update(_models.Job)
        .where(
            _models.Job.tenant_id == tenant_id,
            _models.Job.document_id == document_id,
        )
        .values(status="done", finished_at=now, error_message=None)
    )
    await session.execute(stmt)


async def _mark_job_failed(
    session: AsyncSession,
    tenant_id: str,
    document_id: str,
    error_message: str,
) -> None:
    """Mark the ingest job ``failed`` with ``error_message``."""
    now = _dt.datetime.now(_dt.timezone.utc)
    stmt = (
        update(_models.Job)
        .where(
            _models.Job.tenant_id == tenant_id,
            _models.Job.document_id == document_id,
        )
        .values(status="failed", finished_at=now, error_message=error_message)
    )
    await session.execute(stmt)


async def _mark_document_indexed(
    session: AsyncSession, tenant_id: str, document_id: str
) -> None:
    """Set ``documents.status='indexed'`` and stamp ``indexed_at``."""
    now = _dt.datetime.now(_dt.timezone.utc)
    stmt = (
        update(_models.Document)
        .where(
            _models.Document.tenant_id == tenant_id,
            _models.Document.document_id == document_id,
        )
        .values(status="indexed", indexed_at=now, error_message=None)
    )
    await session.execute(stmt)


async def _mark_document_failed(
    session: AsyncSession,
    tenant_id: str,
    document_id: str,
    error_message: str,
) -> None:
    """Set ``documents.status='failed'`` with ``error_message``."""
    stmt = (
        update(_models.Document)
        .where(
            _models.Document.tenant_id == tenant_id,
            _models.Document.document_id == document_id,
        )
        .values(status="failed", error_message=error_message)
    )
    await session.execute(stmt)


# ---------------------------------------------------------------------------
# Session helper (tests override this to inject a fake session)
# ---------------------------------------------------------------------------


def _open_session() -> Any:
    """Return an async context manager yielding a fresh ``AsyncSession``.

    Exposed as a module-level indirection so tests can replace it with a
    factory that yields an in-memory or mock session without monkeypatching
    SQLAlchemy itself.
    """
    sm = _db_session.get_session_maker()
    return sm.begin()


# ---------------------------------------------------------------------------
# Main task
# ---------------------------------------------------------------------------


async def ingest_document(
    ctx: dict[str, Any],
    tenant_id: str,
    document_id: str,
    *,
    retries_remaining: int = 2,
) -> dict[str, Any]:
    """Arq task: parse + index a single document for a tenant.

    Parameters
    ----------
    ctx:
        Arq job context. Must contain a ``redis`` async client under
        ``ctx["redis"]`` (arq populates it automatically).
    tenant_id:
        Validated tenant identifier.
    document_id:
        UUID string of the document row.
    retries_remaining:
        Hint from the dispatcher — how many arq retry attempts remain.
        We use it to decide whether to re-raise (let arq retry) or to mark
        the job ``failed`` and stop.

    Returns
    -------
    dict
        Summary of what happened (``{"status": "indexed"|"failed"|...}``)
        for arq result-storage / smoke tests. The state-of-record lives in
        the database; the return value is informational only.

    Raises
    ------
    LockBusy
        Propagated so arq schedules a retry — another worker is already
        ingesting for this tenant.
    Exception
        Any parser-classified exception when ``retries_remaining > 0``,
        so arq retries the job.
    """
    redis: aioredis.Redis = ctx["redis"]

    # 1) Per-tenant lock — fail fast on contention so arq retries us cleanly.
    try:
        async with tenant_ingest_lock(redis, tenant_id, ttl=600, acquire_timeout=0):
            return await _ingest_under_lock(
                redis, tenant_id, document_id, retries_remaining
            )
    except LockBusy:
        # Re-raise to arq; the dispatcher will retry after a backoff.
        raise


async def _ingest_under_lock(
    redis: aioredis.Redis,
    tenant_id: str,
    document_id: str,
    retries_remaining: int,
) -> dict[str, Any]:
    """Body of :func:`ingest_document` once the tenant lock is held."""
    # 2-3) Open a session and fetch the document. Mark the job ``running``
    # in the same transaction so the API status endpoint observes a clean
    # transition without an intermediate "queued -> still queued" window.
    async with _open_session() as session:
        doc = await _load_document(session, tenant_id, document_id)
        if doc is None:
            # No matching row at all — fail the job and stop. Don't touch a
            # documents row that doesn't exist.
            await _mark_job_failed(
                session,
                tenant_id,
                document_id,
                error_message=f"document {document_id!r} not found for tenant",
            )
            return {"status": "failed", "reason": "document_not_found"}

        await _mark_job_running(session, tenant_id, document_id)
        storage_path = doc["storage_path"]

    # 4) RAG instance for this tenant (cached + per-tenant build-locked).
    cache = rag_factory.get_cache()
    rag = await cache.get(tenant_id)

    # 5) Compute MinerU output dir and ensure it exists.
    output_dir = _paths.tenant_working_dir(tenant_id) / "mineru" / document_id
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        await rag.process_document_complete(
            file_path=storage_path, output_dir=str(output_dir)
        )
    except Exception as exc:  # noqa: BLE001 — we re-classify and re-raise below
        # 7) Classify and act.
        if _is_parser_error(exc) and not _is_llm_error(exc) and retries_remaining > 0:
            # Transient parser failure with retries left — let arq retry.
            # Do NOT touch documents.status: a future attempt may succeed,
            # and the visible state should remain "parsing"/"queued" for
            # the API consumer until we've truly given up.
            raise

        # Otherwise: fail-fast (LLM error, parser error with no retries
        # left, or any unknown error). Persist the failure and swallow.
        async with _open_session() as session:
            await _mark_job_failed(session, tenant_id, document_id, error_message=str(exc))
            await _mark_document_failed(
                session, tenant_id, document_id, error_message=str(exc)
            )
        return {"status": "failed", "reason": str(exc)}

    # 6) Success path — persist + notify.
    async with _open_session() as session:
        await _mark_document_indexed(session, tenant_id, document_id)
        await _mark_job_done(session, tenant_id, document_id)

    try:
        await redis.publish(f"tenant_reload:{tenant_id}", "1")
    except Exception:
        # Pub/sub is best-effort — a missed notification only delays a
        # cache reload elsewhere; never let it overturn an indexed doc.
        pass

    return {"status": "indexed"}
