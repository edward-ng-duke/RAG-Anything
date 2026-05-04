"""Tests for ``rag_service.api.routers.auth`` — signup / login / me.

Wires the real ``auth`` router against an in-memory SQLite (with the same
PG→SQLite metadata patches the e2e suite uses) plus fakeredis. The auth
flow round-trips real bcrypt hashing, real JWT signing, and real DB rows
— everything that happens in production except the storage backend.

What's covered:

* ``signup`` provisions a user + owner-role personal tenant and returns
  well-formed tokens.
* duplicate email collapses to 409.
* short password collapses to 400.
* ``login`` succeeds for the credentials that just signed up.
* wrong password and unknown email both collapse to a generic 401.
* ``me`` rejects unauth'd requests.
* ``me`` returns the same user + tenants that ``login`` reported.
"""

from __future__ import annotations

# conftest.py at tests/ sets the required env vars. We override ``DATA_DIR``
# locally so each test process gets a writable scratch dir.
import os  # noqa: E402

os.environ.setdefault("DATA_DIR", "/tmp/rag_auth_api_test")

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
# PG → SQLite schema patches (mirrors tests/e2e/test_ingest_query.py)
# ---------------------------------------------------------------------------
#
# ``rag_service.db.models`` uses Postgres-only types (UUID, JSONB) and
# server defaults (``gen_random_uuid()``, ``'{}'::jsonb``, ``Identity``).
# To stand them up on SQLite we (1) compile the dialect-specific types
# down to JSON / CHAR(36); (2) replace PG server defaults with Python
# ``ColumnDefault``s; (3) drop ``Identity`` so SQLite's autoincrement
# applies. Patches are idempotent — the e2e module may have already run
# them earlier in the same pytest process, in which case re-applying is a
# no-op.


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
async def app_and_session(monkeypatch):
    """Build a fresh app+SQLite per test, return ``(client, SessionLocal)``.

    We import inside the fixture so the metadata patch above is applied
    before SQLAlchemy compiles any DDL. Each test gets its own engine so
    fixture state never leaks between cases.
    """
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

    client = TestClient(app)
    try:
        yield client, SessionLocal
    finally:
        await engine.dispose()
        await fake_redis.aclose()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _signup(client: TestClient, email: str, password: str, **extra) -> dict:
    payload = {"email": email, "password": password, **extra}
    return client.post("/v1/auth/signup", json=payload).json()


# ---------------------------------------------------------------------------
# signup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signup_creates_user_and_tenant(app_and_session):
    """Fresh signup persists user + owner-role personal tenant + tokens."""
    from sqlalchemy import select

    from rag_service.db.models import Membership, Tenant, User

    client, SessionLocal = app_and_session
    r = client.post(
        "/v1/auth/signup",
        json={
            "email": "alice@example.com",
            "password": "hunter2hunter2",
            "display_name": "Alice",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()

    # Tokens look well-formed; user payload is the public projection.
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["user"]["email"] == "alice@example.com"
    assert body["user"]["display_name"] == "Alice"
    uuid.UUID(body["user"]["user_id"])
    # password_hash MUST NOT leak into the response.
    assert "password_hash" not in body["user"]

    # One tenant, owner role.
    assert len(body["tenants"]) == 1
    t = body["tenants"][0]
    assert t["role"] == "owner"
    assert t["display_name"] == "Alice"
    assert t["tenant_id"].startswith("u-")
    assert len(t["tenant_id"]) == 14  # "u-" + 12 hex

    # DB state matches — user, tenant, membership all present.
    async with SessionLocal() as s:
        users = (await s.execute(select(User))).scalars().all()
        assert len(users) == 1
        assert users[0].email == "alice@example.com"
        # Hash isn't the plaintext.
        assert users[0].password_hash != "hunter2hunter2"

        tenants = (await s.execute(select(Tenant))).scalars().all()
        assert len(tenants) == 1
        assert tenants[0].tenant_id == t["tenant_id"]

        memberships = (await s.execute(select(Membership))).scalars().all()
        assert len(memberships) == 1
        assert memberships[0].role == "owner"


@pytest.mark.asyncio
async def test_signup_duplicate_email_409(app_and_session):
    """Second signup with the same email collapses to 409."""
    client, _ = app_and_session
    r1 = client.post(
        "/v1/auth/signup",
        json={"email": "bob@example.com", "password": "supersecret1"},
    )
    assert r1.status_code == 201, r1.text

    r2 = client.post(
        "/v1/auth/signup",
        json={"email": "bob@example.com", "password": "supersecret1"},
    )
    assert r2.status_code == 409
    assert r2.json()["detail"] == "email already registered"


@pytest.mark.asyncio
async def test_signup_short_password_400(app_and_session):
    """Password < 8 chars is rejected up-front with 400."""
    client, _ = app_and_session
    r = client.post(
        "/v1/auth/signup",
        json={"email": "charlie@example.com", "password": "short"},
    )
    assert r.status_code == 400
    assert "password" in r.json()["detail"].lower()


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_correct_credentials(app_and_session):
    """Correct creds → 200 with tokens + tenants list matching signup."""
    client, _ = app_and_session
    su = client.post(
        "/v1/auth/signup",
        json={"email": "dave@example.com", "password": "hunter2hunter2"},
    )
    assert su.status_code == 201
    su_body = su.json()

    r = client.post(
        "/v1/auth/login",
        json={"email": "dave@example.com", "password": "hunter2hunter2"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["access_token"]
    assert body["refresh_token"]
    # Same user_id; tokens are freshly minted so don't compare.
    assert body["user"]["user_id"] == su_body["user"]["user_id"]
    assert len(body["tenants"]) == 1
    assert body["tenants"][0]["role"] == "owner"


@pytest.mark.asyncio
async def test_login_wrong_password_401(app_and_session):
    """Wrong password → 401 with the generic ``invalid credentials``."""
    client, _ = app_and_session
    client.post(
        "/v1/auth/signup",
        json={"email": "eve@example.com", "password": "hunter2hunter2"},
    )

    r = client.post(
        "/v1/auth/login",
        json={"email": "eve@example.com", "password": "WRONG-password"},
    )
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid credentials"


@pytest.mark.asyncio
async def test_login_unknown_user_401(app_and_session):
    """Unknown email collapses to the same 401 message as wrong password."""
    client, _ = app_and_session
    r = client.post(
        "/v1/auth/login",
        json={"email": "ghost@example.com", "password": "hunter2hunter2"},
    )
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid credentials"


# ---------------------------------------------------------------------------
# me
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_me_requires_auth(app_and_session):
    """No bearer token → 401."""
    client, _ = app_and_session
    r = client.get("/v1/auth/me")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_me_returns_user_and_tenants(app_and_session):
    """``me`` round-trips the access token issued at signup."""
    client, _ = app_and_session
    su = client.post(
        "/v1/auth/signup",
        json={
            "email": "frank@example.com",
            "password": "hunter2hunter2",
            "display_name": "Frank",
        },
    )
    assert su.status_code == 201
    token = su.json()["access_token"]

    r = client.get(
        "/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"]["email"] == "frank@example.com"
    assert body["user"]["display_name"] == "Frank"
    assert len(body["tenants"]) == 1
    assert body["tenants"][0]["role"] == "owner"
    assert body["tenants"][0]["tenant_id"].startswith("u-")
