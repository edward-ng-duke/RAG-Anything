"""Tests for ``rag_service.api.routers.onyx_query`` — /v1/onyx/query (SSE + sync).

ONYX integration is stateless: the chat history is sent in the request
body and never persisted by RAG. Two endpoints share one underlying
service:

* ``POST /v1/onyx/query``        — SSE stream (``meta`` → ``chunk*`` → ``done|error``).
* ``POST /v1/onyx/query/sync``   — plain JSON (debug / SSE-unfriendly callers).

The RAGAnything dependency is replaced via ``app.dependency_overrides``
so tests need no real LightRAG / Postgres / Redis. The DB session is
stubbed too (the analytics write is best-effort; tests don't care).

Each SSE test reads the full body, splits on ``\\n\\n``, and parses each
frame's ``event:`` / ``data:`` lines. We assert the wire ordering and
payload shape — heartbeat behaviour is intentionally not exercised here
because it's clock-sensitive; the slice-mode generator yields enough
control to the event loop to keep the test deterministic.
"""

from __future__ import annotations

# Required env vars must be set BEFORE importing rag_service.
import os  # noqa: E402

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@h:5432/dbn")
os.environ.setdefault("REDIS_URL", "redis://x")
os.environ.setdefault("INTERNAL_TOKEN", "x" * 64)
os.environ.setdefault("LLM_BASE_URL", "http://llm")
os.environ.setdefault("LLM_API_KEY", "x")
os.environ.setdefault("LLM_MODEL", "m")
os.environ.setdefault("EMBEDDING_BASE_URL", "http://emb")
os.environ.setdefault("EMBEDDING_API_KEY", "x")
os.environ.setdefault("EMBEDDING_MODEL", "e")
os.environ.setdefault("DATA_DIR", "/tmp/rag_onyx_query_test")

import asyncio  # noqa: E402
import json  # noqa: E402
from typing import Any  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeRag:
    """Stand-in for the per-tenant ``RAGAnything`` instance.

    Captures the kwargs of the last ``aquery`` / ``aquery_vlm_enhanced``
    call so tests can assert on the conversation_history that the router
    forwarded after truncation.
    """

    def __init__(
        self,
        answer: str = "hello world",
        sources: list[dict[str, Any]] | None = None,
        tokens: dict[str, int] | None = None,
        raise_exc: BaseException | None = None,
    ) -> None:
        self._answer = answer
        self._sources = sources or []
        self._tokens = tokens
        self._raise = raise_exc
        self.last_call: dict[str, Any] | None = None

    async def aquery(self, question: str, **kwargs: Any) -> Any:
        self.last_call = {"question": question, **kwargs}
        if self._raise is not None:
            raise self._raise
        return {
            "answer": self._answer,
            "sources": self._sources,
            "tokens": self._tokens,
        }

    async def aquery_vlm_enhanced(self, question: str, **kwargs: Any) -> Any:
        return await self.aquery(question, **kwargs)


class FakeCache:
    """Stand-in for :class:`RAGAnythingCache` used by the route deps."""

    def __init__(self, rag: FakeRag) -> None:
        self._rag = rag
        self.requested: list[str] = []

    async def get(self, kb_id: str) -> FakeRag:
        self.requested.append(kb_id)
        return self._rag


class _FakeSession:
    """Best-effort no-op DB session — analytics writes can fail silently."""

    def add(self, _obj: Any) -> None:  # noqa: D401
        pass

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass


# ---------------------------------------------------------------------------
# App builder
# ---------------------------------------------------------------------------


_TOKEN = "a" * 96


def _build_app(rag: FakeRag, *, kb_id: str = "onyx-test") -> tuple[FastAPI, FakeCache]:
    """Mount the onyx_query router with auth / DB / RAG-cache stubs.

    Auth is stubbed by overriding :func:`onyx_service_auth` directly so
    tests don't need to seed a real ``Tenant`` row — token / kb-header
    semantics are already covered by ``test_auth_onyx``. The two
    dedicated header tests below exercise the unstubbed dep (we provide
    a real DB session for those cases).
    """
    from rag_service.api.auth_onyx import OnyxCallContext, onyx_service_auth
    from rag_service.api.deps import get_db_session, get_rag_cache
    from rag_service.api.routers import onyx_query as onyx_query_mod

    app = FastAPI()
    app.include_router(onyx_query_mod.router)

    cache = FakeCache(rag)
    sess = _FakeSession()

    async def _db_override():
        yield sess

    async def _cache_override():
        return cache

    async def _auth_override() -> OnyxCallContext:
        return OnyxCallContext(
            kb_id=kb_id,
            onyx_user_id="u_test",
            request_id="req_abc",
            caller_ip="127.0.0.1",
        )

    app.dependency_overrides[get_db_session] = _db_override
    app.dependency_overrides[get_rag_cache] = _cache_override
    app.dependency_overrides[onyx_service_auth] = _auth_override
    return app, cache


def _build_app_real_auth() -> FastAPI:
    """Variant for the two header-validation tests — keeps real auth_onyx.

    Stubs out ``get_db_session`` with a no-op session so the dep doesn't
    try to talk to a real Postgres for the KB-existence check.
    """
    from rag_service.api.deps import get_db_session, get_rag_cache
    from rag_service.api.routers import onyx_query as onyx_query_mod

    app = FastAPI()
    app.include_router(onyx_query_mod.router)
    sess = _FakeSession()

    async def _db_override():
        yield sess

    async def _cache_override():
        return FakeCache(FakeRag())

    app.dependency_overrides[get_db_session] = _db_override
    app.dependency_overrides[get_rag_cache] = _cache_override
    return app


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------


def _parse_sse(body_text: str) -> list[tuple[str | None, dict | None, str | None]]:
    """Parse an SSE blob into ``(event, json-data, raw-comment)`` triples.

    Comment frames (``: keepalive``) come through as ``(None, None, ":...")``.
    Returns one entry per ``\\n\\n``-separated frame.
    """
    out: list[tuple[str | None, dict | None, str | None]] = []
    for raw in body_text.split("\n\n"):
        if not raw.strip():
            continue
        event: str | None = None
        data_lines: list[str] = []
        comment: str | None = None
        for line in raw.split("\n"):
            if line.startswith(":"):
                comment = line
                continue
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].lstrip())
        data: dict | None = None
        if data_lines:
            try:
                data = json.loads("\n".join(data_lines))
            except json.JSONDecodeError:
                data = None
        out.append((event, data, comment))
    return out


# ===========================================================================
# /v1/onyx/query — SSE
# ===========================================================================


async def test_query_sse_returns_meta_chunks_done_in_order():
    """Mock answer ``"hello world"`` → meta → chunk… → done."""
    rag = FakeRag(answer="hello world")
    app, _cache = _build_app(rag)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.post(
            "/v1/onyx/query",
            json={"question": "hi"},
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert r.status_code == 200, r.text
    frames = [f for f in _parse_sse(r.text) if f[0] is not None]
    events = [f[0] for f in frames]
    # meta is first; done is last; chunk(s) in between.
    assert events[0] == "meta", events
    assert events[-1] == "done", events
    assert all(e == "chunk" for e in events[1:-1]), events
    assert events.count("chunk") >= 1


async def test_query_sse_done_payload_has_answer_and_sources():
    """Final ``done`` event JSON carries answer/sources/latency_ms/tokens."""
    rag = FakeRag(
        answer="42",
        sources=[
            {"document_id": "d1", "file_name": "x.pdf", "snippet": "s", "score": 0.9}
        ],
        tokens={"in": 10, "out": 20},
    )
    app, _cache = _build_app(rag)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.post(
            "/v1/onyx/query",
            json={"question": "what is the answer?"},
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert r.status_code == 200, r.text
    frames = [f for f in _parse_sse(r.text) if f[0] is not None]
    done = [f for f in frames if f[0] == "done"]
    assert len(done) == 1
    payload = done[0][1]
    assert payload is not None
    assert payload["answer"] == "42"
    assert isinstance(payload["sources"], list) and len(payload["sources"]) == 1
    assert payload["sources"][0]["document_id"] == "d1"
    assert payload["sources"][0]["file_name"] == "x.pdf"
    assert isinstance(payload["latency_ms"], int)
    assert payload["tokens"] == {"in": 10, "out": 20}


async def test_query_sse_include_sources_false_returns_empty():
    """``include_sources=False`` → done.sources is an empty list."""
    rag = FakeRag(
        answer="ok",
        sources=[{"document_id": "d1", "snippet": "s"}],
    )
    app, _cache = _build_app(rag)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.post(
            "/v1/onyx/query",
            json={"question": "?", "include_sources": False},
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert r.status_code == 200, r.text
    frames = [f for f in _parse_sse(r.text) if f[0] is not None]
    done = next(f for f in frames if f[0] == "done")
    assert done[1] is not None
    assert done[1]["sources"] == []


async def test_query_sse_history_passed_to_aquery_truncated():
    """10 history msgs + ``max_history_turns=3`` → only last 3 forwarded."""
    rag = FakeRag(answer="x")
    app, _cache = _build_app(rag)

    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"}
        for i in range(10)
    ]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.post(
            "/v1/onyx/query",
            json={
                "question": "?",
                "history": history,
                "max_history_turns": 3,
            },
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert r.status_code == 200, r.text
    assert rag.last_call is not None
    fwd = rag.last_call["conversation_history"]
    assert len(fwd) == 3
    # Tail-truncation: the last three messages travel.
    assert [m["content"] for m in fwd] == ["m7", "m8", "m9"]


async def test_query_sse_emits_error_event_on_upstream_502():
    """``HTTPStatusError(503)`` → final SSE event is ``error`` (HTTP 200)."""
    request = httpx.Request("POST", "http://llm/v1/chat/completions")
    response = httpx.Response(status_code=503, request=request)
    err = httpx.HTTPStatusError("svc unavail", request=request, response=response)
    rag = FakeRag(raise_exc=err)
    app, _cache = _build_app(rag)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.post(
            "/v1/onyx/query",
            json={"question": "?"},
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    # Streaming endpoint stays 200 even on upstream error — error rides
    # in the SSE body.
    assert r.status_code == 200, r.text
    frames = [f for f in _parse_sse(r.text) if f[0] is not None]
    events = [f[0] for f in frames]
    assert events[0] == "meta"
    assert events[-1] == "error", events
    err_payload = frames[-1][1]
    assert err_payload is not None
    assert err_payload["code"] == "upstream_llm_error"
    assert err_payload["retryable"] is True


async def test_query_sse_missing_token_401():
    """No Authorization header → 401 from auth_onyx (real auth path)."""
    import pytest as _pytest  # noqa: F401

    # Pin the internal token + clear any allowlist so the real auth dep
    # has a clean reference to compare against.
    from rag_service import config as _config_mod

    _config_mod.settings.internal_token = "z" * 96
    _config_mod.settings.internal_tokens_legacy = []
    _config_mod.settings.onyx_backend_allowed_cidrs = []

    app = _build_app_real_auth()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.post(
            "/v1/onyx/query",
            json={"question": "?"},
        )
    assert r.status_code == 401


async def test_query_sse_missing_kb_header_400(monkeypatch):
    """Token OK but no ``X-Onyx-KB-Id`` → 400 from auth_onyx."""
    from rag_service import config as _config_mod

    monkeypatch.setattr(_config_mod.settings, "internal_token", "a" * 96)
    monkeypatch.setattr(_config_mod.settings, "internal_tokens_legacy", [])
    monkeypatch.setattr(
        _config_mod.settings, "onyx_backend_allowed_cidrs", []
    )

    app = _build_app_real_auth()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.post(
            "/v1/onyx/query",
            json={"question": "?"},
            headers={"Authorization": f"Bearer {'a' * 96}"},
        )
    assert r.status_code == 400, r.text
    assert "X-Onyx-KB-Id" in r.json()["detail"]


# ===========================================================================
# /v1/onyx/query/sync
# ===========================================================================


async def test_query_sync_returns_full_response():
    """Happy path — 200 with answer / sources / latency_ms / request_id."""
    rag = FakeRag(
        answer="paris",
        sources=[
            {
                "document_id": "d1",
                "file_name": "fr.pdf",
                "chunk_id": "c1",
                "score": 0.7,
                "snippet": "Paris...",
                "modality": "text",
            }
        ],
        tokens={"in": 5, "out": 7},
    )
    app, _cache = _build_app(rag)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.post(
            "/v1/onyx/query/sync",
            json={"question": "capital?"},
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["answer"] == "paris"
    assert body["request_id"] == "req_abc"
    assert isinstance(body["latency_ms"], int)
    assert body["tokens"] == {"in": 5, "out": 7}
    assert len(body["sources"]) == 1
    assert body["sources"][0]["document_id"] == "d1"
    assert body["sources"][0]["page"] is None
    assert body["sources"][0]["bbox"] is None


async def test_query_sync_502_on_upstream_status_error():
    """``HTTPStatusError`` from RAGAnything → 502 Bad Gateway."""
    request = httpx.Request("POST", "http://llm/v1/chat/completions")
    response = httpx.Response(status_code=503, request=request)
    err = httpx.HTTPStatusError("svc unavail", request=request, response=response)
    rag = FakeRag(raise_exc=err)
    app, _cache = _build_app(rag)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.post(
            "/v1/onyx/query/sync",
            json={"question": "?"},
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert r.status_code == 502, r.text


async def test_query_sync_500_on_internal_error():
    """A non-HTTP exception from RAGAnything → 500."""
    rag = FakeRag(raise_exc=ValueError("oops"))
    app, _cache = _build_app(rag)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.post(
            "/v1/onyx/query/sync",
            json={"question": "?"},
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert r.status_code == 500, r.text


async def test_query_sync_truncates_history():
    """10 msgs + ``max_history_turns=2`` → only the last 2 are forwarded."""
    rag = FakeRag(answer="ok")
    app, _cache = _build_app(rag)

    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"}
        for i in range(10)
    ]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.post(
            "/v1/onyx/query/sync",
            json={
                "question": "?",
                "history": history,
                "max_history_turns": 2,
            },
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert r.status_code == 200, r.text
    assert rag.last_call is not None
    fwd = rag.last_call["conversation_history"]
    assert len(fwd) == 2
    assert [m["content"] for m in fwd] == ["m8", "m9"]


# ===========================================================================
# Validation — both endpoints share OnyxQueryRequest
# ===========================================================================


async def test_query_request_question_too_long_returns_422():
    """4001 chars (max=4000) → 422."""
    rag = FakeRag()
    app, _cache = _build_app(rag)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.post(
            "/v1/onyx/query/sync",
            json={"question": "x" * 4001},
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert r.status_code == 422


async def test_query_request_history_too_long_returns_422():
    """51 history messages (max=50) → 422."""
    rag = FakeRag()
    app, _cache = _build_app(rag)
    history = [{"role": "user", "content": f"m{i}"} for i in range(51)]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.post(
            "/v1/onyx/query/sync",
            json={"question": "?", "history": history},
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert r.status_code == 422


async def test_query_request_top_k_out_of_range_returns_422():
    """``top_k`` < 1 OR > 50 → 422."""
    rag = FakeRag()
    app, _cache = _build_app(rag)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r1 = await ac.post(
            "/v1/onyx/query/sync",
            json={"question": "?", "top_k": 0},
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
        r2 = await ac.post(
            "/v1/onyx/query/sync",
            json={"question": "?", "top_k": 51},
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert r1.status_code == 422
    assert r2.status_code == 422


# ===========================================================================
# Regression: heartbeat must not cancel an in-flight aquery
# ===========================================================================


async def test_query_sse_heartbeat_does_not_cancel_slow_aquery(monkeypatch):
    """A slow ``aquery`` (longer than several heartbeat ticks) must complete.

    Reproduction of the bug fixed in this commit: the original
    implementation wrapped ``agen.__anext__()`` in
    ``asyncio.wait_for(..., timeout=15)``. When the timeout fired while
    the upstream generator was suspended inside ``rag.aquery(...)``,
    ``wait_for`` cancelled the underlying coroutine and the
    CancelledError bubbled into the in-flight LLM call, killing it. Any
    query slower than ``HEARTBEAT_INTERVAL_SEC`` would therefore abort.

    The fix splits driver and consumer: a producer task pumps events
    into an ``asyncio.Queue``; the consumer's ``wait_for`` only ever
    times out the ``queue.get()``, so heartbeats never propagate
    cancellation upstream.

    Test strategy: shrink ``HEARTBEAT_INTERVAL_SEC`` to 0.05s so the
    heartbeat fires several times during a 0.3s ``aquery`` block. With
    the bug we'd see meta + (possibly) error and never a ``done``;
    with the fix we see meta + chunk + done plus at least one
    ``: keepalive`` comment line in the raw body.
    """
    # Shrink the heartbeat to a few ticks per second so this test runs
    # quickly while still exercising multiple keepalive emissions.
    monkeypatch.setattr(
        "rag_service.api.routers.onyx_query.HEARTBEAT_INTERVAL_SEC", 0.05
    )

    aquery_started = asyncio.Event()
    aquery_can_finish = asyncio.Event()

    class SlowRag:
        async def aquery(self, q: str, **kwargs: Any) -> Any:  # noqa: ARG002
            aquery_started.set()
            await aquery_can_finish.wait()
            return {"answer": "done", "sources": []}

        async def aquery_vlm_enhanced(self, q: str, **kwargs: Any) -> Any:
            return await self.aquery(q, **kwargs)

    rag = SlowRag()
    # ``_build_app`` is duck-typed; SlowRag duck-types as FakeRag.
    app, _cache = _build_app(rag)  # type: ignore[arg-type]

    async def _release():
        # Once aquery has started, sleep long enough for the heartbeat
        # ticker to fire several times (0.3s at a 0.05s interval ≈ 6
        # ticks of opportunity), then unblock the upstream call.
        await aquery_started.wait()
        await asyncio.sleep(0.3)
        aquery_can_finish.set()

    release_task = asyncio.create_task(_release())
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.post(
                "/v1/onyx/query",
                json={"question": "slow?"},
                headers={"Authorization": f"Bearer {_TOKEN}"},
            )
    finally:
        # Ensure the release helper is awaited regardless of test outcome.
        if not release_task.done():
            release_task.cancel()
            try:
                await release_task
            except (asyncio.CancelledError, Exception):
                pass

    assert r.status_code == 200, r.text
    raw = r.text

    # With the fix: at least one keepalive comment must appear (proves
    # the heartbeat fired) AND a ``done`` event must terminate the
    # stream (proves the aquery completed despite the heartbeats).
    assert ": keepalive" in raw, raw
    frames = [f for f in _parse_sse(raw) if f[0] is not None]
    events = [f[0] for f in frames]
    assert "done" in events, events
    # And no ``error`` event leaked from a cancelled aquery.
    assert "error" not in events, events
