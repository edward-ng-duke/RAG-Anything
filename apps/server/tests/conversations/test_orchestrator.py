"""Tests for ``rag_service.conversations.orchestrator``.

The orchestrator is the boundary between the persistent conversation
store and the per-tenant ``RAGAnything`` cache. We exercise it against
an in-memory SQLite (same PG→SQLite metadata patch the repository tests
use) plus a stubbed RAG instance that the test injects via
``monkeypatch`` of :func:`rag_service.core.rag_factory.get_cache`.

Coverage:
* ``test_streams_delta_then_done`` — string response chunks into deltas
  followed by a single ``done`` event.
* ``test_persists_user_and_assistant_messages`` — both turns hit the DB.
* ``test_handles_dict_response_with_sources`` — dict-shaped response
  surfaces ``sources`` on the terminal ``done`` event.
* ``test_error_yields_error_event_no_assistant_message`` — RAG raises:
  the user message is persisted, no assistant row, single error event.
* ``test_history_passed_to_rag`` — pre-seeded prior turns are forwarded
  via ``conversation_history``, with the just-added user message dropped.
* ``test_vlm_enhanced_routes_to_correct_method`` — the ``vlm_enhanced``
  flag dispatches to ``aquery_vlm_enhanced`` instead of ``aquery``.
"""

from __future__ import annotations

# conftest.py at tests/ sets the required env vars. We override DATA_DIR
# locally so each test process gets a writable scratch dir.
import os  # noqa: E402

os.environ.setdefault("DATA_DIR", "/tmp/rag_conversations_orch_test")

import uuid  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB, UUID  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402
from sqlalchemy.schema import ColumnDefault  # noqa: E402


# ---------------------------------------------------------------------------
# PG → SQLite schema patches (mirrors tests/conversations/test_repository.py)
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
# Stub RAG + cache
# ---------------------------------------------------------------------------


class _StubRAG:
    """Minimal stand-in for :class:`RAGAnything` used by the orchestrator.

    Records every ``aquery`` / ``aquery_vlm_enhanced`` call so the test
    can assert on the keyword arguments the orchestrator forwarded.
    """

    def __init__(
        self,
        *,
        response=None,
        vlm_response=None,
        raises: Exception | None = None,
        expose_vlm: bool = True,
    ) -> None:
        self._response = response
        self._vlm_response = vlm_response if vlm_response is not None else response
        self._raises = raises
        self.aquery_calls: list[dict] = []
        self.vlm_calls: list[dict] = []
        if not expose_vlm:
            # Drop the attribute so ``hasattr`` returns False — the
            # orchestrator falls back to ``aquery``.
            del self.aquery_vlm_enhanced

    async def aquery(self, prompt, **kwargs):
        self.aquery_calls.append({"prompt": prompt, **kwargs})
        if self._raises is not None:
            raise self._raises
        return self._response

    async def aquery_vlm_enhanced(self, prompt, **kwargs):
        self.vlm_calls.append({"prompt": prompt, **kwargs})
        if self._raises is not None:
            raise self._raises
        return self._vlm_response


class _StubCache:
    def __init__(self, rag: _StubRAG) -> None:
        self._rag = rag

    async def get(self, tenant_id: str) -> _StubRAG:  # noqa: ARG002
        return self._rag


@pytest.fixture
def install_stub_rag(monkeypatch):
    """Return a callable that swaps :func:`get_cache` for one returning ``rag``."""

    def _install(rag: _StubRAG) -> _StubCache:
        cache = _StubCache(rag)
        # Patch the symbol the orchestrator imported.
        monkeypatch.setattr(
            "rag_service.conversations.orchestrator.get_cache",
            lambda: cache,
        )
        return cache

    return _install


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


async def _make_tenant_user_convo(
    SessionLocal,
    *,
    tenant_id: str = "t-alpha",
) -> tuple[str, uuid.UUID, uuid.UUID]:
    """Insert a Tenant + User + Conversation; return their ids."""
    from rag_service.db.models import Conversation, Tenant, User

    async with SessionLocal() as s:
        s.add(Tenant(tenant_id=tenant_id, display_name=tenant_id))
        user = User(
            email=f"{uuid.uuid4().hex[:8]}@example.com",
            password_hash="x",
            display_name="u",
        )
        s.add(user)
        await s.commit()
        await s.refresh(user)
        convo = Conversation(tenant_id=tenant_id, user_id=user.user_id, title="t")
        s.add(convo)
        await s.commit()
        await s.refresh(convo)
        return tenant_id, user.user_id, convo.conversation_id


async def _collect(stream) -> list[dict]:
    """Drain an async iterator into a list."""
    out: list[dict] = []
    async for ev in stream:
        out.append(ev)
    return out


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streams_delta_then_done(session_factory, install_stub_rag):
    """A plain string response yields >=1 delta then exactly one done."""
    from rag_service.conversations.orchestrator import stream_assistant_response

    tenant_id, user_id, convo_id = await _make_tenant_user_convo(session_factory)
    rag = _StubRAG(response="hello world")
    install_stub_rag(rag)

    async with session_factory() as s:
        events = await _collect(
            stream_assistant_response(
                s, tenant_id, user_id, convo_id, "what is up?",
            )
        )

    deltas = [e for e in events if e["event"] == "delta"]
    dones = [e for e in events if e["event"] == "done"]
    errors = [e for e in events if e["event"] == "error"]

    assert errors == []
    assert len(dones) == 1
    assert len(deltas) >= 1
    # Re-assembling deltas yields the original answer.
    assert "".join(e["content"] for e in deltas) == "hello world"
    assert dones[0]["sources"] == []


@pytest.mark.asyncio
async def test_persists_user_and_assistant_messages(
    session_factory, install_stub_rag
):
    """After a successful stream, both the user + assistant rows exist."""
    from rag_service.conversations.orchestrator import stream_assistant_response
    from rag_service.db.models import Message

    tenant_id, user_id, convo_id = await _make_tenant_user_convo(session_factory)
    install_stub_rag(_StubRAG(response="answered"))

    async with session_factory() as s:
        await _collect(
            stream_assistant_response(
                s, tenant_id, user_id, convo_id, "ask me anything"
            )
        )

    async with session_factory() as s:
        rows = (
            await s.execute(
                select(Message)
                .where(Message.conversation_id == convo_id)
                .order_by(Message.created_at.asc())
            )
        ).scalars().all()

    roles = [m.role for m in rows]
    contents = [m.content for m in rows]
    assert roles == ["user", "assistant"]
    assert contents == ["ask me anything", "answered"]


@pytest.mark.asyncio
async def test_handles_dict_response_with_sources(
    session_factory, install_stub_rag
):
    """A dict response with ``sources`` propagates them on the done event."""
    from rag_service.conversations.orchestrator import stream_assistant_response
    from rag_service.db.models import Message

    tenant_id, user_id, convo_id = await _make_tenant_user_convo(session_factory)
    payload = {
        "answer": "the sky is blue",
        "sources": [{"doc_id": "d1", "score": 0.9}, {"doc_id": "d2"}],
    }
    install_stub_rag(_StubRAG(response=payload))

    async with session_factory() as s:
        events = await _collect(
            stream_assistant_response(s, tenant_id, user_id, convo_id, "why?")
        )

    done = next(e for e in events if e["event"] == "done")
    assert done["sources"] == payload["sources"]
    # Stored assistant row carries the same sources, wrapped per repo contract.
    async with session_factory() as s:
        assistant_row = (
            await s.execute(
                select(Message).where(
                    Message.conversation_id == convo_id,
                    Message.role == "assistant",
                )
            )
        ).scalar_one()
    assert assistant_row.sources == {"sources": payload["sources"]}
    assert assistant_row.content == "the sky is blue"


@pytest.mark.asyncio
async def test_error_yields_error_event_no_assistant_message(
    session_factory, install_stub_rag
):
    """When the RAG raises, only the user message is persisted."""
    from rag_service.conversations.orchestrator import stream_assistant_response
    from rag_service.db.models import Message

    tenant_id, user_id, convo_id = await _make_tenant_user_convo(session_factory)
    install_stub_rag(_StubRAG(raises=RuntimeError("boom")))

    async with session_factory() as s:
        events = await _collect(
            stream_assistant_response(
                s, tenant_id, user_id, convo_id, "doomed prompt"
            )
        )

    # Exactly one error event, no done, no deltas.
    assert [e["event"] for e in events] == ["error"]
    assert events[0]["message"] == "boom"

    async with session_factory() as s:
        rows = (
            await s.execute(
                select(Message)
                .where(Message.conversation_id == convo_id)
                .order_by(Message.created_at.asc())
            )
        ).scalars().all()
    assert [m.role for m in rows] == ["user"]
    assert [m.content for m in rows] == ["doomed prompt"]


@pytest.mark.asyncio
async def test_history_passed_to_rag(session_factory, install_stub_rag):
    """Pre-seeded prior turns are forwarded; current user msg is dropped."""
    from rag_service.conversations.orchestrator import stream_assistant_response
    from rag_service.db.models import Message

    tenant_id, user_id, convo_id = await _make_tenant_user_convo(session_factory)

    # Seed two prior turns with timestamps strictly *in the past* — the
    # orchestrator's own user-message insert uses ``CURRENT_TIMESTAMP``,
    # and SQLite has 1-second resolution, so we need at least a couple
    # of seconds of headroom for ``recent_history`` to order things
    # correctly.
    async with session_factory() as s:
        base = datetime.now(timezone.utc) - timedelta(minutes=5)
        s.add(
            Message(
                conversation_id=convo_id,
                role="user",
                content="prior-q",
                created_at=base,
            )
        )
        s.add(
            Message(
                conversation_id=convo_id,
                role="assistant",
                content="prior-a",
                created_at=base + timedelta(seconds=1),
            )
        )
        await s.commit()

    rag = _StubRAG(response="ok")
    install_stub_rag(rag)

    async with session_factory() as s:
        await _collect(
            stream_assistant_response(s, tenant_id, user_id, convo_id, "new-q")
        )

    assert len(rag.aquery_calls) == 1
    call = rag.aquery_calls[0]
    assert call["prompt"] == "new-q"
    # Conversation history is the two PRIOR turns — the just-appended
    # user message is excluded because it's already the current prompt.
    assert call["conversation_history"] == [
        {"role": "user", "content": "prior-q"},
        {"role": "assistant", "content": "prior-a"},
    ]


@pytest.mark.asyncio
async def test_vlm_enhanced_routes_to_correct_method(
    session_factory, install_stub_rag
):
    """``vlm_enhanced=True`` dispatches to ``aquery_vlm_enhanced``."""
    from rag_service.conversations.orchestrator import stream_assistant_response

    tenant_id, user_id, convo_id = await _make_tenant_user_convo(session_factory)
    rag = _StubRAG(response="text-answer", vlm_response="vlm-answer")
    install_stub_rag(rag)

    async with session_factory() as s:
        events = await _collect(
            stream_assistant_response(
                s,
                tenant_id,
                user_id,
                convo_id,
                "describe this picture",
                vlm_enhanced=True,
            )
        )

    # Only the VLM path was invoked.
    assert len(rag.vlm_calls) == 1
    assert rag.aquery_calls == []
    # The streamed answer is the VLM response.
    deltas = [e for e in events if e["event"] == "delta"]
    assert "".join(d["content"] for d in deltas) == "vlm-answer"
