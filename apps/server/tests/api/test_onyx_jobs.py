"""Tests for ``rag_service.api.routers.onyx_jobs`` — /v1/onyx/jobs.

KB-scoped job inspection for the ONYX integration. Single endpoint:

* ``GET /v1/onyx/jobs/{job_id}`` — fetch a job row scoped to
  ``X-Onyx-KB-Id`` (auth: :func:`onyx_service_auth`).

The tests stand up an in-memory SQLite engine with the same metadata
patches the rest of the onyx suite applies, mount the router with a
``get_db_session`` override, and hit the resulting app via
``httpx.ASGITransport``. Job rows are seeded directly through the
sessionmaker — no ingest pipeline involvement needed.
"""

from __future__ import annotations

# conftest.py at tests/ already populated the env vars. Override DATA_DIR
# locally so a stray Path resolution doesn't pollute another test's dir.
import os  # noqa: E402

os.environ.setdefault("DATA_DIR", "/tmp/rag_onyx_jobs_test")

import datetime as _dt  # noqa: E402
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
# Auth fixture — pin a known internal token, no legacy, no CIDR allowlist
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _auth_setup(monkeypatch):
    monkeypatch.setattr("rag_service.config.settings.internal_token", "a" * 96)
    monkeypatch.setattr("rag_service.config.settings.internal_tokens_legacy", [])
    monkeypatch.setattr(
        "rag_service.config.settings.onyx_backend_allowed_cidrs", []
    )


_TOKEN = "a" * 96


def _headers(kb_id: str | None = None) -> dict[str, str]:
    """Build the standard auth headers, optionally with X-Onyx-KB-Id."""
    h = {
        "Authorization": f"Bearer {_TOKEN}",
        "X-Onyx-User-Id": "u_test",
    }
    if kb_id is not None:
        h["X-Onyx-KB-Id"] = kb_id
    return h


# ---------------------------------------------------------------------------
# Per-test SQLite engine + tenant / job seeding
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
    """Insert a ``source=onyx`` Tenant row directly and return its kb_id."""
    from rag_service.db.models import Tenant

    kb_id = f"onyx-{uuid.uuid4()}"
    async with session_maker() as s:
        s.add(
            Tenant(
                tenant_id=kb_id,
                display_name=f"KB {suffix or 'test'}",
                storage_quota_mb=1024,
                config_json={"source": "onyx"},
            )
        )
        await s.commit()
    return kb_id


async def _seed_job(
    session_maker,
    kb_id: str,
    *,
    job_type: str = "ingest",
    status: str = "queued",
    progress: dict | None = None,
    document_id: uuid.UUID | None = None,
    error_message: str | None = None,
    created_at: _dt.datetime | None = None,
    started_at: _dt.datetime | None = None,
    finished_at: _dt.datetime | None = None,
    retries: int = 0,
) -> uuid.UUID:
    """Insert a Job row with controllable fields; returns the job_id."""
    from rag_service.db.models import Job

    job_id = uuid.uuid4()
    async with session_maker() as s:
        j = Job(
            job_id=job_id,
            tenant_id=kb_id,
            document_id=document_id,
            job_type=job_type,
            status=status,
            progress=progress if progress is not None else {},
            error_message=error_message,
            retries=retries,
        )
        if created_at is not None:
            j.created_at = created_at
        if started_at is not None:
            j.started_at = started_at
        if finished_at is not None:
            j.finished_at = finished_at
        s.add(j)
        await s.commit()
    return job_id


def _build_app(session_maker) -> FastAPI:
    """Mount the onyx_jobs router with the SQLite session override."""
    from rag_service.api.deps import get_db_session
    from rag_service.api.routers import onyx_jobs as onyx_jobs_mod

    app = FastAPI()
    app.include_router(onyx_jobs_mod.router)

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
# GET /v1/onyx/jobs/{job_id}
# ===========================================================================


async def test_get_job_returns_full_row(session_maker):
    """Insert KB tenant + Job; GET → 200 with all fields populated."""
    kb_id = await _seed_kb(session_maker)
    created = _dt.datetime(2026, 5, 4, 12, 0, 0, tzinfo=_dt.timezone.utc)
    started = _dt.datetime(2026, 5, 4, 12, 0, 5, tzinfo=_dt.timezone.utc)
    finished = _dt.datetime(2026, 5, 4, 12, 1, 0, tzinfo=_dt.timezone.utc)
    doc_id = uuid.uuid4()
    job_id = await _seed_job(
        session_maker,
        kb_id,
        job_type="ingest",
        status="done",
        progress={"stage": "indexed", "percent": 100},
        document_id=doc_id,
        error_message=None,
        created_at=created,
        started_at=started,
        finished_at=finished,
        retries=2,
    )
    app = _build_app(session_maker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            f"/v1/onyx/jobs/{job_id}", headers=_headers(kb_id)
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["job_id"] == str(job_id)
    assert body["document_id"] == str(doc_id)
    assert body["job_type"] == "ingest"
    assert body["status"] == "done"
    assert body["progress"] == {"stage": "indexed", "percent": 100}
    assert body["error_message"] is None
    assert body["retries"] == 2
    assert body["created_at"].startswith("2026-05-04")
    assert body["started_at"].startswith("2026-05-04")
    assert body["finished_at"].startswith("2026-05-04")


async def test_get_job_404_for_unknown(session_maker):
    """Random UUID → 404."""
    kb_id = await _seed_kb(session_maker)
    app = _build_app(session_maker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            f"/v1/onyx/jobs/{uuid.uuid4()}", headers=_headers(kb_id)
        )
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "job not found"


async def test_get_job_404_for_cross_kb(session_maker):
    """Job owned by KB-A is invisible when calling with X-Onyx-KB-Id=KB-B."""
    kb_a = await _seed_kb(session_maker, suffix="A")
    kb_b = await _seed_kb(session_maker, suffix="B")
    job_id = await _seed_job(session_maker, kb_a)

    app = _build_app(session_maker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            f"/v1/onyx/jobs/{job_id}", headers=_headers(kb_b)
        )
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "job not found"


async def test_get_job_returns_progress_dict(session_maker):
    """Job.progress dict is echoed verbatim in the response."""
    kb_id = await _seed_kb(session_maker)
    progress = {"stage": "entity_extraction", "percent": 57}
    job_id = await _seed_job(
        session_maker, kb_id, status="running", progress=progress
    )

    app = _build_app(session_maker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            f"/v1/onyx/jobs/{job_id}", headers=_headers(kb_id)
        )
    assert r.status_code == 200, r.text
    assert r.json()["progress"] == progress


async def test_get_job_missing_token_401(session_maker):
    """No Authorization header → 401."""
    kb_id = await _seed_kb(session_maker)
    job_id = await _seed_job(session_maker, kb_id)

    app = _build_app(session_maker)
    transport = ASGITransport(app=app)
    # Strip the bearer header, keep X-Onyx-KB-Id so we exercise the
    # missing-token branch (not the missing-kb one).
    headers = {"X-Onyx-KB-Id": kb_id, "X-Onyx-User-Id": "u_test"}
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(f"/v1/onyx/jobs/{job_id}", headers=headers)
    assert r.status_code == 401, r.text


async def test_get_job_missing_kb_header_400(session_maker):
    """Authorization OK but no X-Onyx-KB-Id → 400."""
    kb_id = await _seed_kb(session_maker)
    job_id = await _seed_job(session_maker, kb_id)

    app = _build_app(session_maker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            f"/v1/onyx/jobs/{job_id}", headers=_headers()  # no kb_id
        )
    assert r.status_code == 400, r.text
    assert "X-Onyx-KB-Id" in r.json()["detail"]


async def test_get_job_invalid_uuid_returns_422(session_maker):
    """A non-UUID path component → FastAPI 422."""
    kb_id = await _seed_kb(session_maker)
    app = _build_app(session_maker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/v1/onyx/jobs/not-a-uuid", headers=_headers(kb_id)
        )
    assert r.status_code == 422, r.text
