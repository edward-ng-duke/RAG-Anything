"""Tests for ``rag_service.api.auth_onyx`` — internal-token + IP + KB deps.

Two FastAPI dependencies are exercised:

* ``onyx_service_auth_no_kb`` — for create/list endpoints. Validates the
  Bearer ``INTERNAL_TOKEN`` (current + legacy via ``hmac.compare_digest``)
  and the optional CIDR allowlist; returns an :class:`OnyxCallContext`
  with ``kb_id=None`` regardless of any ``X-Onyx-KB-Id`` header.
* ``onyx_service_auth`` — same checks, plus validates ``X-Onyx-KB-Id``
  maps to a ``tenants`` row whose ``config_json.source == "onyx"``;
  unknown KBs and non-onyx tenants both surface as 404.

Tests use an inline ``FastAPI`` app with one ``/probe`` endpoint per
case and hit it via ``httpx.ASGITransport``. KB tests stand up a small
SQLite session with the same metadata patches the rest of the suite
applies, override ``get_db_session``, and seed ``Tenant`` rows.
"""

from __future__ import annotations

# conftest.py at tests/ already populated the env vars. Override DATA_DIR
# locally so a stray Path resolution doesn't pollute another test's dir.
import os  # noqa: E402

os.environ.setdefault("DATA_DIR", "/tmp/rag_auth_onyx_test")

import re  # noqa: E402
import uuid  # noqa: E402

import fakeredis.aioredis  # noqa: E402  # noqa: F401 — keeps import parity w/ other tests
import pytest  # noqa: E402
from fastapi import Depends, FastAPI  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB, UUID  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    async_sessionmaker,
    create_async_engine,
)
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
# Helpers — token rigging
# ---------------------------------------------------------------------------


# Both deps consult ``rag_service.config.settings`` for the live token,
# the legacy token list, and the CIDR allowlist. Tests poke those three
# attrs via ``monkeypatch.setattr`` — the ``settings`` singleton is
# constructed lazily by :func:`rag_service.config.__getattr__`, so we
# touch it once here to make sure the instance exists before patching.
from rag_service import config as _config_mod  # noqa: E402

_ = _config_mod.settings  # force lazy init


def _set_tokens(
    monkeypatch: pytest.MonkeyPatch,
    *,
    current: str = "a" * 96,
    legacy: list[str] | None = None,
    cidrs: list[str] | None = None,
) -> str:
    """Patch settings token / legacy / CIDR in one call. Returns the current token."""
    monkeypatch.setattr(_config_mod.settings, "internal_token", current)
    monkeypatch.setattr(
        _config_mod.settings, "internal_tokens_legacy", list(legacy or [])
    )
    monkeypatch.setattr(
        _config_mod.settings, "onyx_backend_allowed_cidrs", list(cidrs or [])
    )
    return current


# ---------------------------------------------------------------------------
# Helpers — minimal app builders
# ---------------------------------------------------------------------------


def _build_no_kb_app() -> FastAPI:
    """One-route app whose endpoint runs the no-kb dep and echoes ctx."""
    from rag_service.api.auth_onyx import (
        OnyxCallContext,
        onyx_service_auth_no_kb,
    )

    app = FastAPI()

    @app.get("/probe")
    async def probe(  # noqa: ANN202
        ctx: OnyxCallContext = Depends(onyx_service_auth_no_kb),
    ):
        return {
            "kb_id": ctx.kb_id,
            "onyx_user_id": ctx.onyx_user_id,
            "request_id": ctx.request_id,
            "caller_ip": ctx.caller_ip,
        }

    return app


def _build_kb_app(session_maker) -> FastAPI:
    """One-route app whose endpoint runs the kb-validating dep + echoes ctx."""
    from rag_service.api.auth_onyx import OnyxCallContext, onyx_service_auth
    from rag_service.api.deps import get_db_session

    app = FastAPI()

    async def _db_override():
        async with session_maker() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    app.dependency_overrides[get_db_session] = _db_override

    @app.get("/probe")
    async def probe(  # noqa: ANN202
        ctx: OnyxCallContext = Depends(onyx_service_auth),
    ):
        return {
            "kb_id": ctx.kb_id,
            "onyx_user_id": ctx.onyx_user_id,
            "request_id": ctx.request_id,
            "caller_ip": ctx.caller_ip,
        }

    return app


# ---------------------------------------------------------------------------
# Per-test SQLite engine for KB tests
# ---------------------------------------------------------------------------


@pytest.fixture
async def session_maker():
    """Stand up a fresh in-memory SQLite + return its session maker."""
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


async def _seed_tenant(session_maker, *, tenant_id: str, config_json: dict | None) -> None:
    """Insert one ``Tenant`` row with the given ``config_json`` blob."""
    from rag_service.db.models import Tenant

    async with session_maker() as s:
        s.add(
            Tenant(
                tenant_id=tenant_id,
                display_name=tenant_id,
                config_json=config_json,
                storage_quota_mb=1024,
            )
        )
        await s.commit()


# ===========================================================================
# onyx_service_auth_no_kb — bearer / token / request-id / user-id
# ===========================================================================


async def test_no_kb_dep_missing_authorization_returns_401(monkeypatch):
    """No ``Authorization`` header → 401 with ``missing internal token``."""
    _set_tokens(monkeypatch)
    transport = ASGITransport(app=_build_no_kb_app())
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get("/probe")
    assert r.status_code == 401
    assert r.json()["detail"] == "missing internal token"


async def test_no_kb_dep_wrong_scheme_returns_401(monkeypatch):
    """``Authorization: Basic ...`` doesn't satisfy the Bearer-only check."""
    _set_tokens(monkeypatch)
    transport = ASGITransport(app=_build_no_kb_app())
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get("/probe", headers={"Authorization": "Basic xyz"})
    assert r.status_code == 401
    assert r.json()["detail"] == "missing internal token"


async def test_no_kb_dep_invalid_token_returns_401(monkeypatch):
    """A well-formed Bearer header with wrong token → 401 ``invalid``."""
    _set_tokens(monkeypatch, current="a" * 96)
    transport = ASGITransport(app=_build_no_kb_app())
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/probe", headers={"Authorization": f"Bearer {'b' * 96}"}
        )
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid internal token"


async def test_no_kb_dep_valid_token_passes(monkeypatch):
    """Live token round-trips and produces a sane ``OnyxCallContext``."""
    tok = _set_tokens(monkeypatch, current="a" * 96)
    transport = ASGITransport(app=_build_no_kb_app())
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/probe", headers={"Authorization": f"Bearer {tok}"}
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kb_id"] is None
    assert body["onyx_user_id"] is None
    assert isinstance(body["request_id"], str) and len(body["request_id"]) == 32


async def test_no_kb_dep_legacy_token_passes(monkeypatch):
    """A token in the legacy list still authenticates."""
    legacy = "c" * 96
    _set_tokens(monkeypatch, current="a" * 96, legacy=[legacy])
    transport = ASGITransport(app=_build_no_kb_app())
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/probe", headers={"Authorization": f"Bearer {legacy}"}
        )
    assert r.status_code == 200, r.text


async def test_no_kb_dep_legacy_and_current_both_work_independently(monkeypatch):
    """Both the live token AND any legacy token authenticate."""
    current = "a" * 96
    legacy_a = "c" * 96
    legacy_b = "d" * 96
    _set_tokens(monkeypatch, current=current, legacy=[legacy_a, legacy_b])
    transport = ASGITransport(app=_build_no_kb_app())
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        for tok in (current, legacy_a, legacy_b):
            r = await ac.get(
                "/probe", headers={"Authorization": f"Bearer {tok}"}
            )
            assert r.status_code == 200, (tok, r.text)


async def test_no_kb_dep_request_id_generated_when_absent(monkeypatch):
    """Missing ``X-Request-Id`` → ctx carries a fresh uuid4 hex (32 lower-hex)."""
    tok = _set_tokens(monkeypatch)
    transport = ASGITransport(app=_build_no_kb_app())
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/probe", headers={"Authorization": f"Bearer {tok}"}
        )
    assert r.status_code == 200, r.text
    rid = r.json()["request_id"]
    assert re.fullmatch(r"[0-9a-f]{32}", rid), rid


async def test_no_kb_dep_request_id_passed_through_when_present(monkeypatch):
    """Caller-supplied ``X-Request-Id`` is preserved verbatim in ctx."""
    tok = _set_tokens(monkeypatch)
    transport = ASGITransport(app=_build_no_kb_app())
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/probe",
            headers={
                "Authorization": f"Bearer {tok}",
                "X-Request-Id": "deadbeef-1234",
            },
        )
    assert r.status_code == 200, r.text
    assert r.json()["request_id"] == "deadbeef-1234"


async def test_no_kb_dep_user_id_too_long_returns_400(monkeypatch):
    """``X-Onyx-User-Id`` longer than 128 chars → 400 ``too long``."""
    tok = _set_tokens(monkeypatch)
    transport = ASGITransport(app=_build_no_kb_app())
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/probe",
            headers={
                "Authorization": f"Bearer {tok}",
                "X-Onyx-User-Id": "u" * 129,
            },
        )
    assert r.status_code == 400
    assert r.json()["detail"] == "X-Onyx-User-Id too long"


async def test_no_kb_dep_no_kb_header_does_not_validate(monkeypatch):
    """``X-Onyx-KB-Id`` is ignored on the no-kb dep — ctx.kb_id stays ``None``."""
    tok = _set_tokens(monkeypatch)
    transport = ASGITransport(app=_build_no_kb_app())
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/probe",
            headers={
                "Authorization": f"Bearer {tok}",
                "X-Onyx-KB-Id": "totally-bogus-kb-id-not-in-db",
            },
        )
    assert r.status_code == 200, r.text
    assert r.json()["kb_id"] is None


# ===========================================================================
# onyx_service_auth — KB existence + source filter
# ===========================================================================


async def test_kb_dep_missing_kb_header_returns_400(monkeypatch, session_maker):
    """No ``X-Onyx-KB-Id`` → 400 demanding the header."""
    tok = _set_tokens(monkeypatch)
    transport = ASGITransport(app=_build_kb_app(session_maker))
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/probe", headers={"Authorization": f"Bearer {tok}"}
        )
    assert r.status_code == 400
    assert r.json()["detail"] == "X-Onyx-KB-Id header required"


async def test_kb_dep_invalid_kb_id_pattern_returns_404(monkeypatch, session_maker):
    """A KB id that fails the ``validate_tenant_id`` pattern → 404 (cloaked)."""
    tok = _set_tokens(monkeypatch)
    transport = ASGITransport(app=_build_kb_app(session_maker))
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/probe",
            headers={
                "Authorization": f"Bearer {tok}",
                "X-Onyx-KB-Id": "../../etc",
            },
        )
    assert r.status_code == 404
    assert r.json()["detail"] == "kb not found"


async def test_kb_dep_unknown_kb_returns_404(monkeypatch, session_maker):
    """KB id is well-formed but no matching ``tenants`` row → 404."""
    tok = _set_tokens(monkeypatch)
    transport = ASGITransport(app=_build_kb_app(session_maker))
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/probe",
            headers={
                "Authorization": f"Bearer {tok}",
                "X-Onyx-KB-Id": "onyx-deadbeef1234",
            },
        )
    assert r.status_code == 404
    assert r.json()["detail"] == "kb not found"


async def test_kb_dep_kb_without_source_onyx_returns_404(monkeypatch, session_maker):
    """Tenant exists but ``config_json`` doesn't mark it onyx → 404 (cloaked)."""
    tok = _set_tokens(monkeypatch)
    await _seed_tenant(
        session_maker, tenant_id="tnt-other", config_json={"source": "other"}
    )
    await _seed_tenant(session_maker, tenant_id="tnt-empty", config_json={})

    transport = ASGITransport(app=_build_kb_app(session_maker))
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        for kb_id in ("tnt-other", "tnt-empty"):
            r = await ac.get(
                "/probe",
                headers={
                    "Authorization": f"Bearer {tok}",
                    "X-Onyx-KB-Id": kb_id,
                },
            )
            assert r.status_code == 404, (kb_id, r.text)
            assert r.json()["detail"] == "kb not found"


async def test_kb_dep_valid_onyx_kb_passes(monkeypatch, session_maker):
    """Tenant row with ``config_json.source == 'onyx'`` resolves; ctx.kb_id set."""
    tok = _set_tokens(monkeypatch)
    kb_id = "onyx-3f9b2d8a-1234-5678-9abc-def012345678"
    await _seed_tenant(
        session_maker, tenant_id=kb_id, config_json={"source": "onyx"}
    )

    transport = ASGITransport(app=_build_kb_app(session_maker))
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/probe",
            headers={
                "Authorization": f"Bearer {tok}",
                "X-Onyx-KB-Id": kb_id,
                "X-Onyx-User-Id": "alice",
            },
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kb_id"] == kb_id
    assert body["onyx_user_id"] == "alice"


# ===========================================================================
# IP allowlist
# ===========================================================================


async def test_ip_allowlist_empty_skips_check(monkeypatch):
    """Empty CIDR list → any caller IP passes."""
    tok = _set_tokens(monkeypatch, cidrs=[])
    transport = ASGITransport(app=_build_no_kb_app())
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/probe",
            headers={
                "Authorization": f"Bearer {tok}",
                "X-Forwarded-For": "8.8.8.8",
            },
        )
    assert r.status_code == 200, r.text


async def test_ip_allowlist_xff_first_hop_used_when_set(monkeypatch):
    """``X-Forwarded-For`` first hop is checked against the allowlist."""
    tok = _set_tokens(monkeypatch, cidrs=["10.0.1.0/24"])
    transport = ASGITransport(app=_build_no_kb_app())
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/probe",
            headers={
                "Authorization": f"Bearer {tok}",
                "X-Forwarded-For": "10.0.1.5, 192.168.1.1",
            },
        )
    assert r.status_code == 200, r.text
    assert r.json()["caller_ip"] == "10.0.1.5"


async def test_ip_allowlist_xff_first_hop_outside_returns_403(monkeypatch):
    """XFF first hop outside any allowlisted CIDR → 403."""
    tok = _set_tokens(monkeypatch, cidrs=["10.0.1.0/24"])
    transport = ASGITransport(app=_build_no_kb_app())
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/probe",
            headers={
                "Authorization": f"Bearer {tok}",
                "X-Forwarded-For": "8.8.8.8",
            },
        )
    assert r.status_code == 403
    assert r.json()["detail"] == "caller ip not allowed"


async def test_ip_allowlist_falls_back_to_client_host(monkeypatch):
    """Without ``X-Forwarded-For`` we fall back to ``request.client.host``."""
    tok = _set_tokens(monkeypatch, cidrs=["127.0.0.0/8"])
    # ASGITransport reports ``client = ('127.0.0.1', 123)`` by default,
    # which lives in 127.0.0.0/8 — no XFF needed for this case.
    transport = ASGITransport(app=_build_no_kb_app())
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/probe", headers={"Authorization": f"Bearer {tok}"}
        )
    assert r.status_code == 200, r.text


async def test_ip_allowlist_v6_cidr_works(monkeypatch):
    """An IPv6 caller against an IPv6 CIDR matches and is allowed."""
    tok = _set_tokens(monkeypatch, cidrs=["2001:db8::/32"])
    transport = ASGITransport(app=_build_no_kb_app())
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/probe",
            headers={
                "Authorization": f"Bearer {tok}",
                "X-Forwarded-For": "2001:db8::1",
            },
        )
    assert r.status_code == 200, r.text
    assert r.json()["caller_ip"] == "2001:db8::1"
