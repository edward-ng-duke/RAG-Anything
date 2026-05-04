"""Tests for ``rag_service.api.routers.kg`` — flat KG endpoints.

The repository layer (``rag_service.kg.repository``) is patched with
``AsyncMock`` returns so the suite needs no real Postgres or LightRAG
tables. We exercise:

* ``GET /v1/kg/entities`` routes query params through to the repo;
* ``GET /v1/kg/entities/{id}`` 404s when missing, 200s when found;
* ``GET /v1/kg/relations`` routes filters and pagination cursor;
* ``GET /v1/kg/chunks/{id}`` returns the single-row payload;
* ``GET /v1/kg/stats`` surfaces the per-tenant counts;
* every endpoint requires JWT auth — bare requests get 401.
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
os.environ.setdefault("DATA_DIR", "/tmp/rag_kg_api_test")

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
    repo functions ignore — they're ``AsyncMock``s.
    """
    app = FastAPI()
    app.include_router(kg_mod.router)

    async def _db_override():
        yield object()  # placeholder, repo functions are mocked

    async def _tenant_override() -> str:
        return tenant

    app.dependency_overrides[get_db_session] = _db_override
    app.dependency_overrides[current_tenant] = _tenant_override
    return app


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


def test_list_entities_routes_to_repo(monkeypatch):
    """Query params are forwarded to ``repository.list_entities`` verbatim."""
    repo_mock = AsyncMock(
        return_value={
            "items": [
                {
                    "id": "ent-1",
                    "entity_name": "Paris",
                    "content": "Capital of France.",
                    "file_path": "geo.pdf",
                },
                {
                    "id": "ent-2",
                    "entity_name": "France",
                    "content": "Country in Europe.",
                    "file_path": "geo.pdf",
                },
            ],
            "next_cursor": "next-page-cursor",
        }
    )
    monkeypatch.setattr(kg_mod.repository, "list_entities", repo_mock)

    app = _make_app(tenant="tnt-7")
    client = TestClient(app)
    r = client.get(
        "/v1/kg/entities",
        params={
            "type": "Location",
            "search": "par",
            "cursor": "abc",
            "limit": 25,
        },
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["items"]) == 2
    assert body["items"][0] == {
        "id": "ent-1",
        "entity_name": "Paris",
        "entity_type": None,
        "content": "Capital of France.",
        "file_path": "geo.pdf",
        "properties": None,
    }
    assert body["next_cursor"] == "next-page-cursor"
    repo_mock.assert_awaited_once()
    args, kwargs = repo_mock.call_args
    # First positional is the db session, second is tenant_id.
    assert args[1] == "tnt-7"
    assert kwargs == {
        "type": "Location",
        "search": "par",
        "cursor": "abc",
        "limit": 25,
    }


def test_get_entity_404_when_missing(monkeypatch):
    """``None`` from ``get_entity`` collapses to 404."""
    monkeypatch.setattr(
        kg_mod.repository,
        "get_entity",
        AsyncMock(return_value=None),
    )

    app = _make_app()
    client = TestClient(app)
    r = client.get("/v1/kg/entities/missing-id")
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "entity not found"


def test_get_entity_200_when_found(monkeypatch):
    """Found row is normalized and returned as :class:`KGEntity`."""
    monkeypatch.setattr(
        kg_mod.repository,
        "get_entity",
        AsyncMock(
            return_value={
                "id": "ent-42",
                "entity_name": "Eiffel Tower",
                "content": "Iron lattice tower in Paris.",
                "file_path": "monuments.pdf",
                # Extra columns the repo may surface (e.g. content_vector)
                # are silently dropped by the normalizer.
                "content_vector": [0.1, 0.2, 0.3],
            }
        ),
    )

    app = _make_app()
    client = TestClient(app)
    r = client.get("/v1/kg/entities/ent-42")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == "ent-42"
    assert body["entity_name"] == "Eiffel Tower"
    assert body["content"] == "Iron lattice tower in Paris."
    assert body["file_path"] == "monuments.pdf"
    assert body["entity_type"] is None
    assert body["properties"] is None
    # content_vector must NOT leak onto the wire.
    assert "content_vector" not in body


# ---------------------------------------------------------------------------
# Relations
# ---------------------------------------------------------------------------


def test_list_relations(monkeypatch):
    """Filters + cursor + limit forwarded to ``repository.list_relations``."""
    repo_mock = AsyncMock(
        return_value={
            "items": [
                {
                    "id": "rel-1",
                    "source_id": "Paris",
                    "target_id": "France",
                    "content": "Paris is the capital of France.",
                    "file_path": "geo.pdf",
                },
            ],
            "next_cursor": None,
        }
    )
    monkeypatch.setattr(kg_mod.repository, "list_relations", repo_mock)

    app = _make_app(tenant="tnt-9")
    client = TestClient(app)
    r = client.get(
        "/v1/kg/relations",
        params={
            "source": "Paris",
            "target": "France",
            "type": "capital_of",
            "limit": 10,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["id"] == "rel-1"
    assert item["source_id"] == "Paris"
    assert item["target_id"] == "France"
    assert item["content"] == "Paris is the capital of France."
    assert item["file_path"] == "geo.pdf"
    # ``type`` is None on the wire because the repo row didn't include it.
    assert item["type"] is None
    assert body["next_cursor"] is None

    repo_mock.assert_awaited_once()
    args, kwargs = repo_mock.call_args
    assert args[1] == "tnt-9"
    assert kwargs == {
        "source": "Paris",
        "target": "France",
        "type": "capital_of",
        "cursor": None,
        "limit": 10,
    }


# ---------------------------------------------------------------------------
# Chunks
# ---------------------------------------------------------------------------


def test_get_chunk(monkeypatch):
    """A found chunk is rendered as :class:`KGChunk`; missing → 404."""
    monkeypatch.setattr(
        kg_mod.repository,
        "get_chunk",
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

    app = _make_app()
    client = TestClient(app)
    r = client.get("/v1/kg/chunks/chunk-1")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "id": "chunk-1",
        "content": "Paris is the capital of France.",
        "full_doc_id": "doc-99",
        "chunk_order_index": 3,
        "tokens": 12,
        "file_path": "geo.pdf",
    }

    # And a 404 path.
    monkeypatch.setattr(
        kg_mod.repository,
        "get_chunk",
        AsyncMock(return_value=None),
    )
    r = client.get("/v1/kg/chunks/nope")
    assert r.status_code == 404
    assert r.json()["detail"] == "chunk not found"


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


def test_stats(monkeypatch):
    """``GET /v1/kg/stats`` surfaces the repo's per-tenant counts."""
    repo_mock = AsyncMock(
        return_value={"entities": 17, "relations": 23, "chunks": 41}
    )
    monkeypatch.setattr(kg_mod.repository, "stats", repo_mock)

    app = _make_app(tenant="tnt-5")
    client = TestClient(app)
    r = client.get("/v1/kg/stats")
    assert r.status_code == 200, r.text
    assert r.json() == {"entities": 17, "relations": 23, "chunks": 41}

    repo_mock.assert_awaited_once()
    args, _kwargs = repo_mock.call_args
    assert args[1] == "tnt-5"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", "/v1/kg/entities"),
        ("GET", "/v1/kg/entities/anything"),
        ("GET", "/v1/kg/relations"),
        ("GET", "/v1/kg/chunks/anything"),
        ("GET", "/v1/kg/stats"),
    ],
)
def test_requires_auth(method, path):
    """Without a JWT, every endpoint collapses to 401.

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
