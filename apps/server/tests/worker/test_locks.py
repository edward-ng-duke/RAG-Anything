"""Tests for ``rag_service.worker.locks``.

Prefers a real Redis via ``testcontainers`` so the Lua release script is
exercised against an actual server; falls back to ``fakeredis.aioredis``
when testcontainers (or Docker) is not available so the suite still runs
on dev laptops and minimal CI images.
"""

from __future__ import annotations

import asyncio
import importlib.util

import pytest
import pytest_asyncio
import redis.asyncio as aioredis

from rag_service.worker.locks import LockBusy, tenant_ingest_lock


def _try_start_real_redis():
    """Return a ``(container, url)`` tuple if testcontainers Redis works.

    Returns ``None`` when testcontainers is not installed or Docker isn't
    available so the caller can fall back to fakeredis.
    """
    if importlib.util.find_spec("testcontainers") is None:
        return None
    try:
        from testcontainers.redis import RedisContainer  # type: ignore
    except Exception:
        return None
    try:
        container = RedisContainer("redis:7-alpine")
        container.start()
    except Exception:
        return None
    try:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(6379)
        url = f"redis://{host}:{port}/0"
        return container, url
    except Exception:
        try:
            container.stop()
        except Exception:
            pass
        return None


@pytest.fixture
def redis_backend():
    """Per-test backend descriptor.

    Returns a 3-tuple ``(kind, url_or_none, shared_state)`` where
    ``shared_state`` is a fresh ``fakeredis.FakeServer`` for fake mode (so
    two clients in one test see the same data) or ``None`` for real
    Redis. Function-scoped so each test starts with a clean fake server.
    """
    started = _try_start_real_redis()
    if started is not None:
        container, url = started
        try:
            yield ("real", url, None)
        finally:
            try:
                container.stop()
            except Exception:
                pass
        return

    if importlib.util.find_spec("fakeredis") is None:
        pytest.skip("Neither testcontainers redis nor fakeredis is available.")
    import fakeredis  # type: ignore

    yield ("fake", None, fakeredis.FakeServer())


async def _new_client(backend) -> aioredis.Redis:
    """Make a fresh async Redis client for the active backend."""
    kind, url, shared = backend
    if kind == "real":
        return aioredis.from_url(url, decode_responses=True)
    import fakeredis.aioredis as fake_aioredis  # type: ignore

    return fake_aioredis.FakeRedis(server=shared, decode_responses=True)


@pytest_asyncio.fixture
async def redis_client(redis_backend):
    """Per-test async Redis client, flushed clean before yield."""
    client = await _new_client(redis_backend)
    try:
        await client.flushdb()
    except Exception:
        # Some fakeredis versions raise if DB is already empty; ignore.
        pass
    try:
        yield client
    finally:
        try:
            await client.aclose()
        except AttributeError:
            await client.close()


async def test_acquire_release_basic(redis_client):
    """Acquire then release; the key must be gone after the context exits."""
    tenant = "tenantA"
    async with tenant_ingest_lock(redis_client, tenant, ttl=30):
        assert await redis_client.get(f"tenant_lock:{tenant}") is not None
    assert await redis_client.get(f"tenant_lock:{tenant}") is None


async def test_concurrent_acquires_second_fails_fast(redis_client):
    """Second acquisition with acquire_timeout=0 must raise immediately."""
    tenant = "tenantB"
    async with tenant_ingest_lock(redis_client, tenant, ttl=30):
        with pytest.raises(LockBusy):
            async with tenant_ingest_lock(redis_client, tenant, ttl=30, acquire_timeout=0):
                pytest.fail("should not enter inner context")


async def test_acquire_with_timeout_succeeds_when_first_releases(redis_backend):
    """A waiting acquirer with timeout > release-delay must succeed."""
    client_a = await _new_client(redis_backend)
    client_b = await _new_client(redis_backend)
    tenant = "tenantC"
    try:
        await client_a.flushdb()
    except Exception:
        pass

    holder_released = asyncio.Event()
    waiter_acquired = asyncio.Event()

    async def holder():
        async with tenant_ingest_lock(client_a, tenant, ttl=30):
            await asyncio.sleep(0.3)
        holder_released.set()

    async def waiter():
        # Brief delay to ensure holder grabs the lock first.
        await asyncio.sleep(0.05)
        async with tenant_ingest_lock(client_b, tenant, ttl=30, acquire_timeout=2.0):
            waiter_acquired.set()

    try:
        await asyncio.wait_for(asyncio.gather(holder(), waiter()), timeout=5.0)
        assert holder_released.is_set()
        assert waiter_acquired.is_set()
        # Both ran to completion; key should be gone.
        assert await client_a.get(f"tenant_lock:{tenant}") is None
    finally:
        for c in (client_a, client_b):
            try:
                await c.aclose()
            except AttributeError:
                await c.close()


async def test_release_only_owner_token(redis_backend):
    """A second client running the release Lua must not free the lock."""
    from rag_service.worker.locks import RELEASE_SCRIPT

    client_a = await _new_client(redis_backend)
    client_b = await _new_client(redis_backend)
    tenant = "tenantD"
    try:
        await client_a.flushdb()
    except Exception:
        pass

    try:
        async with tenant_ingest_lock(client_a, tenant, ttl=30):
            # Simulate a different holder (or attacker) trying to release.
            wrong_token = "not-the-real-token"
            removed = await client_b.eval(
                RELEASE_SCRIPT, 1, f"tenant_lock:{tenant}", wrong_token
            )
            assert removed == 0
            # Lock still held by client_a -> client_b cannot acquire.
            with pytest.raises(LockBusy):
                async with tenant_ingest_lock(client_b, tenant, ttl=30, acquire_timeout=0):
                    pytest.fail("client_b must not acquire while A holds")
            # Key must still exist while A is inside its context.
            assert await client_a.get(f"tenant_lock:{tenant}") is not None
        # After A exits, key released by A's Lua call.
        assert await client_a.get(f"tenant_lock:{tenant}") is None
    finally:
        for c in (client_a, client_b):
            try:
                await c.aclose()
            except AttributeError:
                await c.close()


async def test_different_tenants_dont_block(redis_client):
    """Locks are scoped per tenant; two tenants can hold concurrently."""
    async with tenant_ingest_lock(redis_client, "tenantE", ttl=30):
        async with tenant_ingest_lock(redis_client, "tenantF", ttl=30, acquire_timeout=0):
            assert await redis_client.get("tenant_lock:tenantE") is not None
            assert await redis_client.get("tenant_lock:tenantF") is not None
    assert await redis_client.get("tenant_lock:tenantE") is None
    assert await redis_client.get("tenant_lock:tenantF") is None
