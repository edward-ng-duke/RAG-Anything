"""CRUD helpers for ``conversations`` and ``messages`` tables.

Every read and mutation is scoped to ``(tenant_id, user_id)`` so the
caller cannot accidentally cross tenant or user boundaries; the
``conversation_id`` predicate alone is never sufficient. The repository
intentionally exposes plain functions (not a class) — orchestration and
HTTP layers above just compose them with their own session.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from rag_service.db.models import Conversation, Message


async def list_conversations(
    db: AsyncSession,
    tenant_id: str,
    user_id: uuid.UUID,
    *,
    limit: int = 50,
) -> list[Conversation]:
    """Return up to ``limit`` conversations for the user, newest first."""
    rows = (
        await db.execute(
            select(Conversation)
            .where(
                Conversation.tenant_id == tenant_id,
                Conversation.user_id == user_id,
            )
            .order_by(Conversation.updated_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


async def create_conversation(
    db: AsyncSession,
    tenant_id: str,
    user_id: uuid.UUID,
    title: str | None = None,
) -> Conversation:
    """Insert a new conversation and return the refreshed row."""
    convo = Conversation(tenant_id=tenant_id, user_id=user_id, title=title)
    db.add(convo)
    await db.flush()
    await db.commit()
    await db.refresh(convo)
    return convo


async def get_with_messages(
    db: AsyncSession,
    tenant_id: str,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> tuple[Conversation, list[Message]] | None:
    """Fetch a conversation + chronological messages, or ``None`` if missing.

    Returns ``None`` when the conversation does not exist *or* belongs to
    a different tenant/user — callers cannot distinguish the two cases,
    by design.
    """
    convo = (
        await db.execute(
            select(Conversation).where(
                Conversation.conversation_id == conversation_id,
                Conversation.tenant_id == tenant_id,
                Conversation.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if convo is None:
        return None
    messages = (
        await db.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.asc())
        )
    ).scalars().all()
    return convo, list(messages)


async def delete_conversation(
    db: AsyncSession,
    tenant_id: str,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> bool:
    """Delete a conversation. Returns ``True`` iff a row was removed."""
    result = await db.execute(
        delete(Conversation)
        .where(
            Conversation.conversation_id == conversation_id,
            Conversation.tenant_id == tenant_id,
            Conversation.user_id == user_id,
        )
        .returning(Conversation.conversation_id)
    )
    deleted = result.scalar_one_or_none()
    await db.commit()
    return deleted is not None


async def append_message(
    db: AsyncSession,
    conversation_id: uuid.UUID,
    role: str,
    content: str,
    sources: dict | list | None = None,
) -> Message:
    """Insert a message and bump the parent conversation's ``updated_at``."""
    msg = Message(
        conversation_id=conversation_id,
        role=role,
        content=content,
        sources=sources,
    )
    db.add(msg)
    # bump conversation updated_at
    await db.execute(
        update(Conversation)
        .where(Conversation.conversation_id == conversation_id)
        .values(updated_at=datetime.now(timezone.utc))
    )
    await db.flush()
    await db.commit()
    await db.refresh(msg)
    return msg


async def recent_history(
    db: AsyncSession,
    conversation_id: uuid.UUID,
    n: int = 10,
) -> list[Message]:
    """Return the last ``n`` messages, oldest-first.

    The query orders by ``created_at DESC`` so SQLite/Postgres can use
    the ``messages_conversation_created_idx`` index, then we reverse the
    slice so the caller sees chronological order.
    """
    rows = (
        await db.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.desc())
            .limit(n)
        )
    ).scalars().all()
    # Reverse to chronological
    return list(reversed(list(rows)))
