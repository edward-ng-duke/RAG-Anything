"""Tests for ``rag_service.api.routers.jobs`` — GET /v1/jobs/{job_id}.

The DB is stubbed via ``app.dependency_overrides`` so the suite needs no
real Postgres. We exercise:

* happy path: a job owned by the caller's tenant returns 200 with the
  full :class:`JobResponse` shape;
* tenant isolation: a job that exists for tenant A is invisible to a
  request authenticated as tenant B (404, never 200/403);
* missing row: a random uuid yields 404;
* missing auth: no Authorization header → 401 from ``current_tenant``.
"""

from __future__ import annotations

# Required env vars must be set BEFORE importing anything from
# ``rag_service`` — the ``settings`` singleton is constructed lazily on
# first attribute access and will trip on missing required vars.
import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@h:5432/dbn")
os.environ.setdefault("REDIS_URL", "redis://x")
os.environ.setdefault("INTERNAL_TOKEN", "x")
os.environ.setdefault("LLM_BASE_URL", "http://llm")
os.environ.setdefault("LLM_API_KEY", "x")
os.environ.setdefault("LLM_MODEL", "m")
os.environ.setdefault("EMBEDDING_BASE_URL", "http://emb")
os.environ.setdefault("EMBEDDING_API_KEY", "x")
os.environ.setdefault("EMBEDDING_MODEL", "e")
os.environ.setdefault("DATA_DIR", "/tmp/rag_jobs_api_test")

import datetime as _dt  # noqa: E402
import uuid  # noqa: E402
from typing import Any  # noqa: E402

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from rag_service.api.deps import get_db_session  # noqa: E402
from rag_service.api.routers import jobs as jobs_mod  # noqa: E402
from rag_service.db.models import Job  # noqa: E402


# ---------------------------------------------------------------------------
# Fake DB session
# ---------------------------------------------------------------------------


class _FakeScalarResult:
    """Mimic the slice of ``Result`` we use: ``.scalar_one_or_none()``."""

    def __init__(self, obj: Any | None) -> None:
        self._obj = obj

    def scalar_one_or_none(self) -> Any | None:
        return self._obj


class _FakeSession:
    """Tiny stand-in for an :class:`AsyncSession` for the jobs router.

    Holds a single optional ``Job`` row keyed by ``(tenant_id, job_id)``
    and answers ``execute()`` by inspecting the compiled SQL: if both
    keys appear in the WHERE clause's bound literals, we return the row;
    otherwise we return ``None`` so the router returns 404.
    """

    def __init__(self, job: Job | None = None) -> None:
        self._job = job

    async def execute(self, stmt: Any) -> _FakeScalarResult:
        if self._job is None:
            return _FakeScalarResult(None)
        # Inspect bound parameters rather than the rendered SQL: SQLAlchemy
        # serialises UUIDs as hex (no dashes) under ``literal_binds`` which
        # is annoying to compare. The bound-param dict keeps native types,
        # so we can do an exact ``UUID == UUID`` and ``str == str`` match.
        params = stmt.compile().params
        bound_values = list(params.values())
        tenant_ok = self._job.tenant_id in bound_values
        job_ok = self._job.job_id in bound_values
        if tenant_ok and job_ok:
            return _FakeScalarResult(self._job)
        return _FakeScalarResult(None)

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


# ---------------------------------------------------------------------------
# App + test-client wiring
# ---------------------------------------------------------------------------


_TEST_TOKEN = "test-token"


def _make_app(fake_session: _FakeSession) -> FastAPI:
    """Build a FastAPI app with the jobs router and DB fake wired in."""
    app = FastAPI()
    app.include_router(jobs_mod.router)

    async def _db_override():
        try:
            yield fake_session
        except Exception:
            await fake_session.rollback()
            raise

    app.dependency_overrides[get_db_session] = _db_override
    return app


def _auth_headers(tenant: str = "tnt-1") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_TEST_TOKEN}",
        "X-Tenant-Id": tenant,
    }


def _make_job(tenant_id: str = "tnt-1", **overrides: Any) -> Job:
    """Build a fully-populated :class:`Job` ORM instance.

    The router skips the DB and returns the model directly via
    ``response_model=JobResponse`` + ``from_attributes=True``, so every
    field the schema reads must be set on the instance — server defaults
    are not applied for transient (un-flushed) rows.
    """
    now = _dt.datetime(2026, 5, 4, 12, 0, 0, tzinfo=_dt.timezone.utc)
    defaults: dict[str, Any] = {
        "job_id": uuid.uuid4(),
        "tenant_id": tenant_id,
        "document_id": uuid.uuid4(),
        "job_type": "ingest",
        "status": "queued",
        "progress": {"stage": "pending"},
        "created_at": now,
        "started_at": None,
        "finished_at": None,
        "error_message": None,
        "retries": 0,
    }
    defaults.update(overrides)
    return Job(**defaults)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def pinned_token(monkeypatch):
    """Pin ``settings.internal_token`` so auth is deterministic across the suite."""
    from rag_service.config import settings

    monkeypatch.setattr(settings, "internal_token", _TEST_TOKEN)
    return _TEST_TOKEN


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_get_own_job(pinned_token):
    """A job owned by the caller's tenant returns 200 with the JobResponse shape."""
    job = _make_job(
        tenant_id="tnt-1",
        status="running",
        progress={"stage": "embedding", "percent": 42},
        retries=1,
    )
    app = _make_app(_FakeSession(job=job))

    client = TestClient(app)
    r = client.get(f"/v1/jobs/{job.job_id}", headers=_auth_headers("tnt-1"))

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["job_id"] == str(job.job_id)
    assert body["document_id"] == str(job.document_id)
    assert body["job_type"] == "ingest"
    assert body["status"] == "running"
    assert body["progress"] == {"stage": "embedding", "percent": 42}
    assert body["error_message"] is None
    assert body["started_at"] is None
    assert body["finished_at"] is None
    assert body["retries"] == 1
    # created_at must serialise — exact format is pydantic's ISO-8601 default.
    assert body["created_at"].startswith("2026-05-04")


def test_get_cross_tenant_returns_404(pinned_token):
    """Job exists for tenant A; tenant B asking for the same id sees 404."""
    job = _make_job(tenant_id="tnt-A")
    app = _make_app(_FakeSession(job=job))

    client = TestClient(app)
    r = client.get(f"/v1/jobs/{job.job_id}", headers=_auth_headers("tnt-B"))

    assert r.status_code == 404
    assert r.json() == {"detail": "job not found"}


def test_get_missing_returns_404(pinned_token):
    """A random UUID with no seeded row yields 404."""
    app = _make_app(_FakeSession(job=None))

    client = TestClient(app)
    r = client.get(f"/v1/jobs/{uuid.uuid4()}", headers=_auth_headers("tnt-1"))

    assert r.status_code == 404
    assert r.json() == {"detail": "job not found"}


def test_missing_auth_401(pinned_token):
    """No Authorization header → 401 from ``current_tenant``."""
    app = _make_app(_FakeSession(job=None))

    client = TestClient(app)
    r = client.get(
        f"/v1/jobs/{uuid.uuid4()}",
        headers={"X-Tenant-Id": "tnt-1"},
    )
    assert r.status_code == 401
