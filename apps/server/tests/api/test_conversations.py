"""Tests for ``rag_service.api.routers.conversations`` — /v1/conversations.

The DB is a fresh in-memory SQLite per test (with the same PG→SQLite
metadata patch the rest of the API suite uses). The orchestrator is
swapped via ``monkeypatch`` for a stub that emits a deterministic
``delta`` + ``done`` so SSE assertions don't depend on a real RAG
instance.

Coverage
--------

* ``test_list_returns_user_convos`` — list endpoint returns rows owned
  by the caller and skips other users' rows in the same tenant.
* ``test_create_returns_new_convo`` — POST creates a row and returns it
  with a 201.
* ``test_get_returns_messages_chronological`` — detail endpoint orders
  messages oldest-first.
* ``test_get_404_when_cross_user`` — another user's conversation is 404
  (never 200/403).
* ``test_delete_204_then_404`` — DELETE returns 204 then 404 on the
  second call.
* ``test_send_message_streams_sse_events`` — POST /messages returns an
  ``text/event-stream`` body containing ``event: delta`` and
  ``event: done``.
* ``test_send_message_404_for_unknown_convo`` — POST /messages on a
  random uuid returns 404 without invoking the orchestrator.
"""

from __future__ import annotations

# conftest.py at tests/ sets the required env vars. We override DATA_DIR
# locally so each test process gets a writable scratch dir.
import os  # noqa: E402

os.environ.setdefault("DATA_DIR", "/tmp/rag_conversations_api_test")

import json  # noqa: E402
import uuid  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
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
# Per-test app + DB factory
# ---------------------------------------------------------------------------


class _MockUser:
    """Stand-in for the ``User`` row that ``current_user`` returns.

    The router only reads ``user.user_id`` so that's all we need to
    expose. Tests construct one per-call so a single fixture serves the
    "two users in one tenant" cases without retaining cross-test state.
    """

    def __init__(self, user_id: uuid.UUID) -> None:
        self.user_id = user_id
        self.is_active = True


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


def _make_app(
    SessionLocal,
    *,
    user_id: uuid.UUID,
    tenant_id: str,
) -> FastAPI:
    """Build a FastAPI app wired to ``SessionLocal`` and a fixed identity.

    ``current_user`` and ``current_tenant`` are overridden so tests don't
    need to mint JWTs — auth is tested elsewhere; here we exercise the
    routes' behaviour given a known caller. ``get_db_session`` yields a
    fresh session per request so a stale row from a previous request
    doesn't bleed into the next one.
    """
    from rag_service.api.auth import current_tenant, current_user
    from rag_service.api.deps import get_db_session
    from rag_service.api.routers.conversations import router as convos_router

    app = FastAPI()
    app.include_router(convos_router)

    async def _db_override():
        async with SessionLocal() as s:
            try:
                yield s
            except Exception:
                await s.rollback()
                raise

    async def _user_override() -> _MockUser:
        return _MockUser(user_id=user_id)

    async def _tenant_override() -> str:
        return tenant_id

    app.dependency_overrides[get_db_session] = _db_override
    app.dependency_overrides[current_user] = _user_override
    app.dependency_overrides[current_tenant] = _tenant_override
    return app


async def _seed_tenant_user(
    SessionLocal,
    *,
    tenant_id: str = "t-alpha",
    email: str | None = None,
) -> uuid.UUID:
    """Insert a Tenant + User and return ``user_id``.

    The tenant row is upserted (``select`` then ``add`` on miss) so
    multiple users can share a tenant without tripping the PK.
    """
    from sqlalchemy import select
    from rag_service.db.models import Tenant, User

    user_email = email or f"{uuid.uuid4().hex[:8]}@example.com"
    async with SessionLocal() as s:
        existing = (
            await s.execute(select(Tenant).where(Tenant.tenant_id == tenant_id))
        ).scalar_one_or_none()
        if existing is None:
            s.add(Tenant(tenant_id=tenant_id, display_name=tenant_id))
        user = User(
            email=user_email,
            password_hash="x",
            display_name=user_email.split("@", 1)[0],
        )
        s.add(user)
        await s.commit()
        await s.refresh(user)
        return user.user_id


# ---------------------------------------------------------------------------
# Stub orchestrator — swapped in via monkeypatch so SSE tests don't need a RAG
# ---------------------------------------------------------------------------


def _install_stub_orchestrator(monkeypatch, *, answer: str = "hi there") -> dict:
    """Patch the orchestrator the router imported with a deterministic stub.

    Returns a ``calls`` dict that records every invocation so the test can
    assert the orchestrator was (or wasn't) called and inspect its args.
    """
    calls: dict = {"count": 0, "args": []}

    async def _fake_stream(
        db,
        tenant_id,
        user_id,
        conversation_id,
        content,
        *,
        mode="hybrid",
        top_k=10,
        vlm_enhanced=False,
        history_window=10,
    ):
        calls["count"] += 1
        calls["args"].append(
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "conversation_id": conversation_id,
                "content": content,
                "mode": mode,
                "top_k": top_k,
                "vlm_enhanced": vlm_enhanced,
            }
        )
        # Minimal but realistic event sequence: one delta then done.
        yield {"event": "delta", "content": answer}
        yield {"event": "done", "sources": []}

    # Patch the symbol the router imported.
    monkeypatch.setattr(
        "rag_service.api.routers.conversations.stream_assistant_response",
        _fake_stream,
    )
    return calls


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_returns_user_convos(session_factory):
    """List returns the caller's rows and skips a sibling user's row."""
    from rag_service.db.models import Conversation

    user_a = await _seed_tenant_user(
        session_factory, tenant_id="t-alpha", email="a@example.com"
    )
    user_b = await _seed_tenant_user(
        session_factory, tenant_id="t-alpha", email="b@example.com"
    )

    # Two convos for user A, one for user B — all in the same tenant.
    async with session_factory() as s:
        s.add(Conversation(tenant_id="t-alpha", user_id=user_a, title="A1"))
        s.add(Conversation(tenant_id="t-alpha", user_id=user_a, title="A2"))
        s.add(Conversation(tenant_id="t-alpha", user_id=user_b, title="B1"))
        await s.commit()

    app = _make_app(session_factory, user_id=user_a, tenant_id="t-alpha")
    client = TestClient(app)
    r = client.get("/v1/conversations")
    assert r.status_code == 200, r.text
    body = r.json()
    titles = sorted(item["title"] for item in body["items"])
    assert titles == ["A1", "A2"]


@pytest.mark.asyncio
async def test_create_returns_new_convo(session_factory):
    """POST returns 201 with the freshly-inserted row's fields."""
    from sqlalchemy import select
    from rag_service.db.models import Conversation

    user_id = await _seed_tenant_user(session_factory, tenant_id="t-alpha")
    app = _make_app(session_factory, user_id=user_id, tenant_id="t-alpha")
    client = TestClient(app)

    r = client.post("/v1/conversations", json={"title": "first chat"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["title"] == "first chat"
    assert uuid.UUID(body["conversation_id"])
    assert body["created_at"]
    assert body["updated_at"]

    async with session_factory() as s:
        rows = (await s.execute(select(Conversation))).scalars().all()
    assert len(rows) == 1
    assert rows[0].title == "first chat"
    assert rows[0].user_id == user_id


@pytest.mark.asyncio
async def test_get_returns_messages_chronological(session_factory):
    """Detail endpoint surfaces messages in oldest-first order."""
    from rag_service.db.models import Conversation, Message

    user_id = await _seed_tenant_user(session_factory)

    async with session_factory() as s:
        convo = Conversation(tenant_id="t-alpha", user_id=user_id, title="t")
        s.add(convo)
        await s.commit()
        await s.refresh(convo)
        base = datetime.now(timezone.utc)
        for i, role in enumerate(["user", "assistant", "user", "assistant"]):
            s.add(
                Message(
                    conversation_id=convo.conversation_id,
                    role=role,
                    content=f"m-{i}",
                    created_at=base + timedelta(seconds=i),
                )
            )
        await s.commit()
        convo_id = convo.conversation_id

    app = _make_app(session_factory, user_id=user_id, tenant_id="t-alpha")
    client = TestClient(app)
    r = client.get(f"/v1/conversations/{convo_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["conversation"]["conversation_id"] == str(convo_id)
    contents = [m["content"] for m in body["messages"]]
    roles = [m["role"] for m in body["messages"]]
    assert contents == ["m-0", "m-1", "m-2", "m-3"]
    assert roles == ["user", "assistant", "user", "assistant"]


@pytest.mark.asyncio
async def test_get_404_when_cross_user(session_factory):
    """User B asking for User A's conversation gets a clean 404."""
    from rag_service.db.models import Conversation

    user_a = await _seed_tenant_user(
        session_factory, tenant_id="t-shared", email="a@example.com"
    )
    user_b = await _seed_tenant_user(
        session_factory, tenant_id="t-shared", email="b@example.com"
    )

    async with session_factory() as s:
        convo = Conversation(tenant_id="t-shared", user_id=user_a, title="a-only")
        s.add(convo)
        await s.commit()
        await s.refresh(convo)
        convo_id = convo.conversation_id

    # User B is the active caller.
    app = _make_app(session_factory, user_id=user_b, tenant_id="t-shared")
    client = TestClient(app)
    r = client.get(f"/v1/conversations/{convo_id}")
    assert r.status_code == 404
    assert r.json() == {"detail": "conversation not found"}


@pytest.mark.asyncio
async def test_delete_204_then_404(session_factory):
    """DELETE returns 204 on success then 404 on the same id second time."""
    from rag_service.db.models import Conversation

    user_id = await _seed_tenant_user(session_factory)

    async with session_factory() as s:
        convo = Conversation(tenant_id="t-alpha", user_id=user_id, title="t")
        s.add(convo)
        await s.commit()
        await s.refresh(convo)
        convo_id = convo.conversation_id

    app = _make_app(session_factory, user_id=user_id, tenant_id="t-alpha")
    client = TestClient(app)

    r1 = client.delete(f"/v1/conversations/{convo_id}")
    assert r1.status_code == 204
    assert r1.content == b""

    r2 = client.delete(f"/v1/conversations/{convo_id}")
    assert r2.status_code == 404
    assert r2.json() == {"detail": "conversation not found"}


@pytest.mark.asyncio
async def test_send_message_streams_sse_events(session_factory, monkeypatch):
    """POST /messages emits a text/event-stream body with delta + done."""
    from rag_service.db.models import Conversation

    user_id = await _seed_tenant_user(session_factory)

    async with session_factory() as s:
        convo = Conversation(tenant_id="t-alpha", user_id=user_id, title="t")
        s.add(convo)
        await s.commit()
        await s.refresh(convo)
        convo_id = convo.conversation_id

    calls = _install_stub_orchestrator(monkeypatch, answer="streamed answer")

    app = _make_app(session_factory, user_id=user_id, tenant_id="t-alpha")
    client = TestClient(app)
    r = client.post(
        f"/v1/conversations/{convo_id}/messages",
        json={"content": "hi", "mode": "local", "top_k": 7, "vlm_enhanced": True},
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")

    body = r.text
    # The wire format is ``event: <name>\ndata: <json>\n\n``.
    assert "event: delta" in body
    assert "event: done" in body

    # Each ``data:`` line is JSON we can decode end-to-end.
    delta_payloads = []
    done_payloads = []
    for chunk in body.split("\n\n"):
        if not chunk.strip():
            continue
        lines = chunk.splitlines()
        ev_line = next((l for l in lines if l.startswith("event:")), "")
        data_line = next((l for l in lines if l.startswith("data:")), "")
        if not data_line:
            continue
        payload = json.loads(data_line[len("data:") :].strip())
        if ev_line.endswith("delta"):
            delta_payloads.append(payload)
        elif ev_line.endswith("done"):
            done_payloads.append(payload)

    assert len(delta_payloads) >= 1
    assert "".join(p["content"] for p in delta_payloads) == "streamed answer"
    assert len(done_payloads) == 1

    # The orchestrator received the request fields verbatim.
    assert calls["count"] == 1
    args = calls["args"][0]
    assert args["content"] == "hi"
    assert args["mode"] == "local"
    assert args["top_k"] == 7
    assert args["vlm_enhanced"] is True
    assert args["conversation_id"] == convo_id


@pytest.mark.asyncio
async def test_send_message_404_for_unknown_convo(session_factory, monkeypatch):
    """POST /messages on a random uuid returns 404 and never calls the stub."""
    user_id = await _seed_tenant_user(session_factory)
    calls = _install_stub_orchestrator(monkeypatch)

    app = _make_app(session_factory, user_id=user_id, tenant_id="t-alpha")
    client = TestClient(app)
    r = client.post(
        f"/v1/conversations/{uuid.uuid4()}/messages",
        json={"content": "hi"},
    )
    assert r.status_code == 404
    assert r.json() == {"detail": "conversation not found"}
    assert calls["count"] == 0
