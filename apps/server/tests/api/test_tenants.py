"""Tests for ``rag_service.api.routers.tenants`` — /v1/tenants/me.

The DB session is stubbed via ``app.dependency_overrides`` so the suite
needs no real Postgres. The router issues three sequential queries
(SELECT tenant, SUM(file_size), COUNT(document_id)); the fake session
inspects each statement and dispatches accordingly. We exercise:

* happy path — tenant + 2 indexed docs (1 MB + 2 MB) → storage_used_mb=3.0,
  document_count=2;
* claim doesn't match any tenant row → 404;
* mixed seed where one of three docs is soft-deleted → count=2 and the
  storage sum excludes the deleted row.
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
os.environ.setdefault("DATA_DIR", "/tmp/rag_tenants_api_test")

import datetime as _dt  # noqa: E402
import uuid  # noqa: E402
from typing import Any  # noqa: E402

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from rag_service.api.auth import current_tenant, current_user  # noqa: E402
from rag_service.api.deps import get_db_session  # noqa: E402
from rag_service.api.routers import tenants as tenants_mod  # noqa: E402
from rag_service.db.models import Document, Tenant  # noqa: E402


_MB = 1024 * 1024


class _MockUser:
    """Minimal stand-in for the ``User`` row that ``current_user`` returns."""

    def __init__(self, user_id: uuid.UUID | None = None) -> None:
        self.user_id = user_id or uuid.uuid4()
        self.is_active = True


# ---------------------------------------------------------------------------
# Fake DB session
# ---------------------------------------------------------------------------


class _FakeScalarOneOrNoneResult:
    """Mimic ``Result.scalar_one_or_none()``."""

    def __init__(self, obj: Any | None) -> None:
        self._obj = obj

    def scalar_one_or_none(self) -> Any | None:
        return self._obj


class _FakeScalarOneResult:
    """Mimic ``Result.scalar_one()`` for aggregate queries."""

    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one(self) -> Any:
        return self._value


class _FakeSession:
    """In-memory stand-in for an :class:`AsyncSession`.

    Holds a list of ``Tenant`` rows and a list of ``Document`` rows.
    ``execute()`` inspects the rendered SQL of the incoming statement to
    dispatch:

    * ``SELECT tenants.*`` — return the single matching row (or ``None``).
    * ``SELECT sum(...)`` — sum ``file_size`` over the seeded docs that
      match the tenant + status filters expressed in the bound params.
    * ``SELECT count(...)`` — same filtering, return the row count.

    We rely on bound-param values for the ``tenant_id`` filter and on
    rendered SQL substrings for the aggregate dispatch; this mirrors the
    pattern used by ``test_documents.py``.
    """

    def __init__(
        self,
        tenants: list[Tenant],
        docs: list[Document],
    ) -> None:
        self.tenants = list(tenants)
        self.docs = list(docs)

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def execute(self, stmt: Any) -> Any:
        compiled_sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        upper = compiled_sql.upper()

        params = stmt.compile().params
        bound_values = list(params.values())
        # Tenant id is the only ``tnt-`` prefixed string our tests use.
        tenant_id = next(
            (v for v in bound_values if isinstance(v, str) and v.startswith("tnt-")),
            None,
        )

        # SELECT tenants.* — single row by primary key.
        if "FROM TENANTS" in upper:
            match = next(
                (t for t in self.tenants if t.tenant_id == tenant_id),
                None,
            )
            return _FakeScalarOneOrNoneResult(match)

        # Aggregate queries on documents: same tenant + status != 'deleted'
        # filter for both SUM(file_size) and COUNT(document_id).
        candidates = [
            d
            for d in self.docs
            if d.tenant_id == tenant_id and d.status != "deleted"
        ]

        if "SUM(" in upper:
            total = sum((d.file_size or 0) for d in candidates)
            return _FakeScalarOneResult(total)

        if "COUNT(" in upper:
            return _FakeScalarOneResult(len(candidates))

        raise AssertionError(f"unexpected statement in fake session: {compiled_sql}")


# ---------------------------------------------------------------------------
# App + test-client wiring
# ---------------------------------------------------------------------------


def _make_app(fake_session: _FakeSession, *, tenant: str = "tnt-1") -> FastAPI:
    """Build a FastAPI app with the tenants router and DB fake wired in.

    Auth is overridden directly: ``current_user`` returns a mock user and
    ``current_tenant`` returns ``tenant``. Cross-tenant tests pass a
    different ``tenant`` instead of threading a header.
    """
    app = FastAPI()
    app.include_router(tenants_mod.router)

    async def _db_override():
        try:
            yield fake_session
        except Exception:
            await fake_session.rollback()
            raise

    async def _user_override() -> _MockUser:
        return _MockUser()

    async def _tenant_override() -> str:
        return tenant

    app.dependency_overrides[get_db_session] = _db_override
    app.dependency_overrides[current_user] = _user_override
    app.dependency_overrides[current_tenant] = _tenant_override
    return app


def _make_tenant(
    tenant_id: str = "tnt-1",
    *,
    display_name: str = "Acme Co.",
    storage_quota_mb: int = 1024,
) -> Tenant:
    """Build a transient :class:`Tenant` instance the router can read from."""
    return Tenant(
        tenant_id=tenant_id,
        display_name=display_name,
        storage_quota_mb=storage_quota_mb,
    )


def _make_doc(
    tenant_id: str = "tnt-1",
    *,
    file_size: int | None = _MB,
    status_: str = "indexed",
    **overrides: Any,
) -> Document:
    """Build a fully-populated transient :class:`Document` instance."""
    base_time = _dt.datetime(2026, 5, 4, 12, 0, 0, tzinfo=_dt.timezone.utc)
    defaults: dict[str, Any] = {
        "document_id": uuid.uuid4(),
        "tenant_id": tenant_id,
        "file_name": "doc.pdf",
        "file_size": file_size,
        "content_hash": uuid.uuid4().hex,
        "mime_type": "application/pdf",
        "storage_path": "/tmp/x.pdf",
        "status": status_,
        "uploaded_at": base_time,
        "indexed_at": None,
        "error_message": None,
    }
    defaults.update(overrides)
    return Document(**defaults)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_me_returns_tenant_info():
    """Tenant + two indexed 1 MB / 2 MB docs surfaces correctly aggregated stats."""
    tenant = _make_tenant("tnt-1", display_name="Acme Co.", storage_quota_mb=1024)
    doc_a = _make_doc(tenant_id="tnt-1", file_size=1 * _MB, file_name="a.pdf")
    doc_b = _make_doc(tenant_id="tnt-1", file_size=2 * _MB, file_name="b.pdf")
    session = _FakeSession(tenants=[tenant], docs=[doc_a, doc_b])
    app = _make_app(session, tenant="tnt-1")

    client = TestClient(app)
    r = client.get("/v1/tenants/me")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant_id"] == "tnt-1"
    assert body["display_name"] == "Acme Co."
    assert body["storage_quota_mb"] == 1024
    assert body["storage_used_mb"] == 3.0
    assert body["document_count"] == 2


def test_me_404_if_tenant_missing():
    """An auth claim with no matching ``tenants`` row collapses to 404."""
    # Seed only an unrelated tenant so the lookup misses.
    other = _make_tenant("tnt-other")
    session = _FakeSession(tenants=[other], docs=[])
    app = _make_app(session, tenant="tnt-1")

    client = TestClient(app)
    r = client.get("/v1/tenants/me")

    assert r.status_code == 404
    assert r.json() == {"detail": "tenant not found"}


def test_me_excludes_deleted_docs():
    """Soft-deleted docs are excluded from both the size sum and the count."""
    tenant = _make_tenant("tnt-1", storage_quota_mb=1024)
    live_a = _make_doc(tenant_id="tnt-1", file_size=1 * _MB, file_name="a.pdf")
    live_b = _make_doc(tenant_id="tnt-1", file_size=2 * _MB, file_name="b.pdf")
    deleted = _make_doc(
        tenant_id="tnt-1",
        file_size=10 * _MB,
        status_="deleted",
        file_name="ghost.pdf",
    )
    session = _FakeSession(tenants=[tenant], docs=[live_a, live_b, deleted])
    app = _make_app(session, tenant="tnt-1")

    client = TestClient(app)
    r = client.get("/v1/tenants/me")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["document_count"] == 2
    # 1 MB + 2 MB only — the 10 MB deleted row must not be counted.
    assert body["storage_used_mb"] == 3.0
