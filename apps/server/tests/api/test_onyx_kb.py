"""Tests for ``rag_service.api.routers.onyx_kb`` — /v1/onyx/kb CRUD.

Service-to-service KB lifecycle endpoints:

* ``POST   /v1/onyx/kb``           — create a KB (auth: no-kb dep).
* ``GET    /v1/onyx/kb``           — list KBs with optional filters / cursor.
* ``GET    /v1/onyx/kb/{kb_id}``   — fetch one KB; requires X-Onyx-KB-Id == path.
* ``DELETE /v1/onyx/kb/{kb_id}``   — cascade-delete; same path/header rule.

Each test stands up a fresh in-memory SQLite engine with the same
metadata patches the rest of the suite applies, mounts the router with a
``get_db_session`` override, and hits the resulting app via
``httpx.ASGITransport``.
"""

from __future__ import annotations

# conftest.py at tests/ already populated the env vars. Override DATA_DIR
# locally so a stray Path resolution doesn't pollute another test's dir.
import os  # noqa: E402

os.environ.setdefault("DATA_DIR", "/tmp/rag_onyx_kb_test")

import re  # noqa: E402
import uuid  # noqa: E402

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
# PG → SQLite schema patches (mirrors tests/api/test_auth_onyx.py)
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
# Settings patch — every test runs with a clean known token + empty CIDR list
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _auth_setup(monkeypatch):
    """Pin a known internal_token, no legacy tokens, no CIDR allowlist."""
    monkeypatch.setattr("rag_service.config.settings.internal_token", "a" * 96)
    monkeypatch.setattr("rag_service.config.settings.internal_tokens_legacy", [])
    monkeypatch.setattr(
        "rag_service.config.settings.onyx_backend_allowed_cidrs", []
    )


_TOKEN = "a" * 96
_BASE_HEADERS = {
    "Authorization": f"Bearer {_TOKEN}",
    "X-Onyx-User-Id": "u_test",
}


# ---------------------------------------------------------------------------
# Per-test SQLite engine
# ---------------------------------------------------------------------------


@pytest.fixture
async def session_maker():
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


def _build_app(session_maker) -> FastAPI:
    """Mount the onyx_kb router with a SQLite-backed ``get_db_session``."""
    from rag_service.api.deps import get_db_session
    from rag_service.api.routers import onyx_kb as onyx_kb_mod

    app = FastAPI()
    app.include_router(onyx_kb_mod.router)

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
# POST /v1/onyx/kb — create
# ===========================================================================


_KB_ID_RE = re.compile(r"^onyx-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


async def test_create_kb_returns_201_with_kb_id(session_maker):
    """Happy path — minimal body, default quota, kb_id matches ``onyx-<uuid4>``."""
    app = _build_app(session_maker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.post(
            "/v1/onyx/kb",
            headers=_BASE_HEADERS,
            json={"display_name": "Engineering Docs"},
        )
    assert r.status_code == 201, r.text
    body = r.json()
    assert _KB_ID_RE.match(body["kb_id"]), body["kb_id"]
    assert body["display_name"] == "Engineering Docs"
    assert body["storage_quota_mb"] == 1024
    assert body["storage_used_mb"] == 0.0
    assert body["document_count"] == 0


async def test_create_kb_with_workspace_and_owner(session_maker):
    """``onyx_workspace_id`` / ``onyx_owner_user_id`` echo back in detail."""
    app = _build_app(session_maker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.post(
            "/v1/onyx/kb",
            headers=_BASE_HEADERS,
            json={
                "display_name": "Sales KB",
                "onyx_workspace_id": "ws-123",
                "onyx_owner_user_id": "owner-abc",
            },
        )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["onyx_workspace_id"] == "ws-123"
    assert body["onyx_owner_user_id"] == "owner-abc"


async def test_create_kb_quota_clamp_min(session_maker):
    """``storage_quota_mb`` below 64 → 422."""
    app = _build_app(session_maker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.post(
            "/v1/onyx/kb",
            headers=_BASE_HEADERS,
            json={"display_name": "X", "storage_quota_mb": 10},
        )
    assert r.status_code == 422


async def test_create_kb_quota_clamp_max(session_maker):
    """``storage_quota_mb`` above 102_400 → 422."""
    app = _build_app(session_maker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.post(
            "/v1/onyx/kb",
            headers=_BASE_HEADERS,
            json={"display_name": "X", "storage_quota_mb": 999_999},
        )
    assert r.status_code == 422


async def test_create_kb_missing_token_401(session_maker):
    """No ``Authorization`` header → 401 from the auth dep."""
    app = _build_app(session_maker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.post("/v1/onyx/kb", json={"display_name": "X"})
    assert r.status_code == 401


async def test_create_kb_invalid_display_name_too_long(session_maker):
    """201-char display_name (max is 200) → 422."""
    app = _build_app(session_maker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.post(
            "/v1/onyx/kb",
            headers=_BASE_HEADERS,
            json={"display_name": "x" * 201},
        )
    assert r.status_code == 422


async def test_create_kb_invalid_display_name_empty(session_maker):
    """Empty display_name → 422 (min_length=1)."""
    app = _build_app(session_maker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.post(
            "/v1/onyx/kb",
            headers=_BASE_HEADERS,
            json={"display_name": ""},
        )
    assert r.status_code == 422


# ===========================================================================
# GET /v1/onyx/kb — list
# ===========================================================================


async def test_list_kbs_empty(session_maker):
    """No KBs in DB → 200 with empty list and null cursor."""
    app = _build_app(session_maker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get("/v1/onyx/kb", headers=_BASE_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["items"] == []
    assert body["next_cursor"] is None


async def test_list_kbs_after_create(session_maker):
    """After two creates the list returns both KB ids (any order)."""
    app = _build_app(session_maker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        ids = []
        for name in ("KB Alpha", "KB Beta"):
            r = await ac.post(
                "/v1/onyx/kb",
                headers=_BASE_HEADERS,
                json={"display_name": name},
            )
            assert r.status_code == 201, r.text
            ids.append(r.json()["kb_id"])

        r = await ac.get("/v1/onyx/kb", headers=_BASE_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    returned = sorted([i["kb_id"] for i in body["items"]])
    assert returned == sorted(ids)


async def test_list_kbs_filter_by_workspace(session_maker):
    """Filter by ``onyx_workspace_id`` — only matching KBs come back."""
    app = _build_app(session_maker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        ra = await ac.post(
            "/v1/onyx/kb",
            headers=_BASE_HEADERS,
            json={"display_name": "Workspace A KB", "onyx_workspace_id": "A"},
        )
        rb = await ac.post(
            "/v1/onyx/kb",
            headers=_BASE_HEADERS,
            json={"display_name": "Workspace B KB", "onyx_workspace_id": "B"},
        )
        assert ra.status_code == 201 and rb.status_code == 201

        r = await ac.get(
            "/v1/onyx/kb?onyx_workspace_id=A", headers=_BASE_HEADERS
        )
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["kb_id"] == ra.json()["kb_id"]


async def test_list_kbs_pagination(session_maker):
    """Seed 3 KBs with distinct ``created_at``, paginate limit=2 → 2 + cursor → 1."""
    # Direct DB seeding rather than API POSTs so we control ``created_at``
    # to be strictly monotonic — otherwise SQLite's TEXT-based datetime
    # compare can get confused by microsecond rounding when three inserts
    # complete in the same wall-clock second.
    import datetime as _dt
    from rag_service.db.models import Tenant

    base = _dt.datetime(2026, 5, 4, 12, 0, 0)
    ids = []
    async with session_maker() as s:
        for i in range(3):
            tid = f"onyx-{uuid.uuid4()}"
            ids.append(tid)
            s.add(
                Tenant(
                    tenant_id=tid,
                    display_name=f"KB-{i}",
                    storage_quota_mb=1024,
                    config_json={"source": "onyx"},
                    created_at=base + _dt.timedelta(seconds=i),
                )
            )
        await s.commit()

    app = _build_app(session_maker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get("/v1/onyx/kb?limit=2", headers=_BASE_HEADERS)
        assert r.status_code == 200, r.text
        page1 = r.json()
        assert len(page1["items"]) == 2
        assert page1["next_cursor"] is not None

        r = await ac.get(
            f"/v1/onyx/kb?limit=2&cursor={page1['next_cursor']}",
            headers=_BASE_HEADERS,
        )
        assert r.status_code == 200, r.text
        page2 = r.json()
        assert len(page2["items"]) == 1
        assert page2["next_cursor"] is None

        # All 3 must have appeared exactly once across both pages.
        seen = {i["kb_id"] for i in page1["items"]} | {
            i["kb_id"] for i in page2["items"]
        }
        assert seen == set(ids)


# ===========================================================================
# GET /v1/onyx/kb/{kb_id} — read by id
# ===========================================================================


async def test_get_kb_existing(session_maker):
    """Create, then fetch by id with matching X-Onyx-KB-Id → 200."""
    app = _build_app(session_maker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.post(
            "/v1/onyx/kb",
            headers=_BASE_HEADERS,
            json={"display_name": "Lookup Me"},
        )
        assert r.status_code == 201, r.text
        kb_id = r.json()["kb_id"]

        r = await ac.get(
            f"/v1/onyx/kb/{kb_id}",
            headers={**_BASE_HEADERS, "X-Onyx-KB-Id": kb_id},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kb_id"] == kb_id
    assert body["display_name"] == "Lookup Me"
    assert body["document_count"] == 0


async def test_get_kb_unknown_returns_404(session_maker):
    """Random well-formed kb_id with no row → 404 from auth dep."""
    app = _build_app(session_maker)
    transport = ASGITransport(app=app)
    bogus = f"onyx-{uuid.uuid4()}"
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            f"/v1/onyx/kb/{bogus}",
            headers={**_BASE_HEADERS, "X-Onyx-KB-Id": bogus},
        )
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "kb not found"


async def test_get_kb_path_header_mismatch_returns_400(session_maker):
    """Path kb_id != header X-Onyx-KB-Id → 400 (with both KBs source=onyx)."""
    app = _build_app(session_maker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        ra = await ac.post(
            "/v1/onyx/kb", headers=_BASE_HEADERS, json={"display_name": "A"}
        )
        rb = await ac.post(
            "/v1/onyx/kb", headers=_BASE_HEADERS, json={"display_name": "B"}
        )
        kb_a = ra.json()["kb_id"]
        kb_b = rb.json()["kb_id"]
        assert kb_a != kb_b

        # Hit /kb/{kb_a} but pass header X-Onyx-KB-Id=kb_b. Auth passes
        # (kb_b is a real source=onyx tenant) but the route guard fails.
        r = await ac.get(
            f"/v1/onyx/kb/{kb_a}",
            headers={**_BASE_HEADERS, "X-Onyx-KB-Id": kb_b},
        )
    assert r.status_code == 400, r.text
    assert "X-Onyx-KB-Id" in r.json()["detail"]


# ===========================================================================
# DELETE /v1/onyx/kb/{kb_id}
# ===========================================================================


async def test_delete_kb_returns_204(session_maker):
    """DELETE → 204; subsequent list shows the KB is gone."""
    app = _build_app(session_maker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.post(
            "/v1/onyx/kb", headers=_BASE_HEADERS, json={"display_name": "Doomed"}
        )
        kb_id = r.json()["kb_id"]

        r = await ac.delete(
            f"/v1/onyx/kb/{kb_id}",
            headers={**_BASE_HEADERS, "X-Onyx-KB-Id": kb_id},
        )
        assert r.status_code == 204, r.text

        r = await ac.get("/v1/onyx/kb", headers=_BASE_HEADERS)
        assert r.status_code == 200
        assert r.json()["items"] == []

    # Verify Tenant row is actually gone at the DB level too.
    from rag_service.db.models import Tenant
    from sqlalchemy import select

    async with session_maker() as s:
        row = (
            await s.execute(select(Tenant).where(Tenant.tenant_id == kb_id))
        ).scalar_one_or_none()
        assert row is None


async def test_delete_kb_unknown_returns_404(session_maker, monkeypatch):
    """``delete_kb_cascade`` returning False (race) → 404 from the route."""
    app = _build_app(session_maker)
    transport = ASGITransport(app=app)

    # Set up a real source=onyx tenant so onyx_service_auth lets us
    # through, then monkeypatch delete_kb_cascade in the router's
    # namespace to simulate a concurrent delete.
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.post(
            "/v1/onyx/kb", headers=_BASE_HEADERS, json={"display_name": "Ghost"}
        )
        kb_id = r.json()["kb_id"]

        async def _fake_delete(db, kb, *, data_dir):  # noqa: ARG001
            return False

        from rag_service.api.routers import onyx_kb as onyx_kb_mod

        monkeypatch.setattr(onyx_kb_mod, "delete_kb_cascade", _fake_delete)

        r = await ac.delete(
            f"/v1/onyx/kb/{kb_id}",
            headers={**_BASE_HEADERS, "X-Onyx-KB-Id": kb_id},
        )
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "kb not found"


async def test_delete_kb_cascade_removes_documents(session_maker):
    """A child Document row is removed when its KB is deleted."""
    app = _build_app(session_maker)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.post(
            "/v1/onyx/kb",
            headers=_BASE_HEADERS,
            json={"display_name": "Has Docs"},
        )
        kb_id = r.json()["kb_id"]

    # Insert a Document row directly so we don't depend on any other router.
    from rag_service.db.models import Document
    from sqlalchemy import select

    async with session_maker() as s:
        s.add(
            Document(
                document_id=uuid.uuid4(),
                tenant_id=kb_id,
                file_name="x.pdf",
                file_size=1024,
                content_hash=uuid.uuid4().hex,
                mime_type="application/pdf",
                storage_path="/tmp/x.pdf",
                status="indexed",
            )
        )
        await s.commit()

    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.delete(
            f"/v1/onyx/kb/{kb_id}",
            headers={**_BASE_HEADERS, "X-Onyx-KB-Id": kb_id},
        )
        assert r.status_code == 204, r.text

    async with session_maker() as s:
        rows = (
            await s.execute(select(Document).where(Document.tenant_id == kb_id))
        ).scalars().all()
        assert rows == []
