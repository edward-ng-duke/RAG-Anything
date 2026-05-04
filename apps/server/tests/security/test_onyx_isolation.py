"""Security regression tests for the ``/v1/onyx/*`` surface.

Plan task 4.2 [S] — exercises the load-bearing isolation, header-shape, and
information-leak invariants the onyx integration relies on. Each test is
written so a regression in one of those invariants would flip its assertion;
the aim is for this file alone to catch the most likely "info-leak /
auth-bypass" classes of mistake during refactors.

Coverage:

* Cross-KB strong access — a token authorised for KB-A asking for an
  object in KB-B must surface as a 404 (cloak), never the row.
* Path-traversal / oversized ``X-Onyx-KB-Id`` headers — must be 404'd by
  ``validate_tenant_id`` *before* any DB lookup or filesystem touch.
* Empty / oversized ``X-Onyx-User-Id`` and ``X-Onyx-KB-Id`` headers —
  the auth dep enforces a 400 (or 404 for kb-id shape) before routes run.
* ``INTERNAL_TOKEN`` value never appears in either captured logs or the
  generated OpenAPI schema (``Authorization`` security-scheme metadata is
  fine; the literal token bytes are not).
* Both the live ``settings.internal_token`` AND any ``internal_tokens_legacy``
  entry independently authenticate a request.
* A failed-auth response body never echoes the rejected token back to the
  caller (so a logging proxy sniffing 401s can't harvest used tokens).
"""

from __future__ import annotations

# conftest.py at tests/ already populated the env vars. Override DATA_DIR
# locally so a stray Path resolution doesn't pollute another test's dir.
import os  # noqa: E402

os.environ.setdefault("DATA_DIR", "/tmp/rag_onyx_security_test")

import io  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import uuid  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

import pytest  # noqa: E402
import structlog  # noqa: E402
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
# Token + headers — every test pins the same well-known live token unless
# it explicitly overrides one of the three settings attributes.
# ---------------------------------------------------------------------------


_TOKEN = "a" * 96


@pytest.fixture(autouse=True)
def _auth_setup(monkeypatch, tmp_path):
    """Pin a known internal_token, no legacy tokens, no CIDR allowlist."""
    monkeypatch.setattr("rag_service.config.settings.internal_token", _TOKEN)
    monkeypatch.setattr(
        "rag_service.config.settings.internal_tokens_legacy", []
    )
    monkeypatch.setattr(
        "rag_service.config.settings.onyx_backend_allowed_cidrs", []
    )
    monkeypatch.setattr("rag_service.config.settings.data_dir", tmp_path)


def _headers(kb_id: str | None = None, *, token: str = _TOKEN) -> dict[str, str]:
    """Standard auth headers, optionally with ``X-Onyx-KB-Id`` set."""
    h = {
        "Authorization": f"Bearer {token}",
        "X-Onyx-User-Id": "u_test",
    }
    if kb_id is not None:
        h["X-Onyx-KB-Id"] = kb_id
    return h


# ---------------------------------------------------------------------------
# Per-test SQLite engine + helpers
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
    """Insert a ``source=onyx`` Tenant row and return its kb_id."""
    from rag_service.db.models import Tenant

    kb_id = f"onyx-{uuid.uuid4()}"
    async with session_maker() as s:
        s.add(
            Tenant(
                tenant_id=kb_id,
                display_name=f"KB {suffix or 'sec'}",
                storage_quota_mb=1024,
                config_json={"source": "onyx"},
            )
        )
        await s.commit()
    return kb_id


async def _seed_doc(session_maker, kb_id: str) -> uuid.UUID:
    """Insert one Document into ``kb_id`` and return its document_id."""
    from rag_service.db.models import Document

    doc_id = uuid.uuid4()
    async with session_maker() as s:
        s.add(
            Document(
                document_id=doc_id,
                tenant_id=kb_id,
                file_name="secret.pdf",
                file_size=42,
                content_hash=uuid.uuid4().hex,
                mime_type="application/pdf",
                storage_path=f"/tmp/{doc_id}.pdf",
                status="indexed",
            )
        )
        await s.commit()
    return doc_id


# ---------------------------------------------------------------------------
# App builders — mirror the patterns used by tests/api/test_onyx_*.py.
# ---------------------------------------------------------------------------


def _build_documents_app(session_maker) -> FastAPI:
    """Mount onyx_documents router only — used for cross-KB doc tests."""
    from rag_service.api.deps import get_db_session
    from rag_service.api.routers import onyx_documents as onyx_docs_mod

    app = FastAPI()
    app.include_router(onyx_docs_mod.router)

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


def _build_kg_app(session_maker) -> FastAPI:
    """Mount onyx_kg router only — used for cross-KB KG entity tests."""
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


def _build_kb_app(session_maker) -> FastAPI:
    """Mount onyx_kb router only — used for legacy/current token check."""
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
# 1) Cross-KB strong access — KB-A token + KB-B kb_id ⇒ 404 (cloak)
# ===========================================================================


async def test_cross_kb_query_returns_404_not_leakage(session_maker):
    """A doc owned by KB-A is invisible when calling with X-Onyx-KB-Id=KB-B.

    "Strong access": we hold the live ``INTERNAL_TOKEN`` (so the bearer
    check passes) and we send a real, source=onyx KB header (KB-B). The
    request *would* have succeeded structurally — the only thing keeping
    KB-A's row out of the response is the per-row WHERE clause that
    matches ``Document.tenant_id`` to the auth context's kb_id. A
    regression that drops that filter would 200 the row; we assert 404.

    Symmetric positive case: the same fetch with the rightful KB-A
    header *does* return 200, proving the row exists and the test isn't
    accidentally passing because of a missing fixture.
    """
    kb_a = await _seed_kb(session_maker, suffix="A")
    kb_b = await _seed_kb(session_maker, suffix="B")
    doc_in_a = await _seed_doc(session_maker, kb_a)

    app = _build_documents_app(session_maker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        # Cross-tenant read — must cloak as 404.
        r = await ac.get(
            f"/v1/onyx/documents/{doc_in_a}",
            headers=_headers(kb_b),
        )
        assert r.status_code == 404, r.text

        # Same call with the rightful KB header — should succeed.
        r = await ac.get(
            f"/v1/onyx/documents/{doc_in_a}",
            headers=_headers(kb_a),
        )
        assert r.status_code == 200, r.text
        assert r.json()["document_id"] == str(doc_in_a)


async def test_cross_kb_kg_returns_404_or_empty(session_maker, monkeypatch):
    """Cross-tenant KG entity reads must collapse to 404, never leak the row.

    The KG router calls ``rag_service.kg.repository.get_entity(db, kb_id,
    entity_id)``. When kb_id ≠ the row's true owner, the repo's WHERE
    clause should miss and return ``None`` — which the router maps to
    404. We mock the repo to return ``None`` whenever it's called with
    KB-B (the wrong tenant) and a non-empty entity dict for KB-A; both
    branches assert the surface behavior.
    """
    kb_a = await _seed_kb(session_maker, suffix="A")
    kb_b = await _seed_kb(session_maker, suffix="B")
    entity_id = "ent-secret"

    async def _fake_get_entity(db, tenant_id, entity_id_arg):  # noqa: ARG001
        if tenant_id == kb_a and entity_id_arg == entity_id:
            return {
                "id": entity_id,
                "entity_name": "TopSecret",
                "content": "classified",
                "file_path": "vault.pdf",
            }
        return None

    monkeypatch.setattr(
        "rag_service.kg.repository.get_entity", AsyncMock(side_effect=_fake_get_entity)
    )

    app = _build_kg_app(session_maker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        # Wrong tenant — cloak.
        r = await ac.get(
            f"/v1/onyx/kg/entities/{entity_id}", headers=_headers(kb_b)
        )
        assert r.status_code == 404, r.text
        # The classified content must NOT appear in the response.
        assert "classified" not in r.text
        assert "TopSecret" not in r.text

        # Rightful tenant — full payload.
        r = await ac.get(
            f"/v1/onyx/kg/entities/{entity_id}", headers=_headers(kb_a)
        )
        assert r.status_code == 200, r.text
        assert r.json()["id"] == entity_id


# ===========================================================================
# 2) Path-traversal / oversized / empty kb_id headers
# ===========================================================================


async def test_kb_id_path_traversal_returns_404(session_maker, monkeypatch):
    """``X-Onyx-KB-Id: ../../etc/passwd`` is regex-rejected → 404 cloak.

    ``validate_tenant_id`` allows only ``[a-zA-Z0-9_-]{1,64}``. The slash
    and dot characters in the traversal payload trip the pattern *before*
    the auth dep ever consults the DB or touches the filesystem. To prove
    the FS isn't touched we monkeypatch ``rag_service.core.paths`` so any
    use of the path helpers would raise loudly — the test passing means
    nothing in the auth path called them.
    """
    # Wire a tripwire: if anything in the auth path tries to build a
    # filesystem path from this kb_id we want the test to fail loudly.
    from rag_service.core import paths as paths_mod

    def _tripwire(*args, **kwargs):  # noqa: ARG001
        raise AssertionError("filesystem path helper called for invalid kb_id")

    monkeypatch.setattr(paths_mod, "tenant_upload_dir", _tripwire)

    app = _build_documents_app(session_maker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/v1/onyx/documents",
            headers=_headers("../../etc/passwd"),
        )
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "kb not found"


async def test_kb_id_long_string_returns_404(session_maker):
    """A 65-char kb_id (one past the 64-char ceiling) → 404."""
    app = _build_documents_app(session_maker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/v1/onyx/documents",
            headers=_headers("a" * 65),
        )
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "kb not found"


async def test_kb_id_empty_string_returns_400(session_maker):
    """An empty ``X-Onyx-KB-Id`` is treated as missing → 400 (header required)."""
    app = _build_documents_app(session_maker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/v1/onyx/documents",
            headers=_headers(""),
        )
    assert r.status_code == 400, r.text
    assert "X-Onyx-KB-Id" in r.json()["detail"]


# ===========================================================================
# 3) INTERNAL_TOKEN never appears in logs
# ===========================================================================


async def test_internal_token_not_in_logs(session_maker, monkeypatch):
    """The literal ``INTERNAL_TOKEN`` value is not echoed into structured logs.

    Configures structlog with a JSON renderer pointed at an in-memory
    buffer, fires both an authenticated and an unauthenticated request
    against the documents router, and asserts the captured log output
    contains *no* substring of the live token. A regression that
    accidentally logs ``request.headers`` verbatim — or echoes the
    rejected bearer in an error log — would fail this test.
    """
    from rag_service.observability.logging import (
        configure_logging,
        onyx_user_id_var,
        request_id_var,
        tenant_id_var,
    )

    # Reset contextvars (otherwise they leak across tests).
    onyx_user_id_var.set(None)
    request_id_var.set(None)
    tenant_id_var.set(None)

    # Wire a fresh stdlib root handler that funnels into a buffer; structlog
    # ultimately calls into stdlib so this catches *every* record.
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    prev_handlers = list(root.handlers)
    prev_level = root.level
    root.handlers = [handler]
    root.setLevel(logging.DEBUG)
    try:
        configure_logging(json=True, level="DEBUG")
        # Hand-fire one log statement at the auth boundary so we can
        # confirm captures are working — and NOT containing the token.
        log = structlog.get_logger("rag_service.security_test")
        log.info("auth-attempt", route="/v1/onyx/documents", outcome="testing")

        kb_id = await _seed_kb(session_maker)
        app = _build_documents_app(session_maker)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            # Authenticated request — token must not leak even on the
            # happy-path log breadcrumbs.
            r = await ac.get(
                "/v1/onyx/documents", headers=_headers(kb_id)
            )
            assert r.status_code == 200, r.text

            # Failed-auth request with a *bogus* token — most likely
            # regression site for "I'll just log the bad token to debug
            # this". Use a recognisable sentinel string.
            bogus = "BOGUS-" + ("x" * 90)
            r = await ac.get(
                "/v1/onyx/documents",
                headers={"Authorization": f"Bearer {bogus}", "X-Onyx-KB-Id": kb_id},
            )
            assert r.status_code == 401, r.text
    finally:
        # Restore root logger state for sibling tests.
        root.handlers = prev_handlers
        root.setLevel(prev_level)

    captured = buf.getvalue()
    # 1) Live token bytes must never appear in any log record.
    assert _TOKEN not in captured, (
        "INTERNAL_TOKEN value leaked into log output:\n" + captured
    )
    # 2) The bogus rejected token must not be echoed either.
    assert "BOGUS-" not in captured, (
        "rejected token value leaked into log output:\n" + captured
    )


# ===========================================================================
# 4) INTERNAL_TOKEN never appears in OpenAPI schema
# ===========================================================================


def test_internal_token_not_in_openapi_schema(monkeypatch):
    """``app.openapi()`` must not embed the live token literal anywhere.

    A regression where someone uses the live token as an OpenAPI example
    (or accidentally hard-codes it in a Pydantic ``Field(..., example=...)``)
    would fail here. We pin a recognisable banned-value string into
    ``settings.internal_token`` so the assertion has a unique sentinel
    to grep for, then walk the full schema dump.
    """
    BANNED = "BANNED_TOKEN_VALUE_" + ("z" * 80)
    monkeypatch.setattr("rag_service.config.settings.internal_token", BANNED)
    monkeypatch.setattr(
        "rag_service.config.settings.internal_tokens_legacy", []
    )
    monkeypatch.setattr(
        "rag_service.config.settings.onyx_backend_allowed_cidrs", []
    )

    from rag_service.api import app as app_mod

    @asynccontextmanager
    async def _noop_lifespan(app):  # noqa: ARG001
        yield

    monkeypatch.setattr(app_mod, "lifespan", _noop_lifespan)
    app = app_mod.create_app()

    schema = app.openapi()
    dumped = json.dumps(schema)

    # The literal banned token must NOT appear anywhere in the schema.
    assert BANNED not in dumped, "INTERNAL_TOKEN leaked into OpenAPI schema"

    # Sanity: the schema is non-trivial (otherwise the assertion above
    # would always pass even if the route layer were broken).
    assert "openapi" in schema
    assert "paths" in schema and len(schema["paths"]) > 0


# ===========================================================================
# 5) Oversized X-Onyx-User-Id rejected
# ===========================================================================


async def test_onyx_user_id_oversized_returns_400(session_maker):
    """``X-Onyx-User-Id`` longer than 128 chars → 400 ``too long``."""
    kb_id = await _seed_kb(session_maker)
    app = _build_documents_app(session_maker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/v1/onyx/documents",
            headers={
                "Authorization": f"Bearer {_TOKEN}",
                "X-Onyx-KB-Id": kb_id,
                "X-Onyx-User-Id": "u" * 129,
            },
        )
    assert r.status_code == 400, r.text
    assert "X-Onyx-User-Id" in r.json()["detail"]


# ===========================================================================
# 6) Legacy + current tokens both authenticate
# ===========================================================================


async def test_legacy_token_works_alongside_current(monkeypatch, session_maker):
    """Both ``settings.internal_token`` and any ``internal_tokens_legacy`` entry
    independently authenticate a service-to-service call.

    Uses ``GET /v1/onyx/kb`` (the no-kb list endpoint) because all that
    needs to pass is the auth dep — no KB header / DB rows required.
    """
    current = "C" * 96
    legacy = "L" * 96
    monkeypatch.setattr(
        "rag_service.config.settings.internal_token", current
    )
    monkeypatch.setattr(
        "rag_service.config.settings.internal_tokens_legacy", [legacy]
    )

    app = _build_kb_app(session_maker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        for tok in (current, legacy):
            r = await ac.get(
                "/v1/onyx/kb",
                headers={
                    "Authorization": f"Bearer {tok}",
                    "X-Onyx-User-Id": "u_test",
                },
            )
            assert r.status_code == 200, (tok[:4] + "...", r.text)


# ===========================================================================
# 7) 401 response body never echoes the rejected token
# ===========================================================================


async def test_invalid_token_returns_401_with_no_token_value_in_response(session_maker):
    """A 401 must not echo the bad bearer back to the caller.

    A naive error handler that ``f"invalid token {token!r}"``-formats
    the rejected value would let a hostile log proxy harvest tokens
    from 401s. The current dep returns a fixed ``invalid internal token``
    detail; this test pins that contract.
    """
    bogus = "wrong-token-" + ("x" * 90)  # well-formed-looking, wrong value
    app = _build_kb_app(session_maker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/v1/onyx/kb",
            headers={
                "Authorization": f"Bearer {bogus}",
                "X-Onyx-User-Id": "u_test",
            },
        )
    assert r.status_code == 401, r.text
    body_text = r.text
    assert bogus not in body_text, (
        "rejected bearer was echoed in 401 response body: " + body_text
    )
    # And nothing that looks like a "wrong-token" prefix either.
    assert "wrong-token" not in body_text
