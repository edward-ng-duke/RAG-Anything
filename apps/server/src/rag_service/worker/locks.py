"""Per-tenant Redis ingest lock.

Provides an async context manager that acquires a Redis-based lock keyed by
tenant id, using ``SET key token NX EX ttl`` for atomic acquisition and a
small Lua script for safe (compare-then-delete) release. The token is a
fresh random value per acquisition so a stale TTL'd lock cannot be released
by a different holder.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets

import redis.asyncio as aioredis


class LockBusy(RuntimeError):
    """Raised when the per-tenant ingest lock cannot be acquired."""


# Lua script for atomic compare-then-delete: only release if we still own
# the token. Avoids the classic "lock expired, another holder has it, we
# just deleted theirs" race.
RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
else
    return 0
end
"""


@contextlib.asynccontextmanager
async def tenant_ingest_lock(
    redis: aioredis.Redis,
    tenant_id: str,
    *,
    ttl: int = 600,
    acquire_timeout: float = 0,
):
    """Async context manager that acquires a per-tenant ingest lock.

    Uses ``SET NX EX`` for atomic acquisition and a Lua compare-then-del
    on release so we only delete the key while we still own the token.

    Args:
        redis: An ``redis.asyncio.Redis`` client.
        tenant_id: Tenant identifier; used to namespace the lock key.
        ttl: Lock expiry in seconds (defensive against crashed holders).
        acquire_timeout: If 0 (default), fail fast on contention. If > 0,
            poll every 0.1s up to ``acquire_timeout`` seconds before raising
            :class:`LockBusy`.

    Raises:
        LockBusy: If the lock cannot be acquired within ``acquire_timeout``.
    """
    key = f"tenant_lock:{tenant_id}"
    token = secrets.token_hex(16)
    deadline = asyncio.get_running_loop().time() + acquire_timeout
    while True:
        ok = await redis.set(key, token, nx=True, ex=ttl)
        if ok:
            break
        if acquire_timeout <= 0 or asyncio.get_running_loop().time() >= deadline:
            raise LockBusy(f"could not acquire {key}")
        await asyncio.sleep(0.1)
    try:
        yield
    finally:
        await redis.eval(RELEASE_SCRIPT, 1, key, token)
