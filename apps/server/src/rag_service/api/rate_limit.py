"""Redis-backed sliding-window rate limiting middleware.

The middleware runs on every HTTP request, classifies it into a route
class (``"auth"`` for ``/v1/auth/*`` paths, ``"default"`` for everything
else), increments a per-(class, identity, time-bucket) counter in Redis,
and rejects with ``429 Too Many Requests`` once the bucket overflows.

Buckets are aligned to wall-clock windows (``floor(now / window)``) which
gives an approximate sliding window: a client hitting the limit at the
end of one window and the start of the next can briefly burst to ``2 *
limit`` per minute. That's an acceptable trade-off for the implementation
simplicity and is the same shape that nginx ``limit_req_zone`` and the
classic Redis INCR pattern produce.

Identity preference (most → least specific):
  1. ``Bearer`` token decoded as a JWT — bucket per ``user_id``;
  2. ``X-Forwarded-For`` first hop — bucket per upstream client IP;
  3. ``request.client.host`` — bucket per direct peer.

Failure mode: if Redis is unreachable, the middleware *fails open* — the
underlying error is swallowed and the request is allowed through. We'd
rather degrade rate limiting than take the API down because of a Redis
outage; metrics + the Redis healthcheck make the issue visible elsewhere.
"""

from __future__ import annotations

import time
from typing import Awaitable, Callable

import redis.asyncio as aioredis
from fastapi import Request, Response, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


# Per-route-class limits as ``(max_requests, window_seconds)``. Auth gets
# the tightest bucket because brute-forcing logins is the most attractive
# target — every other endpoint is a regular API call that legitimate
# clients can hit a few per second under normal use.
DEFAULTS: dict[str, tuple[int, int]] = {
    "auth": (5, 60),       # /v1/auth/* — 5 req/min
    "default": (60, 60),   # everything else — 60 req/min
}


class RateLimitMiddleware(BaseHTTPMiddleware):
    """ASGI middleware that throttles by ``(route-class, identity)``.

    Parameters
    ----------
    app:
        The ASGI app being wrapped (Starlette injects this).
    redis:
        An ``aioredis.Redis`` client. ``aioredis.from_url`` returns a
        connection-pooled client without performing any I/O until the
        first command, so installing the middleware at create-app time
        is safe even before Redis is reachable.
    classifier:
        Optional callable mapping a request to a key in :data:`DEFAULTS`.
        Defaults to :func:`_default_classifier`.
    """

    def __init__(
        self,
        app,
        redis: aioredis.Redis,
        classifier: Callable[[Request], str] | None = None,
    ) -> None:
        super().__init__(app)
        self._redis = redis
        self._classifier = classifier or _default_classifier

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        identity = _identity(request)
        klass = self._classifier(request)
        limit, window = DEFAULTS.get(klass, DEFAULTS["default"])

        bucket = int(time.time()) // window
        key = f"rl:{klass}:{identity}:{bucket}"
        try:
            count = await self._redis.incr(key)
            # Set TTL on first hit so we don't accumulate dead keys. We
            # could PEXPIRE every increment, but that's wasted work — the
            # window's expiry doesn't change after the first hit.
            if count == 1:
                await self._redis.expire(key, window)
        except Exception:
            # Redis is down / network blip / auth error — fail open. The
            # request is *allowed* through; observability surfaces the
            # underlying outage via the redis healthcheck.
            return await call_next(request)

        if count > limit:
            # Time until the *next* bucket starts. Always at least 1s so
            # clients that retry-now don't get caught in a tight loop on
            # a saturated bucket boundary. We return a Response directly
            # rather than raising HTTPException because BaseHTTPMiddleware
            # propagates raised exceptions out to the ASGI runner, which
            # then 500s instead of producing the 429 we want.
            retry_after = max(1, window - (int(time.time()) % window))
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"detail": f"rate limited: max {limit}/{window}s"},
                headers={"Retry-After": str(retry_after)},
            )

        return await call_next(request)


def _default_classifier(request: Request) -> str:
    """Bucket auth flows tighter than everything else."""
    path = request.url.path
    if path.startswith("/v1/auth"):
        return "auth"
    return "default"


def _identity(request: Request) -> str:
    """Resolve a stable identity string for the rate-limit bucket key.

    Prefers a JWT-derived ``user_id`` (so the bucket follows the user
    across IP changes), then ``X-Forwarded-For``'s first hop (the typical
    proxy-deployed path), then the direct peer's IP. We don't *validate*
    the JWT here — an attacker forging a sub claim only manages to share
    a bucket with whoever owns that user_id, which is at worst neutral.
    Validation happens in :mod:`rag_service.api.auth`.
    """
    auth = request.headers.get("authorization") or ""
    if auth.startswith("Bearer "):
        try:
            from rag_service.auth.jwt import decode_token

            claims = decode_token(auth[len("Bearer "):])
            sub = claims.get("sub")
            if sub:
                return f"u:{sub}"
        except Exception:
            # Fall through to IP-based identity below.
            pass
    xff = request.headers.get("x-forwarded-for")
    if xff:
        # XFF is a comma-separated chain — the *first* hop is the
        # original client; subsequent entries are intermediate proxies.
        return f"ip:{xff.split(',')[0].strip()}"
    if request.client is not None:
        return f"ip:{request.client.host}"
    return "ip:unknown"
