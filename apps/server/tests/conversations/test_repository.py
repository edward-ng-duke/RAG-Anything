"""Tests for ``rag_service.conversations.repository``.

Drives the repository against an in-memory SQLite engine — same
PG→SQLite metadata patch the auth/e2e suites use, copied inline so this
test file is self-contained. The patches are idempotent: re-applying
them after another test module already triggered them is a no-op.

Coverage:
* ``create_conversation`` round-trips and returns a refreshed row.
* ``list_conversations`` filters by both ``tenant_id`` and ``user_id``.
* ``get_with_messages`` returns messages in chronological order.
* ``get_with_messages`` returns ``None`` for a sibling user's row
  (cross-user isolation).
* ``delete_conversation`` returns ``True`` on hit, ``False`` on miss.
* ``append_message`` bumps ``conversations.updated_at``.
* ``recent_history`` returns the last N messages, oldest-first.
"""

from __future__ import annotations

# conftest.py at tests/ sets the required env vars. We override DATA_DIR
# locally so each test process gets a writable scratch dir.
import os  # noqa: E402

os.environ.setdefault("DATA_DIR", "/tmp/rag_conversations_repo_test")

import asyncio  # noqa: E402
import uuid  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB, UUID  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402
from sqlalchemy.schema import ColumnDefault  # noqa: E402


# ---------------------------------------------------------------------------
# PG → SQLite schema patches (mirrors tests/api/test_auth_basic.py)
# ---------------------------------------------------------------------------


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):  # noqa: ARG001
    return "JSON"


@compiles(UUID, "sqlite")
def _compile_uuid_sqlite(element, compiler, **kw):  # noqa: ARG001
    return "CHAR(36)"


_id_counter = {"n": 0}


def _next_id() -> int:
    _id_counter["n"] += 1
    return _id_counter["n"]


def _patch_metadata_for_sqlite() -> None:
    from rag_service.db.base import Base
    from rag_service.db import models  # noqa: F401 — registers tables

    for tbl in Base.metadata.tables.values():
        for col in tbl.columns:
            sd = col.server_default
            if sd is not None:
                arg = getattr(sd, "arg", None)
                if arg is not None:
                    rendered = str(arg)
                    if "::jsonb" in rendered:
                        col.server_default = None
                        if col.default is None:
                            col.default = ColumnDefault(lambda: {})
                    elif "gen_random_uuid" in rendered:
                        col.server_default = None
                        if col.default is None:
                            col.default = ColumnDefault(lambda: uuid.uuid4())
            if getattr(col, "identity", None) is not None:
                col.identity = None
                col.autoincrement = True
                if col.primary_key and col.default is None:
                    col.default = ColumnDefault(_next_id)


_patch_metadata_for_sqlite()


# ---------------------------------------------------------------------------
# Per-test engine + session factory
# ---------------------------------------------------------------------------


@pytest.fixture
async def session_factory():
    """Yield an ``async_sessionmaker`` bound to a fresh in-memory SQLite."""
    from rag_service.db.base import Base
    from rag_service.db import models  # noqa: F401 — registers tables

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield SessionLocal
    finally:
        await engine.dispose()


async def _make_tenant_and_user(
    SessionLocal,
    *,
    tenant_id: str = "t-alpha",
    email: str | None = None,
) -> tuple[str, uuid.UUID]:
    """Insert a Tenant + User and return ``(tenant_id, user_id)``."""
    from rag_service.db.models import Tenant, User

    user_email = email or f"{uuid.uuid4().hex[:8]}@example.com"
    async with SessionLocal() as s:
        # Tenant may already exist if multiple users share the same tenant.
        existing = (
            await s.execute(select(Tenant).where(Tenant.tenant_id == tenant_id))
        ).scalar_one_or_none()
        if existing is None:
            s.add(Tenant(tenant_id=tenant_id, display_name=tenant_id))
        user = User(
            email=user_email,
            password_hash="x",  # tests don't auth; placeholder is fine
            display_name=user_email.split("@", 1)[0],
        )
        s.add(user)
        await s.commit()
        await s.refresh(user)
        return tenant_id, user.user_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_conversation(session_factory):
    """``create_conversation`` persists a row and returns the refreshed entity."""
    from rag_service.conversations import repository as repo
    from rag_service.db.models import Conversation

    tenant_id, user_id = await _make_tenant_and_user(session_factory)

    async with session_factory() as s:
        convo = await repo.create_conversation(
            s, tenant_id, user_id, title="hello"
        )

    assert convo.conversation_id is not None
    assert convo.tenant_id == tenant_id
    assert convo.user_id == user_id
    assert convo.title == "hello"
    assert convo.created_at is not None
    assert convo.updated_at is not None

    async with session_factory() as s:
        rows = (await s.execute(select(Conversation))).scalars().all()
        assert len(rows) == 1
        assert rows[0].conversation_id == convo.conversation_id


@pytest.mark.asyncio
async def test_list_conversations_filters_by_tenant_and_user(session_factory):
    """``list_conversations`` only returns rows for the given tenant+user."""
    from rag_service.conversations import repository as repo

    # Two users in tenant A, one user in tenant B.
    tenant_a, user_a1 = await _make_tenant_and_user(
        session_factory, tenant_id="t-a", email="a1@example.com"
    )
    _, user_a2 = await _make_tenant_and_user(
        session_factory, tenant_id="t-a", email="a2@example.com"
    )
    tenant_b, user_b1 = await _make_tenant_and_user(
        session_factory, tenant_id="t-b", email="b1@example.com"
    )

    async with session_factory() as s:
        await repo.create_conversation(s, tenant_a, user_a1, title="A1-1")
        await repo.create_conversation(s, tenant_a, user_a1, title="A1-2")
        await repo.create_conversation(s, tenant_a, user_a2, title="A2-1")
        await repo.create_conversation(s, tenant_b, user_b1, title="B1-1")

    async with session_factory() as s:
        a1 = await repo.list_conversations(s, tenant_a, user_a1)
    titles = {c.title for c in a1}
    assert titles == {"A1-1", "A1-2"}

    async with session_factory() as s:
        a2 = await repo.list_conversations(s, tenant_a, user_a2)
    assert {c.title for c in a2} == {"A2-1"}

    async with session_factory() as s:
        # Same user_id but wrong tenant returns nothing.
        wrong_tenant = await repo.list_conversations(s, tenant_b, user_a1)
    assert wrong_tenant == []


@pytest.mark.asyncio
async def test_get_with_messages_includes_in_chronological_order(session_factory):
    """``get_with_messages`` returns messages oldest → newest."""
    from rag_service.conversations import repository as repo

    tenant_id, user_id = await _make_tenant_and_user(session_factory)

    async with session_factory() as s:
        convo = await repo.create_conversation(s, tenant_id, user_id, title="t")

    # Insert with explicit, monotonically-increasing created_at to get
    # a deterministic ordering even on fast SQLite (which has 1s
    # CURRENT_TIMESTAMP resolution).
    async with session_factory() as s:
        from rag_service.db.models import Message

        base = datetime.now(timezone.utc)
        for i, role in enumerate(["user", "assistant", "user", "assistant"]):
            s.add(
                Message(
                    conversation_id=convo.conversation_id,
                    role=role,
                    content=f"msg-{i}",
                    created_at=base + timedelta(seconds=i),
                )
            )
        await s.commit()

    async with session_factory() as s:
        result = await repo.get_with_messages(
            s, tenant_id, user_id, convo.conversation_id
        )

    assert result is not None
    got_convo, msgs = result
    assert got_convo.conversation_id == convo.conversation_id
    assert [m.content for m in msgs] == ["msg-0", "msg-1", "msg-2", "msg-3"]
    assert [m.role for m in msgs] == ["user", "assistant", "user", "assistant"]


@pytest.mark.asyncio
async def test_get_returns_none_when_cross_user(session_factory):
    """A different user in the same tenant cannot read another's conversation."""
    from rag_service.conversations import repository as repo

    tenant_id, user_a = await _make_tenant_and_user(
        session_factory, tenant_id="t-shared", email="a@example.com"
    )
    _, user_b = await _make_tenant_and_user(
        session_factory, tenant_id="t-shared", email="b@example.com"
    )

    async with session_factory() as s:
        convo = await repo.create_conversation(s, tenant_id, user_a, title="a-only")

    # User B asking for user A's conversation → None.
    async with session_factory() as s:
        result = await repo.get_with_messages(
            s, tenant_id, user_b, convo.conversation_id
        )
    assert result is None

    # And cross-tenant ask returns None too.
    async with session_factory() as s:
        result = await repo.get_with_messages(
            s, "t-other", user_a, convo.conversation_id
        )
    assert result is None


@pytest.mark.asyncio
async def test_delete_conversation_returns_true_on_success(session_factory):
    """Deleting a real row returns True and removes the row."""
    from rag_service.conversations import repository as repo
    from rag_service.db.models import Conversation

    tenant_id, user_id = await _make_tenant_and_user(session_factory)

    async with session_factory() as s:
        convo = await repo.create_conversation(s, tenant_id, user_id)

    async with session_factory() as s:
        ok = await repo.delete_conversation(
            s, tenant_id, user_id, convo.conversation_id
        )
    assert ok is True

    async with session_factory() as s:
        rows = (await s.execute(select(Conversation))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_delete_conversation_returns_false_for_missing(session_factory):
    """Deleting a non-existent / cross-user row returns False."""
    from rag_service.conversations import repository as repo
    from rag_service.db.models import Conversation

    tenant_id, user_a = await _make_tenant_and_user(
        session_factory, tenant_id="t-shared", email="a@example.com"
    )
    _, user_b = await _make_tenant_and_user(
        session_factory, tenant_id="t-shared", email="b@example.com"
    )

    # Random uuid that doesn't exist → False.
    async with session_factory() as s:
        ok = await repo.delete_conversation(s, tenant_id, user_a, uuid.uuid4())
    assert ok is False

    # Real row but belonging to user_a; user_b's delete must be a no-op.
    async with session_factory() as s:
        convo = await repo.create_conversation(s, tenant_id, user_a)

    async with session_factory() as s:
        ok = await repo.delete_conversation(
            s, tenant_id, user_b, convo.conversation_id
        )
    assert ok is False

    # Row still there.
    async with session_factory() as s:
        rows = (await s.execute(select(Conversation))).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_append_message_bumps_updated_at(session_factory):
    """Appending a message updates ``conversations.updated_at``."""
    from rag_service.conversations import repository as repo
    from rag_service.db.models import Conversation

    tenant_id, user_id = await _make_tenant_and_user(session_factory)

    async with session_factory() as s:
        convo = await repo.create_conversation(s, tenant_id, user_id, title="t")
    original_updated_at = convo.updated_at

    # Sleep just enough to ensure datetime.now(...) is strictly greater.
    await asyncio.sleep(0.01)

    async with session_factory() as s:
        msg = await repo.append_message(
            s, convo.conversation_id, role="user", content="hi", sources=None
        )
    assert msg.message_id is not None
    assert msg.role == "user"
    assert msg.content == "hi"

    async with session_factory() as s:
        refreshed = (
            await s.execute(
                select(Conversation).where(
                    Conversation.conversation_id == convo.conversation_id
                )
            )
        ).scalar_one()

    assert refreshed.updated_at > original_updated_at


@pytest.mark.asyncio
async def test_recent_history_returns_n_most_recent_in_chronological_order(
    session_factory,
):
    """``recent_history`` returns the trailing N messages, oldest-first."""
    from rag_service.conversations import repository as repo
    from rag_service.db.models import Message

    tenant_id, user_id = await _make_tenant_and_user(session_factory)

    async with session_factory() as s:
        convo = await repo.create_conversation(s, tenant_id, user_id, title="t")

    # Insert 6 messages with strictly-increasing created_at so ordering
    # is deterministic regardless of clock resolution.
    async with session_factory() as s:
        base = datetime.now(timezone.utc)
        for i in range(6):
            s.add(
                Message(
                    conversation_id=convo.conversation_id,
                    role="user" if i % 2 == 0 else "assistant",
                    content=f"m{i}",
                    created_at=base + timedelta(seconds=i),
                )
            )
        await s.commit()

    async with session_factory() as s:
        history = await repo.recent_history(s, convo.conversation_id, n=3)

    assert [m.content for m in history] == ["m3", "m4", "m5"]

    # n larger than message count returns all rows, still chronological.
    async with session_factory() as s:
        all_history = await repo.recent_history(s, convo.conversation_id, n=100)
    assert [m.content for m in all_history] == ["m0", "m1", "m2", "m3", "m4", "m5"]
