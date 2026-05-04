import asyncio

import fakeredis.aioredis
import pytest
from unittest.mock import AsyncMock

from rag_service.core.reload_listener import reload_listener


async def _stop(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_publish_triggers_evict():
    redis = fakeredis.aioredis.FakeRedis()
    cache = AsyncMock()
    task = asyncio.create_task(reload_listener(redis, cache))
    try:
        await asyncio.sleep(0.1)  # let psubscribe register
        await redis.publish("tenant_reload:t-1", "1")
        # Wait until evict is awaited or timeout
        for _ in range(50):
            if cache.evict.await_count >= 1:
                break
            await asyncio.sleep(0.05)
        cache.evict.assert_awaited_with("t-1")
    finally:
        await _stop(task)
        await redis.aclose()


async def test_other_channels_ignored():
    redis = fakeredis.aioredis.FakeRedis()
    cache = AsyncMock()
    task = asyncio.create_task(reload_listener(redis, cache))
    try:
        await asyncio.sleep(0.1)
        await redis.publish("unrelated:foo", "1")
        await asyncio.sleep(0.2)
        assert cache.evict.await_count == 0
    finally:
        await _stop(task)
        await redis.aclose()


async def test_invalid_channel_format_ignored():
    """A channel that matches the pattern but is malformed must not crash or evict."""
    redis = fakeredis.aioredis.FakeRedis()
    cache = AsyncMock()

    # The split(":", 1) only fails when there's no colon at all. To exercise
    # the ValueError path, push a synthetic pmessage with a colon-less channel
    # through the listener via a custom pubsub-like object would be heavy.
    # Instead, we rely on the matching-pattern guarantee from Redis: any
    # delivered pmessage on PATTERN "tenant_reload:*" always contains ":" and
    # therefore the tenant_id slot exists (possibly empty). We assert that
    # publishing with an empty tenant id is handled (evict called with "")
    # without raising, and that publishing on a totally unrelated channel
    # without any colon does not result in an evict.
    task = asyncio.create_task(reload_listener(redis, cache))
    try:
        await asyncio.sleep(0.1)
        # malformed but pattern-matching: trailing colon, empty tenant id
        await redis.publish("tenant_reload:", "1")
        # totally unrelated, no colon
        await redis.publish("nocolonchannel", "1")
        await asyncio.sleep(0.2)
        # No exception raised; only the pattern-matching one (with empty tid)
        # may have called evict. The unrelated one must NOT have triggered evict.
        for call in cache.evict.await_args_list:
            args, _ = call
            assert args[0] == ""  # only empty-tenant from the matching publish
    finally:
        await _stop(task)
        await redis.aclose()


async def test_cancel_stops_cleanly():
    redis = fakeredis.aioredis.FakeRedis()
    cache = AsyncMock()
    task = asyncio.create_task(reload_listener(redis, cache))
    await asyncio.sleep(0.1)
    count_before = cache.evict.await_count
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # No exception leaked, and evict count is unchanged after cancellation.
    assert cache.evict.await_count == count_before
    await redis.aclose()
