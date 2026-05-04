"""``/v1/conversations`` — chat-history CRUD plus SSE-streamed messages.

This router exposes the persistent conversation store (see
:mod:`rag_service.conversations.repository`) and the streaming
orchestrator (:mod:`rag_service.conversations.orchestrator`) over HTTP.
Every endpoint is scoped to ``(tenant_id, user_id)`` resolved from the
JWT — a user cannot see another user's conversations even within the
same tenant. Cross-user / cross-tenant access surfaces as ``404`` so the
caller cannot probe for existence.

Endpoints
---------

* ``GET    /v1/conversations`` — list the caller's conversations.
* ``POST   /v1/conversations`` — create a new conversation.
* ``GET    /v1/conversations/{id}`` — fetch a conversation + messages.
* ``DELETE /v1/conversations/{id}`` — hard-delete a conversation.
* ``POST   /v1/conversations/{id}/messages`` — append a user turn and
  stream the assistant response back as Server-Sent Events.

The SSE wire format is ``event: <name>\\ndata: <json>\\n\\n`` per the
`html5 spec <https://html.spec.whatwg.org/multipage/server-sent-events.html>`_.
The orchestrator yields plain dicts; this router is the only place that
performs SSE encoding.
"""

from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from rag_service.api.auth import current_tenant, current_user
from rag_service.api.deps import get_db_session
from rag_service.api.schemas import (
    ConversationBrief,
    ConversationCreate,
    ConversationDetailResponse,
    ConversationListResponse,
    MessageResponse,
    SendMessageRequest,
)
from rag_service.conversations import repository
from rag_service.conversations.orchestrator import stream_assistant_response
from rag_service.db.models import User

router = APIRouter(prefix="/v1/conversations", tags=["conversations"])


@router.get("", response_model=ConversationListResponse)
async def list_convos(
    user: User = Depends(current_user),
    tenant_id: str = Depends(current_tenant),
    db: AsyncSession = Depends(get_db_session),
) -> ConversationListResponse:
    """Return up to 50 conversations owned by the caller, newest first."""
    rows = await repository.list_conversations(db, tenant_id, user.user_id)
    return ConversationListResponse(
        items=[ConversationBrief.model_validate(r) for r in rows]
    )


@router.post("", response_model=ConversationBrief, status_code=201)
async def create_convo(
    req: ConversationCreate,
    user: User = Depends(current_user),
    tenant_id: str = Depends(current_tenant),
    db: AsyncSession = Depends(get_db_session),
) -> ConversationBrief:
    """Insert a new conversation and return the freshly-refreshed row."""
    convo = await repository.create_conversation(
        db, tenant_id, user.user_id, title=req.title
    )
    return ConversationBrief.model_validate(convo)


@router.get("/{conversation_id}", response_model=ConversationDetailResponse)
async def get_convo(
    conversation_id: UUID,
    user: User = Depends(current_user),
    tenant_id: str = Depends(current_tenant),
    db: AsyncSession = Depends(get_db_session),
) -> ConversationDetailResponse:
    """Fetch a conversation header plus its chronological message list."""
    result = await repository.get_with_messages(
        db, tenant_id, user.user_id, conversation_id
    )
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
    convo, msgs = result
    return ConversationDetailResponse(
        conversation=ConversationBrief.model_validate(convo),
        messages=[MessageResponse.model_validate(m) for m in msgs],
    )


@router.delete("/{conversation_id}", status_code=204)
async def delete_convo(
    conversation_id: UUID,
    user: User = Depends(current_user),
    tenant_id: str = Depends(current_tenant),
    db: AsyncSession = Depends(get_db_session),
) -> None:
    """Hard-delete a conversation; returns 204 on success, 404 on miss."""
    ok = await repository.delete_conversation(
        db, tenant_id, user.user_id, conversation_id
    )
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
    return None


@router.post("/{conversation_id}/messages")
async def send_message(
    conversation_id: UUID,
    req: SendMessageRequest,
    user: User = Depends(current_user),
    tenant_id: str = Depends(current_tenant),
    db: AsyncSession = Depends(get_db_session),
) -> StreamingResponse:
    """Stream the assistant's reply for a single user turn as SSE.

    Verifies the caller owns the conversation up front (404 otherwise) so
    we don't open a stream just to immediately fail it. Each event from
    the orchestrator is encoded as ``event: <name>\\ndata: <json>\\n\\n``.
    """
    # Verify ownership before opening the stream so a missing / cross-user
    # conversation surfaces as a clean 404 rather than a half-empty SSE body.
    result = await repository.get_with_messages(
        db, tenant_id, user.user_id, conversation_id
    )
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")

    async def event_gen():
        async for ev in stream_assistant_response(
            db,
            tenant_id,
            user.user_id,
            conversation_id,
            req.content,
            mode=req.mode,
            top_k=req.top_k,
            vlm_enhanced=req.vlm_enhanced,
        ):
            yield f"event: {ev['event']}\ndata: {json.dumps(ev)}\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")
