"""End-to-end ingest+query roundtrip with mocked external services.

The full ``RAGAnything`` stack (LLM, MinerU Cloud, vector store) is not
available in CI, so this test wires real FastAPI routers against:

* an in-memory SQLite (``sqlite+aiosqlite:///:memory:``) for the control-plane DB;
* a fake :class:`AsyncMock` ``RAGAnything`` instance returned from a fake cache;
* a stubbed ``enqueue_ingest`` so the arq pool is never opened.

What the test proves end-to-end:

1. ``POST /v1/ingest`` writes ``documents`` + ``jobs`` rows and returns
   ``deduplicated=False`` with well-formed UUIDs.
2. ``GET /v1/jobs/{job_id}`` reads the freshly-created job back.
3. After flipping the document/job to indexed/done (simulating a worker
   pass), ``POST /v1/query`` dispatches to the mocked RA's ``aquery``,
   surfaces the answer, writes a ``query_log`` row, and returns 200.

The test deliberately stops short of running the real ``ingest_document``
arq task body because that path requires the live RAG factory + parser
plumbing — those layers have their own unit coverage in
``tests/worker/test_tasks_ingest.py`` and ``tests/core/test_rag_factory.py``.
What we get here is the wiring proof: HTTP → router → DB → response, with
the same dependency graph the real app uses.

The schema patches at the top of the test (compile JSONB→JSON, UUID→CHAR(36),
strip ``::jsonb`` / ``gen_random_uuid()`` / ``Identity`` server defaults)
are necessary because the production schema is Postgres-specific. We
substitute Python-side defaults so ``Base.metadata.create_all`` produces a
valid SQLite DDL while leaving inserts working through the ORM.
"""

from __future__ import annotations

# conftest.py sets the required env vars before any rag_service import.

import io
import uuid
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.schema import ColumnDefault


# ---------------------------------------------------------------------------
# Make the PG schema buildable on SQLite
# ---------------------------------------------------------------------------
#
# The production models in ``rag_service.db.models`` lean on Postgres-only
# types (UUID, JSONB) and server-side defaults (``gen_random_uuid()``,
# ``'{}'::jsonb``, ``Identity(always=True)``). For an in-memory SQLite to
# stand in, we:
#   1. teach SQLAlchemy to compile JSONB → JSON and UUID → CHAR(36) under
#      the sqlite dialect;
#   2. swap PG-specific server defaults for Python-side ``ColumnDefault``s
#      (UUIDs default to :func:`uuid.uuid4`, JSONB columns default to ``{}``);
#   3. drop ``Identity`` so SQLite uses its built-in autoincrement.
#
# This module-import-time mutation of ``Base.metadata`` is irreversible
# within the test process, but it's harmless: the production engine never
# observes these settings (Postgres compiles JSONB / UUID natively and
# applies the server defaults), and no other test module recreates the
# schema from this metadata.


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
    """Replace PG-only server defaults with sqlite-friendly Python defaults."""
    # Imported lazily so the env-var bootstrap in conftest runs first.
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
            # Identity is unsupported on sqlite. SQLite's built-in
            # autoincrement only fires for ``INTEGER PRIMARY KEY`` columns,
            # but the ``query_log.id`` column is BIGINT — autoincrement won't
            # apply, so we hand it a Python-side counter default instead.
            if getattr(col, "identity", None) is not None:
                col.identity = None
                col.autoincrement = True
                if col.primary_key and col.default is None:
                    col.default = ColumnDefault(_next_id)


_patch_metadata_for_sqlite()


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


_TEST_TOKEN = "e2e-secret"
_TEST_TENANT = "tnt-e2e"


@pytest.mark.asyncio
async def test_e2e_ingest_query_roundtrip(tmp_path, monkeypatch):
    """Upload → queue → (simulated) index → query, all via HTTP."""
    # ------------------------------------------------------------------
    # 1) Fresh in-memory DB, schema created from the patched metadata.
    # ------------------------------------------------------------------
    from rag_service.db.base import Base
    from rag_service.db import models

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    # Seed the tenant the rest of the test will use. The router resolves
    # ``tenant_id`` from the auth header but doesn't enforce a tenants-row
    # existence on every request; we still seed it so foreign-key writes
    # from the ``documents`` table succeed.
    async with SessionLocal() as s:
        s.add(models.Tenant(tenant_id=_TEST_TENANT, display_name="E2E Tenant"))
        await s.commit()

    # ------------------------------------------------------------------
    # 2) Pin settings the routers consult.
    # ------------------------------------------------------------------
    from rag_service.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "internal_token", _TEST_TOKEN)

    # ------------------------------------------------------------------
    # 3) Build the app with a no-op lifespan so startup doesn't try to
    #    connect to a real Redis. We can't reuse ``create_app``'s lifespan
    #    because it spins the reload listener against ``settings.redis_url``.
    # ------------------------------------------------------------------
    from contextlib import asynccontextmanager

    from fastapi import FastAPI

    from rag_service.api.routers.documents import router as documents_router
    from rag_service.api.routers.health import router as health_router
    from rag_service.api.routers.ingest import router as ingest_router
    from rag_service.api.routers.jobs import router as jobs_router
    from rag_service.api.routers.query import router as query_router
    from rag_service.api.routers.tenants import router as tenants_router

    @asynccontextmanager
    async def _noop_lifespan(app):  # noqa: ARG001
        yield

    app = FastAPI(lifespan=_noop_lifespan)
    app.include_router(health_router)
    app.include_router(ingest_router)
    app.include_router(jobs_router)
    app.include_router(documents_router)
    app.include_router(query_router)
    app.include_router(tenants_router)

    # ------------------------------------------------------------------
    # 4) Override deps. The DB dep yields from our in-memory engine; the
    #    rag-cache dep returns a fake whose ``aquery`` is a canned answer.
    # ------------------------------------------------------------------
    from rag_service.api.deps import get_db_session, get_rag_cache, get_redis
    from rag_service.api.routers import ingest as ingest_mod

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

    fake_rag = AsyncMock()
    fake_rag.aquery = AsyncMock(
        return_value={
            "answer": "The answer is X.",
            "sources": [
                {
                    "document_id": "deadbeef-dead-beef-dead-beefdeadbeef",
                    "file_name": "hello.pdf",
                    "chunk_id": "chunk-0",
                    "score": 0.99,
                    "snippet": "X is the answer.",
                    "modality": "text",
                }
            ],
            "tokens": {"in": 12, "out": 5, "cost_usd": 0.0001},
        }
    )

    class _FakeCache:
        async def get(self, tenant_id):  # noqa: ARG002
            return fake_rag

    fake_cache = _FakeCache()

    async def _cache_override():
        return fake_cache

    app.dependency_overrides[get_db_session] = _db_override
    app.dependency_overrides[get_redis] = _redis_override
    app.dependency_overrides[get_rag_cache] = _cache_override

    # Stub the arq enqueue so the ingest path doesn't open a real Redis
    # pool — fakeredis can't satisfy arq's ``RedisSettings.from_dsn`` flow.
    enqueue_stub = AsyncMock(return_value=None)
    monkeypatch.setattr(ingest_mod, "enqueue_ingest", enqueue_stub)

    # ------------------------------------------------------------------
    # 5) Drive the HTTP surface.
    # ------------------------------------------------------------------
    client = TestClient(app)
    headers = {
        "Authorization": f"Bearer {_TEST_TOKEN}",
        "X-Tenant-Id": _TEST_TENANT,
    }

    # 5a) Ingest a tiny PDF.
    pdf_bytes = b"%PDF-1.4\n%e2e-roundtrip\n%%EOF\n"
    r = client.post(
        "/v1/ingest",
        headers=headers,
        files={"file": ("hello.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "queued"
    assert body["deduplicated"] is False
    document_id = body["document_id"]
    job_id = body["job_id"]
    uuid.UUID(document_id)
    uuid.UUID(job_id)
    enqueue_stub.assert_awaited_once()

    # 5b) Job is queryable via the public API.
    r = client.get(f"/v1/jobs/{job_id}", headers=headers)
    assert r.status_code == 200, r.text
    job_body = r.json()
    assert job_body["status"] == "queued"
    assert job_body["job_type"] == "ingest"
    assert job_body["document_id"] == document_id

    # 5c) Document is queryable too, and pending.
    r = client.get(f"/v1/documents/{document_id}", headers=headers)
    assert r.status_code == 200, r.text
    doc_body = r.json()
    assert doc_body["status"] == "pending"
    assert doc_body["file_name"] == "hello.pdf"
    assert doc_body["mime_type"] == "application/pdf"

    # 5d) Simulate the worker finishing: flip statuses and stamp indexed_at.
    import datetime as _dt

    async with SessionLocal() as s:
        await s.execute(
            update(models.Document)
            .where(models.Document.document_id == uuid.UUID(document_id))
            .values(status="indexed", indexed_at=_dt.datetime.now(_dt.timezone.utc))
        )
        await s.execute(
            update(models.Job)
            .where(models.Job.job_id == uuid.UUID(job_id))
            .values(
                status="done",
                finished_at=_dt.datetime.now(_dt.timezone.utc),
            )
        )
        await s.commit()

    # 5e) Document now reports ``indexed``.
    r = client.get(f"/v1/documents/{document_id}", headers=headers)
    assert r.status_code == 200
    assert r.json()["status"] == "indexed"

    # 5f) Query — the mocked RA returns our canned dict.
    r = client.post(
        "/v1/query",
        headers=headers,
        json={"question": "What is X?", "mode": "hybrid", "top_k": 5},
    )
    assert r.status_code == 200, r.text
    q_body = r.json()
    assert "X" in q_body["answer"]
    assert q_body["answer"] == "The answer is X."
    assert len(q_body["sources"]) == 1
    src = q_body["sources"][0]
    assert src["file_name"] == "hello.pdf"
    assert src["score"] == pytest.approx(0.99)
    assert q_body["tokens"] == {"in": 12, "out": 5, "cost_usd": 0.0001}
    fake_rag.aquery.assert_awaited_once_with("What is X?", mode="hybrid", top_k=5)

    # 5g) The query_log row landed.
    async with SessionLocal() as s:
        rows = (await s.execute(select(models.QueryLog))).scalars().all()
    assert len(rows) == 1
    log = rows[0]
    assert log.tenant_id == _TEST_TENANT
    assert log.question == "What is X?"
    assert log.mode == "hybrid"
    assert log.token_in == 12
    assert log.token_out == 5

    # ------------------------------------------------------------------
    # 6) Cleanup. async engines have to be disposed explicitly so the
    # session-scoped tasks don't leak into the next test module.
    # ------------------------------------------------------------------
    await engine.dispose()
    await fake_redis.aclose()
