"""Tests for ``rag_service.api.routers.onyx_kg`` — /v1/onyx/kg.

Service-to-service KG inspection endpoints. Mirrors α's ``/v1/kg`` 1:1
but pulls ``tenant_id`` from ``X-Onyx-KB-Id`` (via
:func:`onyx_service_auth`) instead of the JWT.

Endpoints exercised:

* ``GET /v1/onyx/kg/entities``                       — list (cursor + filters).
* ``GET /v1/onyx/kg/entities/{entity_id}``           — single entity.
* ``GET /v1/onyx/kg/entities/{entity_id}/neighbors`` — k-hop neighbours.
* ``GET /v1/onyx/kg/relations``                      — list (cursor + filters).
* ``GET /v1/onyx/kg/chunks/{chunk_id}``              — single chunk.
* ``GET /v1/onyx/kg/stats``                          — per-tenant counts.
* ``GET /v1/onyx/kg/subgraph``                       — multi-source traversal.

The repository / graph modules are monkey-patched with ``AsyncMock``
returns so the suite needs no real Postgres or AGE. Auth-and-isolation
tests use a real SQLite engine + auth dep, exactly like the rest of the
onyx test suite.
"""

from __future__ import annotations

# conftest.py at tests/ already populated the env vars. Override DATA_DIR
# locally so a stray Path resolution doesn't pollute another test's dir.
import os  # noqa: E402

os.environ.setdefault("DATA_DIR", "/tmp/rag_onyx_kg_test")

import uuid  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB, UUID  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.ext.compiler import compiles  # noqa: E402
from sqlalchemy.schema import ColumnDefault  # noqa: E402


# ---------------------------------------------------------------------------
# PG → SQLite schema patches (mirrors tests/api/test_onyx_kb.py)
# ---------------------------------------------------------------------------


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):  # noqa: ARG001
    return "JSON"


@compiles(UUID, "sqlite")
def _compile_uuid_sqlite(element, compiler, **kw):  # noqa: ARG001
    return "CHAR(36)"


_id_counter = {"n": 0}


def _next_id() -> int:
    _id_counter["n"] += 1
    return _id_counter["n"]


def _patch_metadata_for_sqlite() -> None:
    from rag_service.db.base import Base
    from rag_service.db import models  # noqa: F401 — registers tables

    for tbl in Base.metadata.tables.values():
        for col in tbl.columns:
            sd = col.server_default
            if sd is not None:
                arg = getattr(sd, "arg", None)
                if arg is not None:
                    rendered = str(arg)
                    if "::jsonb" in rendered:
                        col.server_default = None
                        if col.default is None:
                            col.default = ColumnDefault(lambda: {})
                    elif "gen_random_uuid" in rendered:
                        col.server_default = None
                        if col.default is None:
                            col.default = ColumnDefault(lambda: uuid.uuid4())
            if getattr(col, "identity", None) is not None:
                col.identity = None
                col.autoincrement = True
                if col.primary_key and col.default is None:
                    col.default = ColumnDefault(_next_id)


_patch_metadata_for_sqlite()


# ---------------------------------------------------------------------------
# Auth fixture — known token, no legacy, no CIDR allowlist
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _auth_setup(monkeypatch):
    monkeypatch.setattr("rag_service.config.settings.internal_token", "a" * 96)
    monkeypatch.setattr("rag_service.config.settings.internal_tokens_legacy", [])
    monkeypatch.setattr(
        "rag_service.config.settings.onyx_backend_allowed_cidrs", []
    )


_TOKEN = "a" * 96


def _headers(kb_id: str | None = None) -> dict[str, str]:
    """Build the standard auth headers, optionally with X-Onyx-KB-Id."""
    h = {
        "Authorization": f"Bearer {_TOKEN}",
        "X-Onyx-User-Id": "u_test",
    }
    if kb_id is not None:
        h["X-Onyx-KB-Id"] = kb_id
    return h


# ---------------------------------------------------------------------------
# Per-test SQLite engine + tenant seeding
# ---------------------------------------------------------------------------


@pytest.fixture
async def session_maker():
    """Fresh in-memory SQLite engine + bound async sessionmaker per test."""
    from rag_service.db.base import Base
    from rag_service.db import models  # noqa: F401 — registers tables

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield sm
    finally:
        await engine.dispose()


async def _seed_kb(session_maker, *, suffix: str = "") -> str:
    """Insert a ``source=onyx`` Tenant row directly and return its kb_id."""
    from rag_service.db.models import Tenant

    kb_id = f"onyx-{uuid.uuid4()}"
    async with session_maker() as s:
        s.add(
            Tenant(
                tenant_id=kb_id,
                display_name=f"KB {suffix or 'test'}",
                storage_quota_mb=1024,
                config_json={"source": "onyx"},
            )
        )
        await s.commit()
    return kb_id


def _build_app(session_maker) -> FastAPI:
    """Mount the onyx_kg router with the SQLite session override."""
    from rag_service.api.deps import get_db_session
    from rag_service.api.routers import onyx_kg as onyx_kg_mod

    app = FastAPI()
    app.include_router(onyx_kg_mod.router)

    async def _db_override():
        async with session_maker() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    app.dependency_overrides[get_db_session] = _db_override
    return app


# ===========================================================================
# GET /v1/onyx/kg/entities
# ===========================================================================


async def test_list_entities_returns_items(session_maker, monkeypatch):
    """Happy path — repo returns 1 entity; router echoes through KGEntity shape."""
    kb_id = await _seed_kb(session_maker)
    repo_mock = AsyncMock(
        return_value={
            "items": [
                {
                    "id": "ent-1",
                    "entity_name": "Paris",
                    "content": "Capital of France.",
                    "file_path": "geo.pdf",
                }
            ],
            "next_cursor": None,
        }
    )
    monkeypatch.setattr(
        "rag_service.kg.repository.list_entities", repo_mock
    )

    app = _build_app(session_maker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get("/v1/onyx/kg/entities", headers=_headers(kb_id))

    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["id"] == "ent-1"
    assert body["items"][0]["entity_name"] == "Paris"
    assert body["next_cursor"] is None


async def test_list_entities_passes_kb_id_to_repo(session_maker, monkeypatch):
    """The kb_id from X-Onyx-KB-Id is forwarded as the second positional arg."""
    kb_id = await _seed_kb(session_maker)
    repo_mock = AsyncMock(return_value={"items": [], "next_cursor": None})
    monkeypatch.setattr(
        "rag_service.kg.repository.list_entities", repo_mock
    )

    app = _build_app(session_maker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/v1/onyx/kg/entities",
            headers=_headers(kb_id),
            params={"type": "Location", "search": "par", "limit": 25},
        )

    assert r.status_code == 200, r.text
    repo_mock.assert_awaited_once()
    args, kwargs = repo_mock.call_args
    # signature: list_entities(db, tenant_id, *, search, cursor, limit) — the
    # router takes ``type`` from clients for forward-compat but the
    # repository ignores it (no entity_type column).
    assert args[1] == kb_id
    assert kwargs == {
        "search": "par",
        "cursor": None,
        "limit": 25,
    }


# ===========================================================================
# GET /v1/onyx/kg/entities/{entity_id}
# ===========================================================================


async def test_get_entity_returns_full(session_maker, monkeypatch):
    """Repo returns a row dict; router renders it as KGEntity."""
    kb_id = await _seed_kb(session_maker)
    monkeypatch.setattr(
        "rag_service.kg.repository.get_entity",
        AsyncMock(
            return_value={
                "id": "ent-42",
                "entity_name": "Eiffel Tower",
                "content": "Iron lattice tower.",
                "file_path": "monuments.pdf",
            }
        ),
    )

    app = _build_app(session_maker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/v1/onyx/kg/entities/ent-42", headers=_headers(kb_id)
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == "ent-42"
    assert body["entity_name"] == "Eiffel Tower"
    assert body["content"] == "Iron lattice tower."
    assert body["file_path"] == "monuments.pdf"


async def test_get_entity_404_when_repo_returns_none(session_maker, monkeypatch):
    """``None`` from the repo collapses to 404."""
    kb_id = await _seed_kb(session_maker)
    monkeypatch.setattr(
        "rag_service.kg.repository.get_entity", AsyncMock(return_value=None)
    )

    app = _build_app(session_maker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/v1/onyx/kg/entities/missing", headers=_headers(kb_id)
        )

    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "entity not found"


# ===========================================================================
# GET /v1/onyx/kg/relations
# ===========================================================================


async def test_list_relations_returns_items(session_maker, monkeypatch):
    """Happy path — list_relations result echoed through KGRelation."""
    kb_id = await _seed_kb(session_maker)
    repo_mock = AsyncMock(
        return_value={
            "items": [
                {
                    "id": "rel-1",
                    "source_id": "Paris",
                    "target_id": "France",
                    "content": "Paris is the capital of France.",
                    "file_path": "geo.pdf",
                }
            ],
            "next_cursor": None,
        }
    )
    monkeypatch.setattr(
        "rag_service.kg.repository.list_relations", repo_mock
    )

    app = _build_app(session_maker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/v1/onyx/kg/relations",
            headers=_headers(kb_id),
            params={"source": "Paris", "target": "France", "limit": 10},
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["source_id"] == "Paris"
    assert body["items"][0]["target_id"] == "France"

    args, kwargs = repo_mock.call_args
    assert args[1] == kb_id
    # ``type`` accepted by the router for API stability but not forwarded
    # to repository.list_relations (no edge_type column).
    assert kwargs == {
        "source": "Paris",
        "target": "France",
        "cursor": None,
        "limit": 10,
    }


# ===========================================================================
# GET /v1/onyx/kg/chunks/{chunk_id}
# ===========================================================================


async def test_get_chunk_returns_chunk(session_maker, monkeypatch):
    """Repo returns a chunk dict; router echoes it verbatim through KGChunk."""
    kb_id = await _seed_kb(session_maker)
    monkeypatch.setattr(
        "rag_service.kg.repository.get_chunk",
        AsyncMock(
            return_value={
                "id": "chunk-1",
                "content": "Paris is the capital of France.",
                "full_doc_id": "doc-99",
                "chunk_order_index": 3,
                "tokens": 12,
                "file_path": "geo.pdf",
            }
        ),
    )

    app = _build_app(session_maker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/v1/onyx/kg/chunks/chunk-1", headers=_headers(kb_id)
        )

    assert r.status_code == 200, r.text
    assert r.json() == {
        "id": "chunk-1",
        "content": "Paris is the capital of France.",
        "full_doc_id": "doc-99",
        "chunk_order_index": 3,
        "tokens": 12,
        "file_path": "geo.pdf",
    }


async def test_get_chunk_404_when_missing(session_maker, monkeypatch):
    """``None`` from get_chunk → 404."""
    kb_id = await _seed_kb(session_maker)
    monkeypatch.setattr(
        "rag_service.kg.repository.get_chunk", AsyncMock(return_value=None)
    )

    app = _build_app(session_maker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/v1/onyx/kg/chunks/nope", headers=_headers(kb_id)
        )

    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "chunk not found"


# ===========================================================================
# GET /v1/onyx/kg/stats
# ===========================================================================


async def test_stats_returns_counts(session_maker, monkeypatch):
    """Stats dict from repo is echoed verbatim."""
    kb_id = await _seed_kb(session_maker)
    repo_mock = AsyncMock(
        return_value={"entities": 10, "relations": 20, "chunks": 30}
    )
    monkeypatch.setattr("rag_service.kg.repository.stats", repo_mock)

    app = _build_app(session_maker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get("/v1/onyx/kg/stats", headers=_headers(kb_id))

    assert r.status_code == 200, r.text
    assert r.json() == {"entities": 10, "relations": 20, "chunks": 30}

    args, _ = repo_mock.call_args
    assert args[1] == kb_id


# ===========================================================================
# GET /v1/onyx/kg/entities/{id}/neighbors
# ===========================================================================


async def test_neighbors_calls_graph_module(session_maker, monkeypatch):
    """``graph.neighbors`` result is rendered as KGSubgraphResponse."""
    kb_id = await _seed_kb(session_maker)
    graph_mock = AsyncMock(
        return_value={
            "nodes": [
                {
                    "id": "ent-1",
                    "label": "Location",
                    "properties": {"entity_id": "ent-1"},
                },
                {
                    "id": "ent-2",
                    "label": "Country",
                    "properties": {"entity_id": "ent-2"},
                },
            ],
            "edges": [
                {
                    "source": "ent-1",
                    "target": "ent-2",
                    "type": "capital_of",
                    "properties": {},
                }
            ],
        }
    )
    monkeypatch.setattr("rag_service.kg.graph.neighbors", graph_mock)

    app = _build_app(session_maker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/v1/onyx/kg/entities/ent-1/neighbors",
            headers=_headers(kb_id),
            params={"depth": 2},
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert {n["id"] for n in body["nodes"]} == {"ent-1", "ent-2"}
    assert len(body["edges"]) == 1
    assert body["edges"][0]["type"] == "capital_of"

    args, kwargs = graph_mock.call_args
    # signature: neighbors(db, tenant_id, entity_id, *, depth)
    assert args[1] == kb_id
    assert args[2] == "ent-1"
    assert kwargs == {"depth": 2}


async def test_neighbors_depth_clamp(session_maker, monkeypatch):
    """``depth=4`` violates ``Query(le=3)`` → 422."""
    kb_id = await _seed_kb(session_maker)
    graph_mock = AsyncMock(side_effect=AssertionError("must not be called"))
    monkeypatch.setattr("rag_service.kg.graph.neighbors", graph_mock)

    app = _build_app(session_maker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/v1/onyx/kg/entities/ent-1/neighbors",
            headers=_headers(kb_id),
            params={"depth": 4},
        )

    assert r.status_code == 422, r.text
    graph_mock.assert_not_awaited()


# ===========================================================================
# GET /v1/onyx/kg/subgraph
# ===========================================================================


async def test_subgraph_with_entities_param(session_maker, monkeypatch):
    """Comma-separated ``entities`` are split + forwarded as a list."""
    kb_id = await _seed_kb(session_maker)
    graph_mock = AsyncMock(
        return_value={
            "nodes": [
                {"id": "a", "label": "Entity", "properties": {}},
                {"id": "b", "label": "Entity", "properties": {}},
                {"id": "c", "label": "Entity", "properties": {}},
            ],
            "edges": [],
        }
    )
    monkeypatch.setattr("rag_service.kg.graph.subgraph", graph_mock)

    app = _build_app(session_maker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/v1/onyx/kg/subgraph",
            headers=_headers(kb_id),
            # whitespace tolerant — splitter strips
            params={"entities": "a, b ,c", "depth": 3},
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert {n["id"] for n in body["nodes"]} == {"a", "b", "c"}

    args, kwargs = graph_mock.call_args
    # signature: subgraph(db, tenant_id, entity_ids, *, depth)
    assert args[1] == kb_id
    assert args[2] == ["a", "b", "c"]
    assert kwargs == {"depth": 3}


async def test_subgraph_too_many_entities_returns_400(session_maker, monkeypatch):
    """51 IDs trip the ``len > 50`` guard → 400."""
    kb_id = await _seed_kb(session_maker)
    graph_mock = AsyncMock(side_effect=AssertionError("must not be called"))
    monkeypatch.setattr("rag_service.kg.graph.subgraph", graph_mock)

    app = _build_app(session_maker)
    transport = ASGITransport(app=app)
    ids = ",".join(f"ent-{i}" for i in range(51))
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/v1/onyx/kg/subgraph",
            headers=_headers(kb_id),
            params={"entities": ids},
        )

    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "max 50 entity ids"
    graph_mock.assert_not_awaited()


async def test_subgraph_empty_entities_returns_400(session_maker, monkeypatch):
    """All-whitespace ``entities`` collapses to 400 before any cypher call."""
    kb_id = await _seed_kb(session_maker)
    graph_mock = AsyncMock(side_effect=AssertionError("must not be called"))
    monkeypatch.setattr("rag_service.kg.graph.subgraph", graph_mock)

    app = _build_app(session_maker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/v1/onyx/kg/subgraph",
            headers=_headers(kb_id),
            params={"entities": "  , ,"},
        )

    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "no entity ids provided"
    graph_mock.assert_not_awaited()


# ===========================================================================
# Auth & isolation
# ===========================================================================


async def test_kg_endpoint_missing_token_401(session_maker):
    """No Authorization header → 401."""
    kb_id = await _seed_kb(session_maker)
    app = _build_app(session_maker)
    transport = ASGITransport(app=app)
    # Strip the bearer header, keep X-Onyx-KB-Id so we exercise the
    # missing-token branch (not the missing-kb one).
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/v1/onyx/kg/stats",
            headers={"X-Onyx-KB-Id": kb_id, "X-Onyx-User-Id": "u_test"},
        )
    assert r.status_code == 401, r.text


async def test_kg_endpoint_missing_kb_header_400(session_maker):
    """Authorization OK but no X-Onyx-KB-Id → 400."""
    app = _build_app(session_maker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get("/v1/onyx/kg/stats", headers=_headers())  # no kb_id
    assert r.status_code == 400, r.text
    assert "X-Onyx-KB-Id" in r.json()["detail"]


async def test_kg_endpoint_unknown_kb_404(session_maker):
    """A well-formed kb_id with no matching tenant row → 404."""
    app = _build_app(session_maker)
    transport = ASGITransport(app=app)
    bogus = f"onyx-{uuid.uuid4()}"
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get("/v1/onyx/kg/stats", headers=_headers(bogus))
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "kb not found"
