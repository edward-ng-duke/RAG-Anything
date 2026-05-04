"""Tests for ``rag_service.api.routers.query`` — POST /v1/query.

The DB session and the RAG cache are stubbed via ``app.dependency_overrides``
so the suite needs no real Postgres, Redis, LLM, or LightRAG. We exercise:

* basic hybrid retrieval — bare-string answer wraps as ``sources=[]``;
* dict-with-sources result — surfaces as a list of :class:`QuerySource`;
* ``vlm_enhanced=True`` — dispatches to ``aquery_vlm_enhanced`` not ``aquery``;
* upstream LLM ``HTTPStatusError(503)`` → 502 Bad Gateway;
* successful query writes a row into ``query_log``;
* ``query_log`` insert raising does NOT fail the request.
"""

from __future__ import annotations

# Required env vars must be set BEFORE importing anything from
# ``rag_service`` — the ``settings`` singleton is constructed lazily on
# first attribute access and will trip on missing required vars.
import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@h:5432/dbn")
os.environ.setdefault("REDIS_URL", "redis://x")
os.environ.setdefault("INTERNAL_TOKEN", "x")
os.environ.setdefault("LLM_BASE_URL", "http://llm")
os.environ.setdefault("LLM_API_KEY", "x")
os.environ.setdefault("LLM_MODEL", "m")
os.environ.setdefault("EMBEDDING_BASE_URL", "http://emb")
os.environ.setdefault("EMBEDDING_API_KEY", "x")
os.environ.setdefault("EMBEDDING_MODEL", "e")
os.environ.setdefault("DATA_DIR", "/tmp/rag_query_api_test")

from typing import Any  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from rag_service.api.auth import current_tenant, current_user  # noqa: E402
from rag_service.api.deps import get_db_session, get_rag_cache  # noqa: E402
from rag_service.api.routers import query as query_mod  # noqa: E402
from rag_service.db.models import QueryLog  # noqa: E402


import uuid as _uuid  # noqa: E402


class _MockUser:
    """Minimal stand-in for the ``User`` row that ``current_user`` returns."""

    def __init__(self, user_id: _uuid.UUID | None = None) -> None:
        self.user_id = user_id or _uuid.uuid4()
        self.is_active = True


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeSession:
    """Capture everything ``add()``-ed so tests can assert the QueryLog row.

    ``commit_should_fail=True`` makes ``commit()`` raise — this is how we
    simulate a query_log INSERT failure and verify the request still
    succeeds.
    """

    def __init__(self, *, commit_should_fail: bool = False) -> None:
        self.added: list[Any] = []
        self.commits = 0
        self.rollbacks = 0
        self.flushes = 0
        self._commit_should_fail = commit_should_fail

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushes += 1

    async def commit(self) -> None:
        if self._commit_should_fail:
            raise RuntimeError("simulated query_log commit failure")
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class _FakeRagCache:
    """Stand-in for :class:`RAGAnythingCache`.

    ``get(tenant_id)`` returns the pre-loaded ``rag`` mock — we don't
    bother modelling the per-tenant LRU here; the router only relies on
    ``await cache.get(tenant_id)`` returning *something* with
    ``aquery`` / ``aquery_vlm_enhanced`` coroutines.
    """

    def __init__(self, rag: Any) -> None:
        self.rag = rag
        self.tenants_requested: list[str] = []

    async def get(self, tenant_id: str) -> Any:
        self.tenants_requested.append(tenant_id)
        return self.rag


# ---------------------------------------------------------------------------
# App + test-client wiring
# ---------------------------------------------------------------------------


def _make_app(
    *,
    rag: Any,
    session: _FakeSession | None = None,
    tenant: str = "tnt-1",
) -> tuple[FastAPI, _FakeRagCache, _FakeSession]:
    """Build a FastAPI app with the query router and dependency overrides.

    Auth is overridden directly: ``current_user`` returns a mock user and
    ``current_tenant`` returns ``tenant``. Tests no longer thread bearer
    headers through the request — they pass ``tenant=`` instead.
    """
    app = FastAPI()
    app.include_router(query_mod.router)

    cache = _FakeRagCache(rag)
    sess = session if session is not None else _FakeSession()

    async def _db_override():
        try:
            yield sess
        except Exception:
            await sess.rollback()
            raise

    async def _cache_override():
        return cache

    async def _user_override() -> _MockUser:
        return _MockUser()

    async def _tenant_override() -> str:
        return tenant

    app.dependency_overrides[get_db_session] = _db_override
    app.dependency_overrides[get_rag_cache] = _cache_override
    app.dependency_overrides[current_user] = _user_override
    app.dependency_overrides[current_tenant] = _tenant_override
    return app, cache, sess


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_query_basic_hybrid():
    """Bare-string answer wraps as ``sources=[]`` and returns 200."""
    rag = AsyncMock()
    rag.aquery = AsyncMock(return_value="The capital of France is Paris.")
    rag.aquery_vlm_enhanced = AsyncMock(
        side_effect=AssertionError("must not be called")
    )

    app, _cache, _sess = _make_app(rag=rag)

    client = TestClient(app)
    r = client.post(
        "/v1/query",
        json={"question": "What is the capital of France?"},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["answer"] == "The capital of France is Paris."
    assert body["sources"] == []
    assert isinstance(body["latency_ms"], int)
    assert body["latency_ms"] >= 0
    assert body["tokens"] is None
    # Hybrid is the default mode; top_k defaults to 10.
    rag.aquery.assert_awaited_once_with(
        "What is the capital of France?", mode="hybrid", top_k=10
    )


def test_query_with_sources():
    """Dict-shaped RA result is mapped into a list of ``QuerySource``."""
    rag = AsyncMock()
    rag.aquery = AsyncMock(
        return_value={
            "answer": "Paris is the capital of France.",
            "sources": [
                {
                    "document_id": "11111111-1111-1111-1111-111111111111",
                    "file_name": "geo.pdf",
                    "chunk_id": "chunk-1",
                    "score": 0.92,
                    "snippet": "Paris is the capital of France.",
                    "modality": "text",
                },
                {
                    "chunk_id": "chunk-2",
                    "score": 0.81,
                    "text": "An image of the Eiffel Tower.",
                    "modality": "image",
                },
            ],
            "tokens": {"in": 120, "out": 30, "cost_usd": 0.0006},
        }
    )

    app, _cache, sess = _make_app(rag=rag)

    client = TestClient(app)
    r = client.post(
        "/v1/query",
        json={"question": "Where is Paris?", "top_k": 5},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["answer"] == "Paris is the capital of France."
    assert len(body["sources"]) == 2
    s1 = body["sources"][0]
    assert s1["document_id"] == "11111111-1111-1111-1111-111111111111"
    assert s1["file_name"] == "geo.pdf"
    assert s1["chunk_id"] == "chunk-1"
    assert s1["score"] == pytest.approx(0.92)
    assert s1["snippet"] == "Paris is the capital of France."
    assert s1["modality"] == "text"
    s2 = body["sources"][1]
    # ``text`` field surfaces under snippet via the fallback mapping.
    assert s2["snippet"] == "An image of the Eiffel Tower."
    assert s2["modality"] == "image"
    assert s2["document_id"] is None
    assert body["tokens"] == {"in": 120, "out": 30, "cost_usd": 0.0006}
    # top_k forwarded.
    rag.aquery.assert_awaited_once_with(
        "Where is Paris?", mode="hybrid", top_k=5
    )
    # Token counts propagated into the QueryLog row.
    assert len(sess.added) == 1
    log_row = sess.added[0]
    assert isinstance(log_row, QueryLog)
    assert log_row.token_in == 120
    assert log_row.token_out == 30


def test_query_vlm_enhanced():
    """``vlm_enhanced=True`` dispatches to ``aquery_vlm_enhanced``."""
    rag = AsyncMock()
    rag.aquery = AsyncMock(side_effect=AssertionError("must not be called"))
    rag.aquery_vlm_enhanced = AsyncMock(
        return_value={"answer": "VLM says: a chart.", "sources": []}
    )

    app, _cache, _sess = _make_app(rag=rag)

    client = TestClient(app)
    r = client.post(
        "/v1/query",
        json={
            "question": "Describe the chart on page 2.",
            "vlm_enhanced": True,
            "mode": "mix",
            "top_k": 7,
        },
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["answer"] == "VLM says: a chart."
    rag.aquery_vlm_enhanced.assert_awaited_once_with(
        "Describe the chart on page 2.", mode="mix", top_k=7
    )
    rag.aquery.assert_not_awaited()


def test_query_llm_5xx_returns_502():
    """``httpx.HTTPStatusError(503)`` from RA surfaces as 502."""
    rag = AsyncMock()
    request = httpx.Request("POST", "http://llm/v1/chat/completions")
    response = httpx.Response(status_code=503, request=request)
    rag.aquery = AsyncMock(
        side_effect=httpx.HTTPStatusError(
            "service unavailable", request=request, response=response
        )
    )

    app, _cache, _sess = _make_app(rag=rag)

    client = TestClient(app, raise_server_exceptions=False)
    r = client.post(
        "/v1/query",
        json={"question": "anything"},
    )

    assert r.status_code == 502, r.text
    assert "503" in r.json()["detail"]


def test_query_logs_to_query_log():
    """A successful query inserts one row into ``query_log`` and commits."""
    rag = AsyncMock()
    rag.aquery = AsyncMock(
        return_value={
            "answer": "ok",
            "sources": [],
            "tokens": {"in": 5, "out": 10, "cost_usd": 0.0001},
        }
    )

    app, _cache, sess = _make_app(rag=rag, tenant="tnt-42")

    client = TestClient(app)
    r = client.post(
        "/v1/query",
        json={"question": "ping?", "mode": "local"},
    )

    assert r.status_code == 200, r.text
    # Exactly one QueryLog row added; one commit on the analytics path.
    log_rows = [r for r in sess.added if isinstance(r, QueryLog)]
    assert len(log_rows) == 1
    log = log_rows[0]
    assert log.tenant_id == "tnt-42"
    assert log.question == "ping?"
    assert log.mode == "local"
    assert isinstance(log.latency_ms, int)
    assert log.token_in == 5
    assert log.token_out == 10
    assert sess.commits == 1


def test_query_log_failure_doesnt_fail_request():
    """A raise from the query_log INSERT path leaves the response 200."""
    rag = AsyncMock()
    rag.aquery = AsyncMock(return_value="hello")

    sess = _FakeSession(commit_should_fail=True)
    app, _cache, _ = _make_app(rag=rag, session=sess)

    client = TestClient(app)
    r = client.post(
        "/v1/query",
        json={"question": "anything"},
    )

    assert r.status_code == 200, r.text
    assert r.json()["answer"] == "hello"
    # The router caught the commit failure and rolled back.
    assert sess.rollbacks >= 1
