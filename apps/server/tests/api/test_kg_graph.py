"""Tests for ``rag_service.api.routers.kg`` — graph traversal endpoints.

Covers the Task 3.4 additions:

* ``GET /v1/kg/entities/{id}/neighbors`` routes through to ``graph.neighbors``;
* ``GET /v1/kg/subgraph`` routes through to ``graph.subgraph``;
* ``depth`` is validated by FastAPI's ``Query(ge=1, le=3)`` (out-of-range → 422);
* ``entities`` validation: empty → 400, > 50 IDs → 400;
* both endpoints require auth — bare requests get 401.

The cypher-layer module (``rag_service.kg.graph``) is patched with
``AsyncMock`` returns so the suite needs no Postgres or AGE.
"""

from __future__ import annotations

# Required env vars must be set BEFORE importing anything from
# ``rag_service`` — the ``settings`` singleton is constructed lazily on
# first attribute access and will trip on missing required vars.
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
os.environ.setdefault("DATA_DIR", "/tmp/rag_kg_graph_api_test")

from unittest.mock import AsyncMock  # noqa: E402

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from rag_service.api.auth import current_tenant  # noqa: E402
from rag_service.api.deps import get_db_session  # noqa: E402
from rag_service.api.routers import kg as kg_mod  # noqa: E402


# ---------------------------------------------------------------------------
# App / client wiring
# ---------------------------------------------------------------------------


def _make_app(tenant: str = "tnt-1") -> FastAPI:
    """Build a FastAPI app with the kg router and dependency overrides.

    ``current_tenant`` is short-circuited to a fixed string so tests don't
    need a real JWT. ``get_db_session`` yields a sentinel that the patched
    graph functions ignore — they're ``AsyncMock``s.
    """
    app = FastAPI()
    app.include_router(kg_mod.router)

    async def _db_override():
        yield object()  # placeholder, graph functions are mocked

    async def _tenant_override() -> str:
        return tenant

    app.dependency_overrides[get_db_session] = _db_override
    app.dependency_overrides[current_tenant] = _tenant_override
    return app


# ---------------------------------------------------------------------------
# Neighbors
# ---------------------------------------------------------------------------


def test_neighbors_routes_to_graph(monkeypatch):
    """``GET /v1/kg/entities/{id}/neighbors`` forwards to ``graph.neighbors``."""
    graph_mock = AsyncMock(
        return_value={
            "nodes": [
                {
                    "id": "ent-1",
                    "label": "Location",
                    "properties": {"entity_id": "ent-1", "name": "Paris"},
                },
                {
                    "id": "ent-2",
                    "label": "Country",
                    "properties": {"entity_id": "ent-2", "name": "France"},
                },
            ],
            "edges": [
                {
                    "source": "ent-1",
                    "target": "ent-2",
                    "type": "capital_of",
                    "properties": {"weight": 1.0},
                },
            ],
        }
    )
    monkeypatch.setattr(kg_mod.graph_mod, "neighbors", graph_mock)

    app = _make_app(tenant="tnt-7")
    client = TestClient(app)
    r = client.get("/v1/kg/entities/ent-1/neighbors", params={"depth": 2})

    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["nodes"]) == 2
    assert body["nodes"][0] == {
        "id": "ent-1",
        "label": "Location",
        "properties": {"entity_id": "ent-1", "name": "Paris"},
    }
    assert len(body["edges"]) == 1
    assert body["edges"][0] == {
        "source": "ent-1",
        "target": "ent-2",
        "type": "capital_of",
        "properties": {"weight": 1.0},
    }

    graph_mock.assert_awaited_once()
    args, kwargs = graph_mock.call_args
    # Signature: neighbors(db, tenant_id, entity_id, *, depth)
    assert args[1] == "tnt-7"
    assert args[2] == "ent-1"
    assert kwargs == {"depth": 2}


def test_neighbors_invalid_depth_422(monkeypatch):
    """``depth=4`` violates the ``le=3`` ``Query`` validator → 422."""
    # The graph module shouldn't be hit at all — fail loudly if it is.
    graph_mock = AsyncMock(side_effect=AssertionError("must not be called"))
    monkeypatch.setattr(kg_mod.graph_mod, "neighbors", graph_mock)

    app = _make_app()
    client = TestClient(app)
    r = client.get("/v1/kg/entities/ent-1/neighbors", params={"depth": 4})
    assert r.status_code == 422, r.text
    graph_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# Subgraph
# ---------------------------------------------------------------------------


def test_subgraph_with_multiple_entities(monkeypatch):
    """Comma-separated ``entities`` are split + forwarded to ``graph.subgraph``."""
    graph_mock = AsyncMock(
        return_value={
            "nodes": [
                {"id": "a", "label": "Entity", "properties": {"entity_id": "a"}},
                {"id": "b", "label": "Entity", "properties": {"entity_id": "b"}},
                {"id": "c", "label": "Entity", "properties": {"entity_id": "c"}},
            ],
            "edges": [
                {
                    "source": "a",
                    "target": "b",
                    "type": "REL",
                    "properties": {},
                },
            ],
        }
    )
    monkeypatch.setattr(kg_mod.graph_mod, "subgraph", graph_mock)

    app = _make_app(tenant="tnt-9")
    client = TestClient(app)
    r = client.get(
        "/v1/kg/subgraph",
        # Whitespace around commas is tolerated by the splitter.
        params={"entities": "a, b ,c", "depth": 3},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert {n["id"] for n in body["nodes"]} == {"a", "b", "c"}
    assert len(body["edges"]) == 1

    graph_mock.assert_awaited_once()
    args, kwargs = graph_mock.call_args
    # Signature: subgraph(db, tenant_id, entity_ids, *, depth)
    assert args[1] == "tnt-9"
    assert args[2] == ["a", "b", "c"]
    assert kwargs == {"depth": 3}


def test_subgraph_empty_entities_400(monkeypatch):
    """All-whitespace / empty ``entities`` collapse to a 400 before the cypher call."""
    graph_mock = AsyncMock(side_effect=AssertionError("must not be called"))
    monkeypatch.setattr(kg_mod.graph_mod, "subgraph", graph_mock)

    app = _make_app()
    client = TestClient(app)

    # All-whitespace input: split yields no non-empty tokens.
    r = client.get("/v1/kg/subgraph", params={"entities": "  , ,"})
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "no entity ids provided"
    graph_mock.assert_not_awaited()


def test_subgraph_too_many_entities_400(monkeypatch):
    """51 IDs trip the ``len > 50`` guard → 400."""
    graph_mock = AsyncMock(side_effect=AssertionError("must not be called"))
    monkeypatch.setattr(kg_mod.graph_mod, "subgraph", graph_mock)

    app = _make_app()
    client = TestClient(app)
    ids = ",".join(f"ent-{i}" for i in range(51))
    r = client.get("/v1/kg/subgraph", params={"entities": ids})
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "max 50 entity ids"
    graph_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", "/v1/kg/entities/anything/neighbors"),
        ("GET", "/v1/kg/subgraph?entities=a,b"),
    ],
)
def test_requires_auth(method, path):
    """Without a JWT, both traversal endpoints collapse to 401.

    We intentionally do NOT override ``current_tenant`` here so the real
    auth dep runs and rejects the bare request. ``get_db_session`` is
    still overridden to avoid a Postgres connect attempt during dep
    resolution.
    """
    app = FastAPI()
    app.include_router(kg_mod.router)

    async def _db_override():
        yield object()

    app.dependency_overrides[get_db_session] = _db_override

    client = TestClient(app)
    r = client.request(method, path)
    assert r.status_code == 401, r.text
