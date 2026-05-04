"""Redis-backed sliding-window rate limiting middleware.

The middleware runs on every HTTP request, classifies it into a route
class (``"auth"`` for ``/v1/auth/*`` paths, the ``"onyx_*"`` family for
``/v1/onyx/*`` paths, and ``"default"`` for everything else), increments
a per-(class, identity, time-bucket) counter in Redis, and rejects with
``429 Too Many Requests`` once the bucket overflows.

Buckets are aligned to wall-clock windows (``floor(now / window)``) which
gives an approximate sliding window: a client hitting the limit at the
end of one window and the start of the next can briefly burst to ``2 *
limit`` per minute. That's an acceptable trade-off for the implementation
simplicity and is the same shape that nginx ``limit_req_zone`` and the
classic Redis INCR pattern produce.

Identity preference (most → least specific):
  * For ``/v1/onyx/*`` paths: the ``X-Onyx-User-Id`` header (the onyx
    user uuid forwarded by the onyx backend), namespaced under
    ``s:onyx:`` so onyx user buckets cannot collide with the α user_id
    buckets. Missing header → ``s:onyx:_anon``.
  * Otherwise:
    1. ``Bearer`` token decoded as a JWT — bucket per ``user_id``;
    2. ``X-Forwarded-For`` first hop — bucket per upstream client IP;
    3. ``request.client.host`` — bucket per direct peer.

Onyx routes additionally trip a per-INTERNAL_TOKEN aggregate bucket
(``onyx_token_total``) so a single shared service-token cannot be used
to fan out across many onyx users and exceed the global ceiling. The
token-aggregate counter is checked *after* the per-user bucket passes,
so the per-user 429 (which is the actionable error for end users) wins
when both would trip on the same request.

Failure mode: if Redis is unreachable, the middleware *fails open* — the
underlying error is swallowed and the request is allowed through. We'd
rather degrade rate limiting than take the API down because of a Redis
outage; metrics + the Redis healthcheck make the issue visible elsewhere.
"""

from __future__ import annotations

import hashlib
import time
from typing import Awaitable, Callable

import redis.asyncio as aioredis
from fastapi import Request, Response, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


# Per-route-class limits as ``(max_requests, window_seconds)``. Auth gets
# the tightest bucket because brute-forcing logins is the most attractive
# target. The onyx family carries per-route caps tuned to the expected
# call frequency from the onyx backend (queries are user-driven, doc
# writes are coarse-grained, KG reads are chatty), plus a global
# per-token aggregate (``onyx_token_total``) so a single shared service
# token can't fan-out around the per-user buckets.
DEFAULTS: dict[str, tuple[int, int]] = {
    "auth": (5, 60),                  # /v1/auth/* — 5 req/min
    "onyx_query": (30, 60),           # /v1/onyx/query, /v1/onyx/query/sync
    "onyx_docs_post": (10, 60),       # POST /v1/onyx/documents
    "onyx_kg": (120, 60),             # /v1/onyx/kg/*
    "onyx_other": (60, 60),           # other /v1/onyx/*
    "onyx_token_total": (1000, 60),   # global per-INTERNAL_TOKEN aggregate
    "default": (60, 60),              # everything else — 60 req/min
}


def _apply_overrides() -> dict[str, tuple[int, int]]:
    """Return a per-call patched copy of :data:`DEFAULTS`.

    Reads ``settings.onyx_ratelimit_overrides`` (whitelisted in the
    config validator to keys ``{query, docs_post, kg, other,
    token_total}``) and overrides the corresponding ``onyx_<key>``
    tuple's first element (the ``max_requests`` field). The window
    stays at 60 seconds — operators can scale capacity but not change
    the period without a code change.

    Called *lazily* from inside :meth:`RateLimitMiddleware.dispatch` so
    monkeypatched settings (e.g. test fixtures that swap a value on the
    cached ``settings`` instance) take effect without rebuilding the
    middleware. Pydantic-settings caches the singleton per-process, and
    the override dict is small (≤ 5 entries), so per-call dict
    construction is cheap.
    """
    patched = dict(DEFAULTS)
    try:
        from rag_service.config import settings

        overrides = getattr(settings, "onyx_ratelimit_overrides", {}) or {}
    except Exception:
        return patched
    for key, value in overrides.items():
        klass = f"onyx_{key}"
        if klass not in patched:
            # Defensive: the config validator already rejects unknown
            # keys at startup, so reaching here means an operator
            # bypassed the validator. Skip silently rather than trip an
            # error on the request hot path.
            continue
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            continue
        _, window = patched[klass]
        patched[klass] = (value, window)
    return patched


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
        # Patched table built once per request — cheap and lets test
        # monkeypatches against settings flow through without restart.
        patched = _apply_overrides()
        limit, window = patched.get(klass, patched["default"])

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

        # Per-INTERNAL_TOKEN aggregate. Only checked for onyx routes
        # *after* the per-onyx-user bucket has passed — the per-user
        # 429 is the actionable response for end users, so it wins when
        # both would trip on the same request. The token bucket exists
        # to bound total throughput from a compromised or misbehaving
        # onyx backend instance, not to gate individual users.
        if klass.startswith("onyx_"):
            auth = request.headers.get("authorization", "")
            token = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
            if token:
                # Hash with sha256 + truncate so the bucket key doesn't
                # leak the raw INTERNAL_TOKEN into Redis (or its slow
                # logs / persistence files). 16 hex chars = 64 bits of
                # collision resistance, which is comfortably more than
                # the number of distinct tokens we expect to see.
                token_hash = hashlib.sha256(token.encode()).hexdigest()[:16]
                token_limit, token_window = patched["onyx_token_total"]
                token_bucket = int(time.time()) // token_window
                token_key = f"rl:onyx_token_total:{token_hash}:{token_bucket}"
                try:
                    t_count = await self._redis.incr(token_key)
                    if t_count == 1:
                        await self._redis.expire(token_key, token_window)
                except Exception:
                    # Redis blip on the second INCR — fall through and
                    # fail open per the existing convention.
                    return await call_next(request)
                if t_count > token_limit:
                    retry_after = max(
                        1, token_window - (int(time.time()) % token_window)
                    )
                    return JSONResponse(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        content={
                            "detail": (
                                f"rate limited: token total "
                                f"{token_limit}/{token_window}s"
                            )
                        },
                        headers={"Retry-After": str(retry_after)},
                    )

        return await call_next(request)


def _default_classifier(request: Request) -> str:
    """Bucket auth flows tighter than everything else, with onyx-aware classes."""
    path = request.url.path
    if path.startswith("/v1/auth"):
        return "auth"
    if path.startswith("/v1/onyx/"):
        if path == "/v1/onyx/query" or path == "/v1/onyx/query/sync":
            return "onyx_query"
        if request.method == "POST" and path == "/v1/onyx/documents":
            return "onyx_docs_post"
        if path.startswith("/v1/onyx/kg/"):
            return "onyx_kg"
        return "onyx_other"
    return "default"


def _identity(request: Request) -> str:
    """Resolve a stable identity string for the rate-limit bucket key.

    For ``/v1/onyx/*`` paths, identity is sourced from the
    ``X-Onyx-User-Id`` header (the onyx user uuid forwarded by the onyx
    backend) and bucketed under the ``s:onyx:`` namespace so onyx user
    buckets don't collide with the α user_id buckets. Missing header →
    ``s:onyx:_anon`` (one shared bucket so anonymous traffic can't
    silently fan-out across IPs).

    For other paths, prefers a JWT-derived ``user_id`` (so the bucket
    follows the user across IP changes), then ``X-Forwarded-For``'s
    first hop (the typical proxy-deployed path), then the direct peer's
    IP. We don't *validate* the JWT here — an attacker forging a sub
    claim only manages to share a bucket with whoever owns that
    user_id, which is at worst neutral. Validation happens in
    :mod:`rag_service.api.auth`.
    """
    path = request.url.path
    if path.startswith("/v1/onyx/"):
        oxuid = request.headers.get("x-onyx-user-id")
        if oxuid:
            return f"s:onyx:{oxuid}"
        return "s:onyx:_anon"
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
