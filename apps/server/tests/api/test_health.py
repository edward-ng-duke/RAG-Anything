"""Tests for ``rag_service.api.routers.health``: /healthz + /readyz."""

from __future__ import annotations

# Set required env vars before any rag_service import so the lazy
# ``settings`` singleton can be constructed without a real .env file.
import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x@x/x")
os.environ.setdefault("REDIS_URL", "redis://x")
os.environ.setdefault("INTERNAL_TOKEN", "x" * 64)
os.environ.setdefault("LLM_BASE_URL", "x")
os.environ.setdefault("LLM_API_KEY", "x")
os.environ.setdefault("LLM_MODEL", "x")
os.environ.setdefault("EMBEDDING_BASE_URL", "x")
os.environ.setdefault("EMBEDDING_API_KEY", "x")
os.environ.setdefault("EMBEDDING_MODEL", "x")

from unittest.mock import AsyncMock  # noqa: E402

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from rag_service.api.deps import get_db_session, get_redis  # noqa: E402
from rag_service.api.routers.health import router  # noqa: E402


@pytest.fixture
def app(tmp_path, monkeypatch):
    """FastAPI app with the health router mounted and data_dir pointed at tmp."""
    from rag_service.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    a = FastAPI()
    a.include_router(router)
    return a


def _override(app: FastAPI, *, db_ok: bool = True, redis_ok: bool = True) -> None:
    """Wire dependency overrides for db + redis with the given health state."""
    db = AsyncMock()
    if db_ok:
        db.execute = AsyncMock(return_value=None)
    else:
        db.execute = AsyncMock(side_effect=Exception("boom"))

    redis = AsyncMock()
    if redis_ok:
        redis.ping = AsyncMock(return_value=True)
    else:
        redis.ping = AsyncMock(side_effect=Exception("nope"))

    async def _db():
        yield db

    async def _redis():
        return redis

    app.dependency_overrides[get_db_session] = _db
    app.dependency_overrides[get_redis] = _redis


def test_healthz_always_ok(app):
    client = TestClient(app)
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_readyz_all_ok(app):
    _override(app, db_ok=True, redis_ok=True)
    client = TestClient(app)
    r = client.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert all(v == "ok" for v in body["checks"].values())
    assert set(body["checks"].keys()) == {"pg", "redis", "data_dir"}


def test_readyz_503_on_pg_fail(app):
    _override(app, db_ok=False, redis_ok=True)
    client = TestClient(app)
    r = client.get("/readyz")
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert detail["status"] == "not_ready"
    assert detail["checks"]["pg"].startswith("fail")
    assert detail["checks"]["redis"] == "ok"


def test_readyz_503_on_redis_fail(app):
    _override(app, db_ok=True, redis_ok=False)
    client = TestClient(app)
    r = client.get("/readyz")
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert detail["status"] == "not_ready"
    assert detail["checks"]["redis"].startswith("fail")
    assert detail["checks"]["pg"] == "ok"


def test_readyz_503_on_data_dir_fail(app, monkeypatch, tmp_path):
    """If data_dir cannot be created/written, /readyz returns 503."""
    from rag_service.config import settings

    # Point at a path under a regular file — mkdir(parents=True) will fail.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    bad_path = blocker / "subdir"
    monkeypatch.setattr(settings, "data_dir", bad_path)

    _override(app, db_ok=True, redis_ok=True)
    client = TestClient(app)
    r = client.get("/readyz")
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert detail["checks"]["data_dir"].startswith("fail")
