"""Tests for ``rag_service.api.routers.auth`` — refresh / logout / select_tenant.

Covers the session-management surface added in Task 2.5:

* ``refresh`` mints a new access token from a still-valid refresh, rejects
  revoked / wrong-type / undecodable refresh tokens.
* ``logout`` blacklists the access token and (best-effort) revokes the
  refresh token so neither can be used again.
* ``select_tenant`` validates membership and mints a tenant-scoped access
  token; non-members get 403.

Wires the real auth router against an in-memory SQLite (with the same
PG→SQLite metadata patches the e2e suite uses) plus fakeredis. Mirrors
the fixture pattern in ``test_auth_basic.py`` so the two suites share no
state and can be run independently.
"""

from __future__ import annotations

# conftest.py at tests/ sets the required env vars. We override ``DATA_DIR``
# locally so each test process gets a writable scratch dir.
import os  # noqa: E402

os.environ.setdefault("DATA_DIR", "/tmp/rag_auth_session_test")

import uuid  # noqa: E402

import fakeredis.aioredis  # noqa: E402
import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB, UUID  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402
from sqlalchemy.schema import ColumnDefault  # noqa: E402


# ---------------------------------------------------------------------------
# PG → SQLite schema patches (mirrors tests/api/test_auth_basic.py)
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
# Per-test app + DB factory
# ---------------------------------------------------------------------------


@pytest.fixture
async def app_and_session():
    """Build a fresh app+SQLite per test, return ``(client, SessionLocal)``."""
    from rag_service.db.base import Base
    from rag_service.db import models  # noqa: F401 — registers tables

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    from rag_service.api.deps import get_db_session, get_redis
    from rag_service.api.routers.auth import router as auth_router

    async def _db_override():
        async with SessionLocal() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    fake_redis = fakeredis.aioredis.FakeRedis()

    async def _redis_override():
        return fake_redis

    app = FastAPI()
    app.include_router(auth_router)
    app.dependency_overrides[get_db_session] = _db_override
    app.dependency_overrides[get_redis] = _redis_override

    # ``with TestClient(app)`` keeps a single anyio portal (== single event
    # loop) alive across all requests in the test. Without this, each
    # ``client.post()`` spins up a fresh portal, and the fakeredis queue —
    # bound to the first portal — refuses subsequent reads with
    # ``RuntimeError: Queue is bound to a different event loop``.
    with TestClient(app) as client:
        try:
            yield client, SessionLocal
        finally:
            await engine.dispose()
            await fake_redis.aclose()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _signup(client: TestClient, email: str = "alice@example.com") -> dict:
    """Sign up a fresh user and return the response body."""
    r = client.post(
        "/v1/auth/signup",
        json={"email": email, "password": "hunter2hunter2", "display_name": "A"},
    )
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# refresh
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_valid_returns_new_access(app_and_session):
    """A valid refresh token mints a working new access token."""
    client, _ = app_and_session
    body = _signup(client)
    refresh_tok = body["refresh_token"]

    r = client.post("/v1/auth/refresh", json={"refresh_token": refresh_tok})
    assert r.status_code == 200, r.text
    new_access = r.json()["access_token"]
    assert new_access
    # The new access token actually authenticates against /me.
    me = client.get("/v1/auth/me", headers={"Authorization": f"Bearer {new_access}"})
    assert me.status_code == 200


@pytest.mark.asyncio
async def test_refresh_revoked_jti_rejected(app_and_session):
    """A refresh token whose jti has been revoked is rejected with 401."""
    client, _ = app_and_session
    body = _signup(client)
    refresh_tok = body["refresh_token"]

    # Revoke the refresh token via /logout with X-Refresh-Token, then try to use it.
    out = client.post(
        "/v1/auth/logout",
        headers={"X-Refresh-Token": refresh_tok},
    )
    assert out.status_code == 204

    r = client.post("/v1/auth/refresh", json={"refresh_token": refresh_tok})
    assert r.status_code == 401
    assert "revoked" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_refresh_invalid_token_401(app_and_session):
    """Garbage refresh token → 401 with the invalid-token message."""
    client, _ = app_and_session
    r = client.post(
        "/v1/auth/refresh", json={"refresh_token": "not-a-valid-jwt"}
    )
    assert r.status_code == 401
    assert "invalid" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_refresh_wrong_type_401(app_and_session):
    """Passing an *access* token to /refresh is rejected with 401."""
    client, _ = app_and_session
    body = _signup(client)
    access_tok = body["access_token"]

    r = client.post("/v1/auth/refresh", json={"refresh_token": access_tok})
    assert r.status_code == 401
    assert "wrong token type" in r.json()["detail"].lower()


# ---------------------------------------------------------------------------
# logout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_logout_blacklists_access(app_and_session):
    """After logout, the same access token can no longer hit /me."""
    client, _ = app_and_session
    body = _signup(client)
    access_tok = body["access_token"]

    # Sanity: the access token works before logout.
    pre = client.get("/v1/auth/me", headers={"Authorization": f"Bearer {access_tok}"})
    assert pre.status_code == 200

    out = client.post(
        "/v1/auth/logout",
        headers={"Authorization": f"Bearer {access_tok}"},
    )
    assert out.status_code == 204

    post = client.get(
        "/v1/auth/me", headers={"Authorization": f"Bearer {access_tok}"}
    )
    assert post.status_code == 401
    assert "revoked" in post.json()["detail"].lower()


@pytest.mark.asyncio
async def test_logout_revokes_refresh(app_and_session):
    """Passing X-Refresh-Token to /logout revokes that refresh token."""
    client, _ = app_and_session
    body = _signup(client)
    refresh_tok = body["refresh_token"]

    out = client.post(
        "/v1/auth/logout",
        headers={"X-Refresh-Token": refresh_tok},
    )
    assert out.status_code == 204

    r = client.post("/v1/auth/refresh", json={"refresh_token": refresh_tok})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# select_tenant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_select_tenant_member_succeeds(app_and_session):
    """Switching to a tenant the caller belongs to mints a tenant-scoped token."""
    client, _ = app_and_session
    body = _signup(client)
    access_tok = body["access_token"]
    tenant_id = body["tenants"][0]["tenant_id"]

    r = client.post(
        "/v1/auth/select_tenant",
        json={"tenant_id": tenant_id},
        headers={"Authorization": f"Bearer {access_tok}"},
    )
    assert r.status_code == 200, r.text
    rb = r.json()
    assert rb["tenant_id"] == tenant_id
    assert rb["access_token"]

    # The new token decodes to a `tenant` claim matching the requested id.
    from rag_service.auth.jwt import decode_token

    claims = decode_token(rb["access_token"])
    assert claims["tenant"] == tenant_id
    assert claims["type"] == "access"


@pytest.mark.asyncio
async def test_select_tenant_non_member_403(app_and_session):
    """Switching to a tenant the caller doesn't belong to → 403."""
    client, SessionLocal = app_and_session
    body = _signup(client)
    access_tok = body["access_token"]

    # Create a tenant the caller is *not* a member of.
    from rag_service.db.models import Tenant

    foreign_tenant_id = "u-foreigntenant"
    async with SessionLocal() as s:
        s.add(Tenant(tenant_id=foreign_tenant_id, display_name="foreign"))
        await s.commit()

    r = client.post(
        "/v1/auth/select_tenant",
        json={"tenant_id": foreign_tenant_id},
        headers={"Authorization": f"Bearer {access_tok}"},
    )
    assert r.status_code == 403
    assert "not a member" in r.json()["detail"].lower()
