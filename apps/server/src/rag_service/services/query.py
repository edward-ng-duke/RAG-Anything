"""Stateless query service for ``/v1/onyx/*`` endpoints.

Wraps RAGAnything's ``aquery`` / ``aquery_vlm_enhanced`` with a uniform
interface that yields SSE-friendly events. The events match the contract
in ``ONYX_INTEGRATION_PLAN.md §6.4.1``: ``meta`` → ``chunk*`` → ``done |
error``.

Two entry points are exported:

* :func:`iter_query_events` — async generator yielding
  ``(event_name, payload_dict)`` tuples for the SSE writer.
* :func:`query_once` — non-streaming variant returning the final
  ``(answer, sources, latency_ms, tokens, warnings)`` tuple.

Both share the same upstream-call + history-truncation +
result-normalisation path so the SSE and sync endpoints can never
drift in semantics.

Why the indirection? LightRAG's native streaming is not yet wired
through RAGAnything in a way that matches our SSE shape, so the plan
permits a slice-mode fallback: compute the full answer, chunk it
into ~24-char slices, and emit each as a ``chunk`` event. The public
SSE wire format stays identical to a future native-streaming
implementation, so swapping in real streaming later is a service-layer
change with no router or client churn.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from rag_service.api.routers.query import _coerce_source, _normalise_result  # noqa: F401

_log = logging.getLogger(__name__)

# Slice size for the fallback streaming path. ~24 chars renders as a
# tiny "typing" cadence on the consumer side without producing so many
# frames that the SSE channel becomes the bottleneck. Trivially tunable.
CHUNK_SIZE_FALLBACK = 24


async def query_once(
    *,
    rag: Any,
    question: str,
    history: list[dict[str, str]],
    mode: str,
    top_k: int,
    vlm_enhanced: bool,
    max_history_turns: int,
) -> tuple[str, list[dict[str, Any]], int, dict[str, int] | None, list[str]]:
    """Run a single non-streaming query through RAGAnything.

    Returns ``(answer, sources_as_dicts, latency_ms, tokens, warnings)``.
    Caller-owned exceptions (``httpx.HTTPStatusError`` for upstream LLM
    transport errors and arbitrary ``Exception`` for internal failures)
    propagate untouched — the SSE generator translates them into
    ``error`` events; the sync endpoint translates them into 502 / 500.

    History is truncated to the *trailing* ``max_history_turns`` messages
    so the prompt budget stays predictable; ``0`` disables history
    altogether.
    """
    truncated = history[-max_history_turns:] if max_history_turns > 0 else []
    started = time.perf_counter()
    warnings: list[str] = []

    if vlm_enhanced:
        result = await rag.aquery_vlm_enhanced(
            question, mode=mode, top_k=top_k, conversation_history=truncated
        )
    else:
        result = await rag.aquery(
            question, mode=mode, top_k=top_k, conversation_history=truncated
        )

    latency_ms = int((time.perf_counter() - started) * 1000)
    answer, q_sources, tokens = _normalise_result(result)

    # Convert α's QuerySource pydantic objects to dicts for SSE
    # serialisation. ``page`` / ``bbox`` aren't on α's QuerySource so
    # we echo them as ``None`` for forward compatibility — when
    # RAGAnything starts surfacing PDF citations we'll surface them
    # through ``_coerce_source`` in one place.
    sources_as_dicts: list[dict[str, Any]] = []
    for s in q_sources:
        d = s.model_dump()
        d.setdefault("page", None)
        d.setdefault("bbox", None)
        sources_as_dicts.append(d)

    return answer, sources_as_dicts, latency_ms, tokens, warnings


async def iter_query_events(
    *,
    rag: Any,
    request_id: str,
    kb_id: str,
    question: str,
    history: list[dict[str, str]],
    mode: str,
    top_k: int,
    vlm_enhanced: bool,
    include_sources: bool,
    max_history_turns: int,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """Yield ``(event_name, payload_dict)`` tuples per the SSE contract.

    Sequence on success: ``meta`` → ``chunk`` … ``chunk`` → ``done``.
    Sequence on upstream error: ``meta`` → ``error``.

    The slice-mode fallback yields control to the event loop after every
    ``chunk`` so the router's heartbeat ticker can fire if the answer is
    large.
    """
    yield "meta", {
        "request_id": request_id,
        "kb_id": kb_id,
        "mode": mode,
        "top_k": top_k,
    }

    try:
        answer, sources, latency_ms, tokens, warnings = await query_once(
            rag=rag,
            question=question,
            history=history,
            mode=mode,
            top_k=top_k,
            vlm_enhanced=vlm_enhanced,
            max_history_turns=max_history_turns,
        )
    except httpx.HTTPStatusError as e:
        sc = e.response.status_code if e.response is not None else 0
        yield "error", {
            "code": "upstream_llm_error",
            "message": f"upstream model error (status={sc})",
            # 5xx → upstream outage, retry sensible. 4xx → almost
            # always a config problem on our side; client retry won't
            # help, so flag non-retryable.
            "retryable": sc >= 500,
        }
        return
    except Exception as e:  # noqa: BLE001 — RAGAnything internal failure
        _log.exception("RAGAnything query failed in onyx stream")
        yield "error", {
            "code": "internal_error",
            "message": f"query failed: {type(e).__name__}",
            "retryable": False,
        }
        return

    # Slice-mode chunking. The plan permits this since LightRAG's native
    # streaming integration would need deeper rework; the public SSE
    # shape stays identical.
    for i in range(0, len(answer), CHUNK_SIZE_FALLBACK):
        slice_text = answer[i : i + CHUNK_SIZE_FALLBACK]
        yield "chunk", {"text": slice_text}
        # 0-sleep yields control to the event loop so the router's
        # heartbeat ticker can fire while a long answer is streaming.
        await asyncio.sleep(0)

    done_payload: dict[str, Any] = {
        "answer": answer,
        "latency_ms": latency_ms,
        "tokens": tokens,
        "warnings": warnings,
        "sources": sources if include_sources else [],
    }
    yield "done", done_payload
