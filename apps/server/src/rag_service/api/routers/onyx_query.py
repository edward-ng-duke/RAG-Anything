"""``/v1/onyx/query`` — stateless RAG query (SSE + sync) for ONYX.

ONYX is the source of truth for chat history; we accept history in the
request body and never persist conversations on our side. Two endpoints
share one underlying service:

* ``POST /v1/onyx/query``        — SSE stream (``text/event-stream``).
* ``POST /v1/onyx/query/sync``   — plain JSON for SSE-unfriendly callers.

The SSE writer interleaves the upstream generator with a heartbeat
ticker so reverse proxies (nginx, ALB) don't idle-close a long-running
connection: every ``HEARTBEAT_INTERVAL_SEC`` of silence we emit a
``: keepalive`` comment frame. The wire-level error path is in-band —
``error`` events ride inside a 200 SSE response — so the consumer's
event-loop never has to special-case "did the HTTP layer fail or did the
generator emit an error frame?".
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from rag_service.api.auth_onyx import OnyxCallContext, onyx_service_auth
from rag_service.api.deps import get_db_session, get_rag_cache
from rag_service.api.onyx_schemas import (
    OnyxQueryRequest,
    OnyxQuerySource,
    OnyxQuerySyncResponse,
)
from rag_service.api.routers.query import _write_query_log
from rag_service.services.query import iter_query_events, query_once

router = APIRouter(prefix="/v1/onyx", tags=["onyx-query"])

_log = logging.getLogger(__name__)

# How often to emit a ``: keepalive`` comment when the upstream
# generator is silent. 15s is shorter than nginx's default 60s
# proxy_read_timeout so our heartbeat keeps the connection warm.
HEARTBEAT_INTERVAL_SEC = 15


def _to_sse(event: str, data: dict) -> bytes:
    """Encode one ``(event, data)`` pair per the SSE wire format.

    ``ensure_ascii=False`` so non-ASCII tokens don't bloat the byte
    stream; consumer browsers / Onyx parsers all assume UTF-8.
    """
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


@router.post("/query")
async def query_stream(
    body: OnyxQueryRequest,
    ctx: OnyxCallContext = Depends(onyx_service_auth),
    db: AsyncSession = Depends(get_db_session),
    rag_cache=Depends(get_rag_cache),
) -> StreamingResponse:
    """Stream a RAG answer as Server-Sent Events.

    Frame ordering is ``meta`` → ``chunk*`` → ``done`` on success and
    ``meta`` → ``error`` on upstream failure. The HTTP status stays 200
    in both cases — error semantics live inside the SSE body so a
    consumer always opens one persistent stream and parses one shape.
    """
    rag = await rag_cache.get(ctx.kb_id)
    history_dicts = [m.model_dump() for m in body.history]

    async def gen():
        # Producer/consumer split: a background task drives the upstream
        # event generator and pushes events into a queue; this consumer
        # only ever waits on ``queue.get()`` with a heartbeat timeout.
        #
        # The naive ``asyncio.wait_for(agen.__anext__(), timeout=15)``
        # design cancels the *underlying* coroutine on timeout — and if
        # the upstream generator is suspended inside ``rag.aquery(...)``
        # at the moment the heartbeat fires, that CancelledError
        # propagates into the in-flight LLM call and aborts it. Every
        # 15s tick would kill any long query. Pumping through a queue
        # decouples the two: the producer runs to completion regardless
        # of how many heartbeats we emit on the consumer side.
        queue: asyncio.Queue = asyncio.Queue()
        SENTINEL = object()

        async def producer():
            try:
                async for event_name, payload in iter_query_events(
                    rag=rag,
                    request_id=ctx.request_id,
                    kb_id=ctx.kb_id,
                    question=body.question,
                    history=history_dicts,
                    mode=body.mode,
                    top_k=body.top_k,
                    vlm_enhanced=body.vlm_enhanced,
                    include_sources=body.include_sources,
                    max_history_turns=body.max_history_turns,
                ):
                    await queue.put((event_name, payload))
            except Exception as e:  # noqa: BLE001
                # Belt-and-braces: iter_query_events already converts
                # its own raises into ``error`` events, but if anything
                # leaks (e.g. cancellation from client disconnect)
                # surface it as one trailing error frame rather than
                # tearing the whole response down silently.
                _log.exception("onyx stream producer crashed")
                await queue.put(
                    (
                        "error",
                        {
                            "code": "internal_error",
                            "message": f"query failed: {type(e).__name__}",
                            "retryable": False,
                        },
                    )
                )
            finally:
                await queue.put(SENTINEL)

        task = asyncio.create_task(producer())
        try:
            while True:
                try:
                    item = await asyncio.wait_for(
                        queue.get(), timeout=HEARTBEAT_INTERVAL_SEC
                    )
                except asyncio.TimeoutError:
                    yield b": keepalive\n\n"
                    continue
                if item is SENTINEL:
                    break
                event_name, payload = item
                yield _to_sse(event_name, payload)
                if event_name == "done":
                    # Best-effort analytics write — never fail the request.
                    try:
                        await _write_query_log(
                            db,
                            tenant_id=ctx.kb_id,
                            question=body.question,
                            mode=body.mode,
                            latency_ms=payload.get("latency_ms", 0),
                            tokens=payload.get("tokens"),
                        )
                    except Exception:  # noqa: BLE001
                        _log.exception("query_log write failed (onyx stream)")
                    break
                if event_name == "error":
                    break
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    headers = {
        "Cache-Control": "no-cache",
        # Tell nginx not to buffer this response — buffering would
        # defeat the SSE streaming model.
        "X-Accel-Buffering": "no",
        "X-Request-Id": ctx.request_id,
    }
    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers=headers,
    )


@router.post("/query/sync", response_model=OnyxQuerySyncResponse)
async def query_sync(
    body: OnyxQueryRequest,
    ctx: OnyxCallContext = Depends(onyx_service_auth),
    db: AsyncSession = Depends(get_db_session),
    rag_cache=Depends(get_rag_cache),
) -> OnyxQuerySyncResponse:
    """Non-streaming variant — returns the same payload as the SSE ``done`` event.

    Upstream LLM transport errors surface as 502; everything else is a
    500. We don't 4xx upstream errors because a 4xx from the LLM is
    almost always a misconfiguration on our side, not the client's.
    """
    rag = await rag_cache.get(ctx.kb_id)
    history_dicts = [m.model_dump() for m in body.history]
    try:
        answer, sources, latency_ms, tokens, warnings = await query_once(
            rag=rag,
            question=body.question,
            history=history_dicts,
            mode=body.mode,
            top_k=body.top_k,
            vlm_enhanced=body.vlm_enhanced,
            max_history_turns=body.max_history_turns,
        )
    except Exception as e:  # noqa: BLE001
        _log.exception("RAGAnything query failed in onyx /query/sync")
        # Treat upstream HTTP errors as 502; everything else as 500.
        if isinstance(e, httpx.HTTPStatusError):
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY, "upstream model error"
            )
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "query failed")

    # Best-effort analytics write — failure must not fail the request.
    try:
        await _write_query_log(
            db,
            tenant_id=ctx.kb_id,
            question=body.question,
            mode=body.mode,
            latency_ms=latency_ms,
            tokens=tokens,
        )
    except Exception:  # noqa: BLE001
        _log.exception("query_log write failed (onyx sync)")

    return OnyxQuerySyncResponse(
        request_id=ctx.request_id,
        answer=answer,
        sources=[
            OnyxQuerySource(**s) for s in (sources if body.include_sources else [])
        ],
        latency_ms=latency_ms,
        tokens=tokens,
        warnings=warnings,
    )
