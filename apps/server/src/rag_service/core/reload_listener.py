import asyncio
import logging
from typing import Any

import redis.asyncio as aioredis

log = logging.getLogger(__name__)

PATTERN = "tenant_reload:*"


async def reload_listener(redis: aioredis.Redis, cache: Any) -> None:
    """Listen for tenant_reload:{tenant_id} pubsub messages, evict cache.

    Long-running coroutine. Cancel to stop.
    """
    pubsub = redis.pubsub()
    await pubsub.psubscribe(PATTERN)
    try:
        async for msg in pubsub.listen():
            if msg["type"] != "pmessage":
                continue
            channel = msg["channel"]
            if isinstance(channel, bytes):
                channel = channel.decode()
            # channel format: "tenant_reload:{tenant_id}"
            try:
                _, tenant_id = channel.split(":", 1)
            except ValueError:
                continue
            try:
                await cache.evict(tenant_id)
            except Exception:
                log.exception("evict failed for %s", tenant_id)
    finally:
        await pubsub.punsubscribe(PATTERN)
        await pubsub.aclose()


def start_in_background(redis: aioredis.Redis, cache: Any) -> asyncio.Task:
    return asyncio.create_task(reload_listener(redis, cache), name="reload_listener")
