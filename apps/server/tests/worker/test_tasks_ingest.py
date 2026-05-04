"""Tests for ``rag_service.worker.tasks.ingest_document``.

We never stand up a real database here. Instead the tests monkey-patch
the small ``_load_document`` / ``_mark_*`` helpers and the
``_open_session`` factory inside the ``tasks`` module so the task body
runs end-to-end against in-memory state. Redis is provided by
``fakeredis.aioredis`` so the tenant lock takes the same code path as
production — the lock is the bit we most want to exercise unmocked.
"""

from __future__ import annotations

# Required env vars must be set before importing ``rag_service`` modules
# (the ``settings`` singleton is constructed lazily at first attribute
# access, so any import that touches ``settings`` will trip on missing
# vars).
import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@h:5432/dbn")
os.environ.setdefault("REDIS_URL", "redis://x")
os.environ.setdefault("INTERNAL_TOKEN", "x" * 64)
os.environ.setdefault("LLM_BASE_URL", "http://llm")
os.environ.setdefault("LLM_API_KEY", "x")
os.environ.setdefault("LLM_MODEL", "m")
os.environ.setdefault("EMBEDDING_BASE_URL", "http://emb")
os.environ.setdefault("EMBEDDING_API_KEY", "x")
os.environ.setdefault("EMBEDDING_MODEL", "e")
os.environ.setdefault("PARSER_MODE", "default")
os.environ.setdefault("DATA_DIR", "/tmp/rag_tasks_test_data")

import contextlib  # noqa: E402
from typing import Any  # noqa: E402
from unittest.mock import AsyncMock, MagicMock  # noqa: E402

import fakeredis.aioredis as fake_aioredis  # noqa: E402
import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from rag_service.parsers.mineru_cloud import MineruCloudTimeoutError  # noqa: E402
from rag_service.worker import tasks as tasks_mod  # noqa: E402
from rag_service.worker.locks import LockBusy, tenant_ingest_lock  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def redis_client():
    """Fresh fakeredis client per test."""
    server = fake_aioredis.FakeRedis(decode_responses=True)
    try:
        await server.flushdb()
    except Exception:
        pass
    try:
        yield server
    finally:
        try:
            await server.aclose()
        except AttributeError:
            await server.close()


class _FakeSession:
    """Stand-in :class:`AsyncSession` that simply records any execute()
    calls. The tests monkeypatch the higher-level ``_load_document`` /
    ``_mark_*`` helpers, so this object never has its ``execute`` exercised
    in the tested code paths — but we still need a context manager that
    looks/acts session-shaped.
    """

    def __init__(self) -> None:
        self.executed: list[Any] = []

    async def execute(self, stmt: Any) -> Any:  # pragma: no cover - guard
        self.executed.append(stmt)
        return MagicMock()


@pytest.fixture
def fake_session_factory(monkeypatch: pytest.MonkeyPatch) -> _FakeSession:
    """Patch ``_open_session`` so the task uses an in-memory session."""
    session = _FakeSession()

    @contextlib.asynccontextmanager
    async def _factory():
        yield session

    monkeypatch.setattr(tasks_mod, "_open_session", lambda: _factory())
    return session


@pytest.fixture
def patch_db_helpers(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    """Monkey-patch the DB helpers and return the mocks for assertions.

    Each helper is replaced by an ``AsyncMock``; ``_load_document`` is left
    to the per-test default (returns a row) and individual tests may
    override it with ``mocks["load"].return_value = ...``.
    """
    mocks = {
        "load": AsyncMock(
            return_value={
                "document_id": "doc-1",
                "tenant_id": "tenant-A",
                "storage_path": "/tmp/dummy.pdf",
            }
        ),
        "mark_running": AsyncMock(),
        "mark_done": AsyncMock(),
        "mark_failed": AsyncMock(),
        "doc_indexed": AsyncMock(),
        "doc_failed": AsyncMock(),
    }
    monkeypatch.setattr(tasks_mod, "_load_document", mocks["load"])
    monkeypatch.setattr(tasks_mod, "_mark_job_running", mocks["mark_running"])
    monkeypatch.setattr(tasks_mod, "_mark_job_done", mocks["mark_done"])
    monkeypatch.setattr(tasks_mod, "_mark_job_failed", mocks["mark_failed"])
    monkeypatch.setattr(tasks_mod, "_mark_document_indexed", mocks["doc_indexed"])
    monkeypatch.setattr(tasks_mod, "_mark_document_failed", mocks["doc_failed"])
    return mocks


@pytest.fixture
def patch_rag_cache(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch ``rag_factory.get_cache`` to return a fake cache.

    The fake cache's ``.get(tenant_id)`` returns a fresh ``MagicMock``
    RAGAnything per call whose ``process_document_complete`` is an
    ``AsyncMock``. Tests can grab the instance via ``cache.get.return_value``
    after the task has run, or pre-set a side_effect/return_value before.
    """
    fake_rag = MagicMock(name="FakeRAGAnything")
    fake_rag.process_document_complete = AsyncMock()

    fake_cache = MagicMock(name="FakeRAGAnythingCache")
    fake_cache.get = AsyncMock(return_value=fake_rag)

    monkeypatch.setattr(tasks_mod.rag_factory, "get_cache", lambda: fake_cache)
    # Stash the rag instance on the cache mock so tests can reach it.
    fake_cache._rag = fake_rag
    return fake_cache


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_happy_path(
    redis_client,
    fake_session_factory,
    patch_db_helpers,
    patch_rag_cache,
):
    """A clean run: doc -> indexed, job -> done, reload notify fired."""
    ctx = {"redis": redis_client}

    # Subscribe before the task runs so we can observe the publish.
    pubsub = redis_client.pubsub()
    await pubsub.subscribe("tenant_reload:tenant-A")
    # Drain the initial subscribe-confirmation message.
    await pubsub.get_message(timeout=1.0)

    result = await tasks_mod.ingest_document(
        ctx, "tenant-A", "doc-1", retries_remaining=2
    )

    assert result == {"status": "indexed"}
    patch_db_helpers["mark_running"].assert_awaited_once()
    patch_db_helpers["doc_indexed"].assert_awaited_once()
    patch_db_helpers["mark_done"].assert_awaited_once()
    patch_db_helpers["mark_failed"].assert_not_awaited()
    patch_db_helpers["doc_failed"].assert_not_awaited()
    patch_rag_cache._rag.process_document_complete.assert_awaited_once()

    msg = await pubsub.get_message(timeout=1.0)
    assert msg is not None
    assert msg["channel"] == "tenant_reload:tenant-A"
    assert msg["data"] == "1"

    await pubsub.unsubscribe()
    await pubsub.aclose()


async def test_parser_error_retried(
    redis_client,
    fake_session_factory,
    patch_db_helpers,
    patch_rag_cache,
):
    """A MinerU timeout with retries left re-raises so arq retries us."""
    patch_rag_cache._rag.process_document_complete = AsyncMock(
        side_effect=MineruCloudTimeoutError("upstream slow")
    )

    ctx = {"redis": redis_client}

    with pytest.raises(MineruCloudTimeoutError):
        await tasks_mod.ingest_document(
            ctx, "tenant-A", "doc-1", retries_remaining=2
        )

    # Job started running, but the failure is NOT persisted yet — arq will
    # retry, and only on the final attempt do we mark it failed.
    patch_db_helpers["mark_running"].assert_awaited_once()
    patch_db_helpers["mark_failed"].assert_not_awaited()
    patch_db_helpers["doc_failed"].assert_not_awaited()
    patch_db_helpers["mark_done"].assert_not_awaited()
    patch_db_helpers["doc_indexed"].assert_not_awaited()


async def test_parser_error_no_retries_left_marks_failed(
    redis_client,
    fake_session_factory,
    patch_db_helpers,
    patch_rag_cache,
):
    """With ``retries_remaining=0`` a parser error becomes a permanent fail."""
    patch_rag_cache._rag.process_document_complete = AsyncMock(
        side_effect=MineruCloudTimeoutError("final attempt"),
    )

    ctx = {"redis": redis_client}
    result = await tasks_mod.ingest_document(
        ctx, "tenant-A", "doc-1", retries_remaining=0
    )

    assert result["status"] == "failed"
    patch_db_helpers["mark_failed"].assert_awaited_once()
    patch_db_helpers["doc_failed"].assert_awaited_once()


async def test_llm_error_fails_fast(
    redis_client,
    fake_session_factory,
    patch_db_helpers,
    patch_rag_cache,
):
    """An LLM 401 (auth) is permanent — no retry, mark failed, swallow."""
    fake_response = httpx.Response(
        status_code=401,
        request=httpx.Request("POST", "https://llm.example/chat"),
    )
    patch_rag_cache._rag.process_document_complete = AsyncMock(
        side_effect=httpx.HTTPStatusError(
            "401 Unauthorized", request=fake_response.request, response=fake_response
        )
    )

    ctx = {"redis": redis_client}
    # Should NOT raise — the task swallows after persisting failure.
    result = await tasks_mod.ingest_document(
        ctx, "tenant-A", "doc-1", retries_remaining=2
    )

    assert result["status"] == "failed"
    patch_db_helpers["mark_failed"].assert_awaited_once()
    patch_db_helpers["doc_failed"].assert_awaited_once()
    patch_db_helpers["doc_indexed"].assert_not_awaited()
    patch_db_helpers["mark_done"].assert_not_awaited()


async def test_lock_busy_reraises(
    redis_client,
    fake_session_factory,
    patch_db_helpers,
    patch_rag_cache,
):
    """If the per-tenant lock is held, ingest_document re-raises LockBusy."""
    ctx = {"redis": redis_client}

    # Pre-occupy the lock — a second client holds it for the duration of
    # this test so our task must fail to acquire.
    holder = fake_aioredis.FakeRedis(
        server=redis_client.connection_pool.connection_kwargs.get("server"),
        decode_responses=True,
    )
    # FakeRedis with no shared server param above wouldn't see the same
    # state — set the key directly through the same client instead.
    await redis_client.set("tenant_lock:tenant-A", "someone-else", ex=600)

    try:
        with pytest.raises(LockBusy):
            await tasks_mod.ingest_document(
                ctx, "tenant-A", "doc-1", retries_remaining=2
            )
    finally:
        await redis_client.delete("tenant_lock:tenant-A")
        try:
            await holder.aclose()
        except AttributeError:
            await holder.close()

    # Nothing in the DB should have been touched if we never entered the
    # critical section.
    patch_db_helpers["mark_running"].assert_not_awaited()
    patch_db_helpers["mark_failed"].assert_not_awaited()


async def test_unknown_document_id_marks_job_failed(
    redis_client,
    fake_session_factory,
    patch_db_helpers,
    patch_rag_cache,
):
    """No matching documents row -> job marked failed, document untouched."""
    patch_db_helpers["load"].return_value = None
    ctx = {"redis": redis_client}

    result = await tasks_mod.ingest_document(
        ctx, "tenant-A", "ghost-doc", retries_remaining=2
    )

    assert result["status"] == "failed"
    assert "not_found" in result["reason"] or "not found" in result["reason"]
    patch_db_helpers["mark_failed"].assert_awaited_once()
    # The persisted error message should mention the missing doc id.
    failed_call = patch_db_helpers["mark_failed"].await_args
    assert "not found" in failed_call.kwargs.get("error_message", "")
    # Crucially, we don't touch the documents row when there isn't one.
    patch_db_helpers["doc_failed"].assert_not_awaited()
    patch_db_helpers["mark_running"].assert_not_awaited()
    patch_rag_cache._rag.process_document_complete.assert_not_called()


# ---------------------------------------------------------------------------
# Helper-classifier tests (cheap, but worth the line — they pin the
# heuristic so a future refactor can't silently broaden the LLM bucket).
# ---------------------------------------------------------------------------


def test_is_parser_error_recognises_mineru_timeout():
    assert tasks_mod._is_parser_error(MineruCloudTimeoutError("x"))


def test_is_parser_error_recognises_qualname_match():
    class FancyMineruError(RuntimeError):
        pass

    assert tasks_mod._is_parser_error(FancyMineruError("y"))


def test_is_llm_error_recognises_httpx_status_error():
    req = httpx.Request("GET", "https://x")
    resp = httpx.Response(500, request=req)
    assert tasks_mod._is_llm_error(
        httpx.HTTPStatusError("boom", request=req, response=resp)
    )


def test_is_llm_error_negative_for_plain_exception():
    assert not tasks_mod._is_llm_error(ValueError("nope"))
