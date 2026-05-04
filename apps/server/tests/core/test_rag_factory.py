"""Tests for ``rag_service.core.rag_factory``: LRU cache, lifecycle, locking.

We never stand up real LightRAG / Postgres here. Instead we patch the
``RAGAnything`` symbol *inside the rag_factory module* (so the production
code's ``RAGAnything(...)`` call hits our fake). Each fake instance owns
an ``AsyncMock`` ``finalize_storages`` so eviction / shutdown can be
asserted against call counts.
"""

from __future__ import annotations

# Set required env vars before any rag_service import — the lazy
# ``settings`` singleton must be constructable at import time.
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
os.environ.setdefault("PARSER_MODE", "default")  # disable cloud-parser path
# Use a tmp data_dir so tenant_working_dir mkdir is harmless during build.
os.environ.setdefault("DATA_DIR", "/tmp/rag_factory_test_data")

import asyncio  # noqa: E402
from typing import Any  # noqa: E402
from unittest.mock import AsyncMock, MagicMock  # noqa: E402

import pytest  # noqa: E402

from rag_service.core import rag_factory as mod  # noqa: E402
from rag_service.core.paths import InvalidTenantIdError  # noqa: E402
from rag_service.core.rag_factory import RAGAnythingCache  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_instance() -> MagicMock:
    """A stand-in RAGAnything: anything attribute access works and
    ``finalize_storages`` is awaitable."""
    inst = MagicMock(name="FakeRAGAnything")
    inst.finalize_storages = AsyncMock()
    inst.initialize = None  # rag_factory tolerates a missing init hook
    return inst


def _patch_class(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace ``rag_factory.RAGAnything`` with a callable that returns a
    fresh fake instance per call. Returns the class-mock for assertions."""
    cls = MagicMock(name="RAGAnythingClass")

    def factory(**kwargs: Any) -> MagicMock:
        return _make_fake_instance()

    cls.side_effect = factory
    monkeypatch.setattr(mod, "RAGAnything", cls)
    # Also stub out RAGAnythingConfig: the real one reads env vars eagerly.
    monkeypatch.setattr(mod, "RAGAnythingConfig", MagicMock())
    # Skip the DSN env-var population: we don't want side effects on the
    # test process environment, and the cache only does it once anyway.
    monkeypatch.setattr(
        mod, "_populate_postgres_env_from_dsn", lambda _dsn: None
    )
    return cls


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_cache_hit_returns_same_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_class(monkeypatch)
    cache = RAGAnythingCache(capacity=4)

    a = await cache.get("tenant-a")
    b = await cache.get("tenant-a")

    assert a is b


async def test_cache_miss_builds_new(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cls = _patch_class(monkeypatch)
    cache = RAGAnythingCache(capacity=4)

    await cache.get("tenant-a")
    await cache.get("tenant-b")

    assert cls.call_count == 2


async def test_lru_eviction_when_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_class(monkeypatch)
    cache = RAGAnythingCache(capacity=2)

    t1 = await cache.get("tenant-1")
    t2 = await cache.get("tenant-2")
    # tenant-1 is the LRU; inserting tenant-3 should evict it.
    t3 = await cache.get("tenant-3")

    # Drain the eviction's background finalize task.
    await asyncio.gather(*cache._pending_finalisations, return_exceptions=True)

    t1.finalize_storages.assert_awaited_once()
    t2.finalize_storages.assert_not_called()
    t3.finalize_storages.assert_not_called()

    # tenant-1 should be gone; tenant-2 + tenant-3 still cached.
    assert "tenant-1" not in cache._cache
    assert "tenant-2" in cache._cache
    assert "tenant-3" in cache._cache


async def test_evict_removes_and_finalizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_class(monkeypatch)
    cache = RAGAnythingCache(capacity=4)

    inst = await cache.get("tenant-x")
    await cache.evict("tenant-x")

    # Drain the scheduled background finalize task.
    await asyncio.gather(*cache._pending_finalisations, return_exceptions=True)

    inst.finalize_storages.assert_awaited_once()
    assert "tenant-x" not in cache._cache


async def test_evict_unknown_tenant_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_class(monkeypatch)
    cache = RAGAnythingCache(capacity=4)

    # Must not raise even though the tenant was never built.
    await cache.evict("never-seen")
    assert cache._cache == {}


async def test_aclose_finalizes_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_class(monkeypatch)
    cache = RAGAnythingCache(capacity=4)

    a = await cache.get("tenant-a")
    b = await cache.get("tenant-b")
    c = await cache.get("tenant-c")

    await cache.aclose()

    a.finalize_storages.assert_awaited_once()
    b.finalize_storages.assert_awaited_once()
    c.finalize_storages.assert_awaited_once()
    assert cache._cache == {}


async def test_invalid_tenant_id_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_class(monkeypatch)
    cache = RAGAnythingCache(capacity=4)

    with pytest.raises(InvalidTenantIdError):
        await cache.get("../etc")
    with pytest.raises(InvalidTenantIdError):
        await cache.evict("../etc")


async def test_concurrent_get_same_tenant_one_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two concurrent ``get(tid)`` for the same tenant must build only once.

    We make the build observably slow by patching ``_build`` directly to
    sleep before returning a fake. The per-tenant build lock should
    serialise the second caller; on its turn the cache hit short-circuits
    so the builder runs exactly once.
    """
    cache = RAGAnythingCache(capacity=4)

    build_calls = 0

    async def slow_build(self_: RAGAnythingCache, tid: str) -> Any:
        nonlocal build_calls
        build_calls += 1
        await asyncio.sleep(0.05)
        return _make_fake_instance()

    monkeypatch.setattr(RAGAnythingCache, "_build", slow_build)

    a, b = await asyncio.gather(
        cache.get("tenant-z"), cache.get("tenant-z")
    )

    assert build_calls == 1
    assert a is b


async def test_concurrent_get_different_tenants_both_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-tenant lock must not serialise *across* tenants."""
    cache = RAGAnythingCache(capacity=4)

    build_calls = 0

    async def slow_build(self_: RAGAnythingCache, tid: str) -> Any:
        nonlocal build_calls
        build_calls += 1
        await asyncio.sleep(0.05)
        return _make_fake_instance()

    monkeypatch.setattr(RAGAnythingCache, "_build", slow_build)

    a, b = await asyncio.gather(
        cache.get("tenant-a"), cache.get("tenant-b")
    )

    assert build_calls == 2
    assert a is not b


async def test_capacity_validation() -> None:
    with pytest.raises(ValueError):
        RAGAnythingCache(capacity=0)


async def test_get_cache_returns_singleton() -> None:
    # Reset the lru_cache so we get a fresh build under our test settings.
    mod.get_cache.cache_clear()
    a = mod.get_cache()
    b = mod.get_cache()
    assert a is b


def test_populate_postgres_env_from_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Smoke-test the DSN parser against a typical asyncpg URL."""
    # Clear any pre-existing values so setdefault actually sets ours.
    for key in (
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DATABASE",
        "POSTGRES_SERVER_SETTINGS",
    ):
        monkeypatch.delenv(key, raising=False)

    mod._populate_postgres_env_from_dsn(
        "postgresql+asyncpg://alice:secret@db.example.com:6543/ragdb"
    )

    assert os.environ["POSTGRES_HOST"] == "db.example.com"
    assert os.environ["POSTGRES_PORT"] == "6543"
    assert os.environ["POSTGRES_USER"] == "alice"
    assert os.environ["POSTGRES_PASSWORD"] == "secret"
    assert os.environ["POSTGRES_DATABASE"] == "ragdb"
    assert "search_path=lightrag,public" in os.environ["POSTGRES_SERVER_SETTINGS"]
