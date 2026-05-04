"""History-aware streaming orchestrator for chat-style RAG queries.

This module bridges the persistent conversation store (see
:mod:`rag_service.conversations.repository`) and the per-tenant
``RAGAnything`` instance cache (see :mod:`rag_service.core.rag_factory`).

The single public entry point :func:`stream_assistant_response` is an
async generator yielding SSE-shaped event dicts:

* ``{"event": "delta", "content": "<chunk>"}`` — one or more, in order
* ``{"event": "done", "sources": [...]}`` — exactly one terminal event
* ``{"event": "error", "message": "..."}`` — at most one, in lieu of done

The HTTP layer (Task 4.4) is responsible for SSE encoding; this module
intentionally stays transport-agnostic and synchronous-test-friendly.

Today RAGAnything's ``aquery`` returns a fully-materialised string (or
dict). We chunk it client-side so the public API is already shaped for
real token streaming once upstream exposes it.
"""

from __future__ import annotations

import uuid
from typing import Any, AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from rag_service.conversations import repository
from rag_service.core.rag_factory import get_cache


# Size of each ``delta`` chunk when we re-slice a fully-materialised
# answer. Small enough to feel streamy in a browser; large enough that
# a multi-KB answer doesn't produce thousands of events.
_DELTA_CHUNK_SIZE = 32


def _format_history(messages) -> list[dict[str, str]]:
    """Format prior ``Message`` rows as LightRAG ``conversation_history``.

    LightRAG expects ``[{"role": "user"|"assistant", "content": "..."}]``.
    """
    return [{"role": m.role, "content": m.content} for m in messages]


def _normalise_response(result: Any) -> tuple[str, list[Any]]:
    """Coerce ``aquery`` / ``aquery_vlm_enhanced`` output to ``(answer, sources)``.

    RAGAnything has historically returned either a plain string or a
    dict carrying ``answer``/``response``/``result`` plus optional
    ``sources``. We accept both, plus an arbitrary fallback (stringified)
    so an unexpected upstream shape never blows up the stream.
    """
    if isinstance(result, str):
        return result, []
    if isinstance(result, dict):
        answer = (
            result.get("answer")
            or result.get("response")
            or result.get("result")
            or ""
        )
        raw_sources = result.get("sources") or []
        sources = list(raw_sources) if isinstance(raw_sources, list) else []
        return answer, sources
    return str(result), []


async def stream_assistant_response(
    db: AsyncSession,
    tenant_id: str,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    user_message_content: str,
    *,
    mode: str = "hybrid",
    top_k: int = 10,
    vlm_enhanced: bool = False,
    history_window: int = 10,
) -> AsyncIterator[dict]:
    """Yield SSE-compatible event dicts for a single user turn.

    Side effects:

    * Persists the user message **before** querying the RAG (so a crash
      mid-query still leaves a record of what the user asked).
    * Persists the assistant message + sources only on success.

    Note ``user_id`` is currently unused inside the orchestrator — it's
    accepted in the signature so that future authorisation / audit hooks
    don't require a breaking change at the call site.
    """
    del user_id  # reserved for future audit hooks

    # 1. Persist user message immediately. If the RAG call fails we still
    #    have the prompt on disk; the assistant turn is a no-op on error.
    await repository.append_message(
        db, conversation_id, role="user", content=user_message_content
    )

    # 2. Pull recent history for context. We fetch ``window + 1`` so we can
    #    safely drop the just-appended user message — that message is the
    #    current ``prompt`` and shouldn't be duplicated in
    #    ``conversation_history``.
    history = await repository.recent_history(
        db, conversation_id, n=history_window + 1
    )
    if (
        history
        and history[-1].role == "user"
        and history[-1].content == user_message_content
    ):
        history = history[:-1]
    # If the resulting history exceeds the requested window (because the
    # caller passed >= 1 turns of true context plus our +1), trim the
    # head — older context is the cheaper thing to lose.
    if len(history) > history_window:
        history = history[-history_window:]

    history_payload = _format_history(history)

    # 3. Resolve the per-tenant RAGAnything instance.
    cache = get_cache()
    rag = await cache.get(tenant_id)

    # 4. Query. We dispatch to ``aquery_vlm_enhanced`` when the caller
    #    asks for it AND the instance actually exposes the method;
    #    otherwise fall through to ``aquery``.
    full_answer = ""
    sources: list[Any] = []
    try:
        if vlm_enhanced and hasattr(rag, "aquery_vlm_enhanced"):
            result = await rag.aquery_vlm_enhanced(
                user_message_content,
                mode=mode,
                top_k=top_k,
                conversation_history=history_payload,
            )
        else:
            result = await rag.aquery(
                user_message_content,
                mode=mode,
                top_k=top_k,
                conversation_history=history_payload,
            )
        full_answer, sources = _normalise_response(result)

        # Chunk the answer client-side. Once RAGAnything exposes a true
        # token-streaming API we'll swap this loop for an ``async for``.
        for i in range(0, len(full_answer), _DELTA_CHUNK_SIZE):
            yield {
                "event": "delta",
                "content": full_answer[i : i + _DELTA_CHUNK_SIZE],
            }
    except Exception as e:  # noqa: BLE001 — surface to client as error event
        yield {"event": "error", "message": str(e)}
        # Do NOT persist a partial / failed assistant message; the user
        # message remains so the next attempt can see what was asked.
        return

    # 5. Persist assistant message + sources, then close the stream.
    await repository.append_message(
        db,
        conversation_id,
        role="assistant",
        content=full_answer,
        sources={"sources": sources} if sources else None,
    )

    yield {"event": "done", "sources": sources}
