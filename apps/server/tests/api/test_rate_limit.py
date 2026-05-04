"""Tests for ``rag_service.api.rate_limit.RateLimitMiddleware``.

A throwaway FastAPI app with one ``GET /ping`` endpoint and one
``GET /v1/auth/login`` endpoint is mounted with the middleware so each
test exercises the dispatch path directly. The Redis backend is
:mod:`fakeredis` so no real Redis is required and tests run in-process.
"""

from __future__ import annotations

# Required env vars must be set before any ``rag_service`` import — the
# settings singleton is constructed lazily on first attribute access and
# trips on missing required vars.
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
os.environ.setdefault("DATA_DIR", "/tmp/rag_rate_limit_test")
os.environ.setdefault(
    "JWT_SECRET_KEY",
    "x" * 64,  # min length 64; value irrelevant for rate-limit tests.
)

import fakeredis.aioredis  # noqa: E402
import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from rag_service.api.rate_limit import (  # noqa: E402
    DEFAULTS,
    RateLimitMiddleware,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_app(redis) -> FastAPI:
    """A tiny app: ``/ping`` (default class) + ``/v1/auth/login`` (auth class)."""
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, redis=redis)

    @app.get("/ping")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/v1/auth/login")
    async def login() -> dict[str, bool]:
        return {"ok": True}

    return app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_default_limit_allows_burst_then_blocks() -> None:
    """First ``limit`` requests succeed; the next gets a 429 with Retry-After."""
    redis = fakeredis.aioredis.FakeRedis()
    app = _build_app(redis)
    limit, _ = DEFAULTS["default"]

    # ``with TestClient(app)`` keeps one anyio portal alive across requests
    # so the fakeredis Queue (bound to the first request's loop) is reused
    # rather than re-bound each call.
    with TestClient(app) as client:
        for i in range(limit):
            r = client.get("/ping")
            assert r.status_code == 200, f"request {i} unexpectedly throttled: {r.text}"

        r = client.get("/ping")
        assert r.status_code == 429
        assert "Retry-After" in r.headers
        retry = int(r.headers["Retry-After"])
        assert 1 <= retry <= 60


def test_auth_route_class_lower_limit() -> None:
    """``/v1/auth/*`` paths use the tighter ``auth`` bucket (5/min by default)."""
    redis = fakeredis.aioredis.FakeRedis()
    app = _build_app(redis)
    limit, _ = DEFAULTS["auth"]

    with TestClient(app) as client:
        for i in range(limit):
            r = client.post("/v1/auth/login")
            assert r.status_code == 200, f"auth request {i} unexpectedly throttled"

        r = client.post("/v1/auth/login")
        assert r.status_code == 429


def test_redis_down_fails_open() -> None:
    """If Redis raises on every command, the middleware lets the request through."""

    class _BoomRedis:
        async def incr(self, key):  # noqa: D401, ARG002
            raise ConnectionError("redis is down")

        async def expire(self, key, ttl):  # noqa: D401, ARG002
            raise ConnectionError("redis is down")

    app = _build_app(_BoomRedis())
    with TestClient(app) as client:
        # Any number of requests — none of them should be throttled because
        # the dispatch path swallows the redis error.
        for _ in range(DEFAULTS["default"][0] + 5):
            r = client.get("/ping")
            assert r.status_code == 200, r.text


def test_429_has_retry_after_header() -> None:
    """The 429 response must carry a ``Retry-After`` header for client backoff."""
    redis = fakeredis.aioredis.FakeRedis()
    app = _build_app(redis)
    limit, _ = DEFAULTS["default"]

    with TestClient(app) as client:
        for _ in range(limit):
            client.get("/ping")
        r = client.get("/ping")
        assert r.status_code == 429
        assert "Retry-After" in r.headers
        assert int(r.headers["Retry-After"]) >= 1


def test_separate_identities_have_separate_buckets() -> None:
    """Two distinct ``X-Forwarded-For`` clients shouldn't share a bucket."""
    redis = fakeredis.aioredis.FakeRedis()
    app = _build_app(redis)
    limit, _ = DEFAULTS["default"]

    with TestClient(app) as client:
        # Burn client A's bucket.
        for _ in range(limit):
            r = client.get("/ping", headers={"X-Forwarded-For": "10.0.0.1"})
            assert r.status_code == 200
        r = client.get("/ping", headers={"X-Forwarded-For": "10.0.0.1"})
        assert r.status_code == 429

        # Client B is independent and has a fresh bucket.
        r = client.get("/ping", headers={"X-Forwarded-For": "10.0.0.2"})
        assert r.status_code == 200


def test_redis_keys_have_ttl() -> None:
    """First INCR sets EXPIRE — verified by reading the TTL on the bucket key."""
    redis = fakeredis.aioredis.FakeRedis()
    app = _build_app(redis)

    # Drive one request through the middleware, then probe the redis state
    # via the same TestClient portal so the fakeredis queue stays bound to
    # one event loop. We piggy-back a debug endpoint onto the same app.

    @app.get("/_probe_ttl")
    async def _probe_ttl():
        keys = [k async for k in redis.scan_iter(match="rl:default:ip:10.0.0.99:*")]
        if not keys:
            return {"found": False}
        ttl = await redis.ttl(keys[0])
        return {"found": True, "ttl": ttl}

    with TestClient(app) as client:
        client.get("/ping", headers={"X-Forwarded-For": "10.0.0.99"})
        r = client.get("/_probe_ttl")
        body = r.json()

    assert body["found"] is True
    assert 0 < body["ttl"] <= DEFAULTS["default"][1]
