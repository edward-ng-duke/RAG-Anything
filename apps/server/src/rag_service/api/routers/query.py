"""``POST /v1/query`` — retrieval-augmented question answering.

End-to-end flow:

1. Authenticate the request (bearer token) and resolve ``tenant_id`` via
   the ``current_tenant`` dependency.
2. Resolve the tenant's :class:`RAGAnything` from the process-wide LRU
   cache via :func:`rag_service.api.deps.get_rag_cache`.
3. Time the call and dispatch to either ``aquery`` or
   ``aquery_vlm_enhanced`` based on the ``vlm_enhanced`` flag.
4. Normalise the result: RAGAnything may return a bare string (older
   surface) or a dict-like object (``{"answer": str, "sources": [...]}``).
   We coerce both into a :class:`QueryResponse`.
5. Best-effort log a row into ``query_log`` so analytics has a record;
   logging failures never fail the request.
6. Map upstream LLM transport errors (``httpx.HTTPStatusError``) to a
   ``502 Bad Gateway`` so the client distinguishes "we couldn't reach the
   model" from "we couldn't process the request" (``500``). Both 5xx and
   4xx upstream responses surface as 502 — a 4xx from the LLM is almost
   always a misconfiguration on our side, not the client's, so leaking
   it as a 4xx would be misleading.

Why a separate code path for ``vlm_enhanced``? RAGAnything exposes a
distinct ``aquery_vlm_enhanced`` coroutine that pulls the multimodal
indexes (image / table / equation chunks) into the retrieval round.
Tenants without a VLM configured shouldn't pay that cost on every call,
so we keep it opt-in via the request flag.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from rag_service.api.deps import current_tenant, get_db_session, get_rag_cache
from rag_service.api.schemas import QueryRequest, QueryResponse, QuerySource
from rag_service.db.models import QueryLog

router = APIRouter(prefix="/v1", tags=["query"])

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result normalisation
# ---------------------------------------------------------------------------


def _coerce_source(item: Any) -> QuerySource:
    """Coerce one element of RAGAnything's ``sources`` list into a :class:`QuerySource`.

    Tolerates both dict-shaped sources (the common case) and arbitrary
    objects with attributes (so a future RA release returning typed source
    classes still works). Anything we can't identify is dropped to a bare
    snippet so we never raise here — the request shouldn't fail just
    because we got an unfamiliar source shape.
    """
    if isinstance(item, dict):
        return QuerySource(
            document_id=_stringify(item.get("document_id")),
            file_name=item.get("file_name"),
            chunk_id=_stringify(item.get("chunk_id")),
            score=_floatify(item.get("score")),
            snippet=item.get("snippet") or item.get("text") or item.get("content"),
            modality=item.get("modality"),
        )
    # Object with attributes — best effort.
    return QuerySource(
        document_id=_stringify(getattr(item, "document_id", None)),
        file_name=getattr(item, "file_name", None),
        chunk_id=_stringify(getattr(item, "chunk_id", None)),
        score=_floatify(getattr(item, "score", None)),
        snippet=getattr(item, "snippet", None)
        or getattr(item, "text", None)
        or getattr(item, "content", None),
        modality=getattr(item, "modality", None),
    )


def _stringify(v: Any) -> str | None:
    """``None`` passes through; everything else becomes ``str(...)``.

    UUIDs / ints / etc. round-trip through JSON cleanly as strings, which
    is what the schema expects.
    """
    if v is None:
        return None
    return str(v)


def _floatify(v: Any) -> float | None:
    """Best-effort float coercion; non-numerics surface as ``None``."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _normalise_result(result: Any) -> tuple[str, list[QuerySource], dict | None]:
    """Coerce RAGAnything's heterogeneous return into ``(answer, sources, tokens)``.

    Three shapes we accept:

    * bare string → ``(s, [], None)``
    * dict with ``answer`` + ``sources`` → unpack
    * anything else → string-cast the value as the answer, no sources

    The ``tokens`` field surfaces RA's usage block when present (key
    ``tokens`` or ``usage``) so the client can see in/out counts and a
    cost estimate without us recomputing it.
    """
    if isinstance(result, str):
        return result, [], None
    if isinstance(result, dict):
        answer = result.get("answer")
        if not isinstance(answer, str):
            # Some RA modes put the text under "response" / "result".
            answer = result.get("response") or result.get("result") or ""
            answer = str(answer)
        raw_sources = result.get("sources") or []
        sources = [_coerce_source(s) for s in raw_sources]
        tokens = result.get("tokens") or result.get("usage")
        if tokens is not None and not isinstance(tokens, dict):
            tokens = None
        return answer, sources, tokens
    # Unknown shape — degrade gracefully.
    return str(result), [], None


# ---------------------------------------------------------------------------
# query_log writer (best-effort)
# ---------------------------------------------------------------------------


async def _write_query_log(
    db: AsyncSession,
    *,
    tenant_id: str,
    question: str,
    mode: str,
    latency_ms: int,
    tokens: dict | None,
) -> None:
    """Insert a ``query_log`` row. Errors are logged and swallowed.

    We never let an analytics write failure tank a successful retrieval.
    The row is committed on its own so the calling request's session is
    not affected by an integrity violation here. (The ingest path uses
    the request session for transactional document+job writes; query_log
    is fire-and-forget.)
    """
    try:
        token_in = int(tokens["in"]) if tokens and "in" in tokens else None
        token_out = int(tokens["out"]) if tokens and "out" in tokens else None
        cost_usd = (
            tokens["cost_usd"]
            if tokens and "cost_usd" in tokens
            else None
        )
        row = QueryLog(
            tenant_id=tenant_id,
            question=question,
            mode=mode,
            latency_ms=latency_ms,
            token_in=token_in,
            token_out=token_out,
            cost_usd=cost_usd,
        )
        db.add(row)
        await db.flush()
        await db.commit()
    except Exception:  # noqa: BLE001 — best-effort
        _log.exception("query_log insert failed; continuing")
        try:
            await db.rollback()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/query", response_model=QueryResponse)
async def query(
    body: QueryRequest,
    tenant_id: str = Depends(current_tenant),
    db: AsyncSession = Depends(get_db_session),
    rag_cache=Depends(get_rag_cache),
) -> QueryResponse:
    """Route ``body.question`` through the tenant's RAGAnything instance."""
    rag = await rag_cache.get(tenant_id)

    started = time.perf_counter()
    try:
        if body.vlm_enhanced:
            result = await rag.aquery_vlm_enhanced(
                body.question, mode=body.mode, top_k=body.top_k
            )
        else:
            result = await rag.aquery(
                body.question, mode=body.mode, top_k=body.top_k
            )
    except httpx.HTTPStatusError as e:
        # Upstream LLM/embedding endpoint returned non-2xx. 5xx → upstream
        # outage, 4xx → almost certainly our own config (bad model name,
        # missing key, etc.). Both surface as 502 so the client doesn't
        # mistake them for a problem with their own request payload.
        status_code = e.response.status_code if e.response is not None else 0
        if status_code >= 500:
            _log.warning("LLM upstream %d: %s", status_code, e)
        else:
            _log.error("LLM upstream %d (likely config): %s", status_code, e)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail=f"upstream model error (status={status_code})",
        )
    except HTTPException:
        # Don't double-wrap — let auth / dependency 4xx through unchanged.
        raise
    except Exception as e:  # noqa: BLE001 — RAGAnything internal failure
        _log.exception("RAGAnything query failed")
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"query failed: {type(e).__name__}",
        )
    latency_ms = int((time.perf_counter() - started) * 1000)

    answer, sources, tokens = _normalise_result(result)

    # Best-effort analytics write — never fails the request.
    await _write_query_log(
        db,
        tenant_id=tenant_id,
        question=body.question,
        mode=body.mode,
        latency_ms=latency_ms,
        tokens=tokens,
    )

    return QueryResponse(
        answer=answer,
        sources=sources,
        latency_ms=latency_ms,
        tokens=tokens,
    )
