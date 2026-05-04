"""Tests for the onyx-aware rate-limit classification + identity logic.

These tests exercise the per-onyx-user buckets (sourced from
``X-Onyx-User-Id``) and the per-INTERNAL_TOKEN aggregate bucket added in
Task 3.1. They share the same fakeredis-backed approach as
``test_rate_limit.py`` so no real Redis is required and tests run
in-process.
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
os.environ.setdefault("DATA_DIR", "/tmp/rag_rate_limit_onyx_test")
os.environ.setdefault("JWT_SECRET_KEY", "x" * 64)

import fakeredis.aioredis  # noqa: E402
import pytest  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from rag_service.api.rate_limit import (  # noqa: E402
    DEFAULTS,
    RateLimitMiddleware,
    _default_classifier,
    _identity,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_app(redis) -> FastAPI:
    """Tiny FastAPI app with stub endpoints under ``/v1/onyx/*``."""
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, redis=redis)

    @app.post("/v1/onyx/query")
    async def onyx_query() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/v1/onyx/query/sync")
    async def onyx_query_sync() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/v1/onyx/documents")
    async def onyx_documents_post() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/v1/onyx/documents")
    async def onyx_documents_get() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/v1/onyx/kg/entities")
    async def onyx_kg_entities() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/v1/onyx/kb")
    async def onyx_kb() -> dict[str, bool]:
        return {"ok": True}

    return app


def _fake_request(path: str, method: str = "GET", headers: dict[str, str] | None = None) -> Request:
    """Build a minimal Starlette ``Request`` for classifier/identity tests.

    Only the fields the code-under-test reads are populated — enough for
    the helpers to make their decisions without spinning up a TestClient.
    """
    raw_headers = []
    for k, v in (headers or {}).items():
        raw_headers.append((k.lower().encode("latin-1"), v.encode("latin-1")))
    scope = {
        "type": "http",
        "method": method.upper(),
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": raw_headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "scheme": "http",
        "root_path": "",
    }
    return Request(scope)


# ---------------------------------------------------------------------------
# Classifier tests
# ---------------------------------------------------------------------------


def test_classifier_query_path() -> None:
    """Both async and sync onyx-query routes use the ``onyx_query`` bucket."""
    assert _default_classifier(_fake_request("/v1/onyx/query", "POST")) == "onyx_query"
    assert (
        _default_classifier(_fake_request("/v1/onyx/query/sync", "POST"))
        == "onyx_query"
    )


def test_classifier_docs_post_path() -> None:
    """POST /v1/onyx/documents is the doc-write bucket; GET falls back to ``other``."""
    assert (
        _default_classifier(_fake_request("/v1/onyx/documents", "POST"))
        == "onyx_docs_post"
    )
    assert (
        _default_classifier(_fake_request("/v1/onyx/documents", "GET"))
        == "onyx_other"
    )


def test_classifier_kg_path() -> None:
    """``/v1/onyx/kg/*`` paths all bucket as ``onyx_kg``."""
    assert _default_classifier(_fake_request("/v1/onyx/kg/entities")) == "onyx_kg"


def test_classifier_other_path() -> None:
    """Unrecognised onyx subpaths fall back to ``onyx_other``."""
    assert _default_classifier(_fake_request("/v1/onyx/kb")) == "onyx_other"


def test_classifier_non_onyx_path() -> None:
    """Non-onyx paths still hit the original ``auth`` / ``default`` classes."""
    assert _default_classifier(_fake_request("/v1/auth/login", "POST")) == "auth"
    assert _default_classifier(_fake_request("/ping")) == "default"


# ---------------------------------------------------------------------------
# Identity tests
# ---------------------------------------------------------------------------


def test_identity_uses_onyx_user_header() -> None:
    """Onyx requests bucket per ``X-Onyx-User-Id``, namespaced as ``s:onyx:``."""
    req = _fake_request(
        "/v1/onyx/query",
        "POST",
        headers={"x-onyx-user-id": "u_alice"},
    )
    assert _identity(req) == "s:onyx:u_alice"


def test_identity_falls_back_to_anon_when_missing_header() -> None:
    """No ``X-Onyx-User-Id`` → all such traffic shares the ``s:onyx:_anon`` bucket."""
    req = _fake_request("/v1/onyx/query", "POST")
    assert _identity(req) == "s:onyx:_anon"


def test_identity_non_onyx_unchanged() -> None:
    """Existing JWT/XFF/peer identity logic must still apply outside ``/v1/onyx/*``."""
    req = _fake_request(
        "/ping",
        "GET",
        headers={"x-forwarded-for": "10.0.0.7"},
    )
    assert _identity(req) == "ip:10.0.0.7"


# ---------------------------------------------------------------------------
# Limit-enforcement tests
# ---------------------------------------------------------------------------


def test_query_limit_30_per_minute() -> None:
    """``/v1/onyx/query`` throttles at 30 hits/min per onyx user."""
    redis = fakeredis.aioredis.FakeRedis()
    app = _build_app(redis)
    limit, _ = DEFAULTS["onyx_query"]
    assert limit == 30

    headers = {"X-Onyx-User-Id": "u_alice"}
    with TestClient(app) as client:
        for i in range(limit):
            r = client.post("/v1/onyx/query", headers=headers)
            assert r.status_code == 200, f"req {i} unexpectedly throttled: {r.text}"
        r = client.post("/v1/onyx/query", headers=headers)
        assert r.status_code == 429
        assert "Retry-After" in r.headers


def test_docs_post_limit_10_per_minute() -> None:
    """``POST /v1/onyx/documents`` throttles at 10 hits/min per onyx user."""
    redis = fakeredis.aioredis.FakeRedis()
    app = _build_app(redis)
    limit, _ = DEFAULTS["onyx_docs_post"]
    assert limit == 10

    headers = {"X-Onyx-User-Id": "u_bob"}
    with TestClient(app) as client:
        for i in range(limit):
            r = client.post("/v1/onyx/documents", headers=headers)
            assert r.status_code == 200, f"req {i} unexpectedly throttled: {r.text}"
        r = client.post("/v1/onyx/documents", headers=headers)
        assert r.status_code == 429


def test_kg_limit_120_per_minute() -> None:
    """``/v1/onyx/kg/*`` throttles at 120 hits/min per onyx user."""
    redis = fakeredis.aioredis.FakeRedis()
    app = _build_app(redis)
    limit, _ = DEFAULTS["onyx_kg"]
    assert limit == 120

    headers = {"X-Onyx-User-Id": "u_eve"}
    with TestClient(app) as client:
        for i in range(limit):
            r = client.get("/v1/onyx/kg/entities", headers=headers)
            assert r.status_code == 200, f"req {i} unexpectedly throttled: {r.text}"
        r = client.get("/v1/onyx/kg/entities", headers=headers)
        assert r.status_code == 429


def test_other_limit_60_per_minute() -> None:
    """Unbucketed onyx routes throttle at 60 hits/min per onyx user."""
    redis = fakeredis.aioredis.FakeRedis()
    app = _build_app(redis)
    limit, _ = DEFAULTS["onyx_other"]
    assert limit == 60

    headers = {"X-Onyx-User-Id": "u_carol"}
    with TestClient(app) as client:
        for i in range(limit):
            r = client.get("/v1/onyx/kb", headers=headers)
            assert r.status_code == 200, f"req {i} unexpectedly throttled: {r.text}"
        r = client.get("/v1/onyx/kb", headers=headers)
        assert r.status_code == 429


def test_token_total_aggregate_1000_per_minute() -> None:
    """One INTERNAL_TOKEN spread across many users still hits the global cap."""
    redis = fakeredis.aioredis.FakeRedis()
    app = _build_app(redis)
    token_limit, _ = DEFAULTS["onyx_token_total"]
    assert token_limit == 1000
    # Per-onyx-kg per-user limit (120) so we need 9 distinct users to
    # avoid tripping the per-user bucket before the token aggregate.
    per_user_limit, _ = DEFAULTS["onyx_kg"]

    bearer = "tok_" + "a" * 60
    auth_headers = {"Authorization": f"Bearer {bearer}"}

    total_hits = token_limit + 1  # 1001st should 429 on token aggregate
    user_count = (total_hits // per_user_limit) + 1

    with TestClient(app) as client:
        seen_429 = False
        for i in range(total_hits):
            user_idx = i // per_user_limit
            assert user_idx < user_count
            headers = {**auth_headers, "X-Onyx-User-Id": f"u_{user_idx}"}
            r = client.get("/v1/onyx/kg/entities", headers=headers)
            if i < token_limit:
                assert r.status_code == 200, (
                    f"req {i} unexpectedly throttled before token aggregate "
                    f"limit: {r.text}"
                )
            else:
                assert r.status_code == 429
                assert "token total" in r.json()["detail"]
                seen_429 = True
                break
        assert seen_429, "expected 429 from token-total aggregate"


def test_per_user_overrides_via_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """``settings.onyx_ratelimit_overrides`` patches the per-class cap."""
    from rag_service import config as rag_config

    # Override only the ``query`` class to a tight 5/min ceiling; everything
    # else falls through to defaults.
    monkeypatch.setattr(
        rag_config.settings,
        "onyx_ratelimit_overrides",
        {"query": 5},
        raising=False,
    )

    redis = fakeredis.aioredis.FakeRedis()
    app = _build_app(redis)
    headers = {"X-Onyx-User-Id": "u_override"}

    with TestClient(app) as client:
        for i in range(5):
            r = client.post("/v1/onyx/query", headers=headers)
            assert r.status_code == 200, f"req {i} unexpectedly throttled: {r.text}"
        r = client.post("/v1/onyx/query", headers=headers)
        assert r.status_code == 429


def test_redis_failure_fails_open() -> None:
    """Redis raising on ``incr`` lets onyx requests through (existing convention)."""

    class _BoomRedis:
        async def incr(self, key):  # noqa: D401, ARG002
            raise ConnectionError("redis is down")

        async def expire(self, key, ttl):  # noqa: D401, ARG002
            raise ConnectionError("redis is down")

    app = _build_app(_BoomRedis())
    headers = {"X-Onyx-User-Id": "u_alice"}
    with TestClient(app) as client:
        for _ in range(50):
            r = client.post("/v1/onyx/query", headers=headers)
            assert r.status_code == 200, r.text
