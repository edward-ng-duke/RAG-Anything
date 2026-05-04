"""End-to-end roundtrip across the entire ``/v1/onyx/*`` surface.

This test drives a single linear flow through every public ONYX
endpoint with the real FastAPI app stack (built via :func:`create_app`
so middlewares are wired) and the same dependency overrides used by
``test_ingest_query.py``. Every step asserts both the HTTP status and
a load-bearing field of the response payload before moving on, so a
mid-flow failure stops the chain at exactly the broken hop.

Mocked / stubbed externals:

* :class:`RAGAnything` — replaced with an :class:`AsyncMock` whose
  ``aquery`` returns a canned answer + sources tuple.
* ``rag_service.kg.repository.stats`` and ``.list_entities`` —
  monkeypatched to return canned dicts so the KG path doesn't need a
  live LightRAG schema.
* ``rag_service.services.ingest.enqueue_ingest`` and
  ``rag_service.api.routers.onyx_documents.enqueue_rebuild`` — replaced
  with no-op stubs so the upload + delete paths don't spin a real arq
  pool.

What this test deliberately does *not* exercise:

* Streaming SSE (``/v1/onyx/query``) — the heartbeat behavior is
  clock-sensitive; ``query/sync`` is an easier assertion target and
  shares the same upstream call path with the SSE generator.
* IP-allowlist short-circuit and rate limiting — those are exercised
  by ``tests/api/test_app_onyx_integration.py`` and
  ``tests/api/test_rate_limit_*``. Here the allowlist is empty (default)
  so requests pass straight through to the auth dep, and the rate-limit
  middleware fails open against the unreachable test Redis.
* The full worker indexing pipeline — the job stays ``"queued"`` because
  the arq enqueue is stubbed; this proves the HTTP plumbing all the way
  to the persistence layer without invoking the parser / LLM stacks.

The metadata patches at the top mirror ``test_ingest_query.py``: SQLite
can't natively express ``JSONB``, ``UUID``, server-side ``gen_random_uuid()``
defaults, or ``Identity`` columns, so we substitute Python-side
equivalents via the same ``compiles(...)`` + ``ColumnDefault`` overrides.
"""

from __future__ import annotations

# conftest.py at tests/e2e/ sets the env vars required by Settings before
# any rag_service import below.

import re
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.schema import ColumnDefault
from httpx import ASGITransport, AsyncClient


# ---------------------------------------------------------------------------
# PG → SQLite metadata patches (mirrors tests/e2e/test_ingest_query.py)
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
    """Swap PG-only server defaults for sqlite-friendly Python defaults."""
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
            if getattr(col, "identity", None) is not None:
                col.identity = None
                col.autoincrement = True
                if col.primary_key and col.default is None:
                    col.default = ColumnDefault(_next_id)


_patch_metadata_for_sqlite()


# ---------------------------------------------------------------------------
# Constants — token matches conftest (INTERNAL_TOKEN = "e" * 64)
# ---------------------------------------------------------------------------


_TOKEN = "e" * 64
_KB_ID_RE = re.compile(
    r"^onyx-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_onyx_full_flow_e2e(tmp_path, monkeypatch):
    """Drive the entire ``/v1/onyx/*`` surface in one linear flow."""
    # ------------------------------------------------------------------
    # 1) Pin settings the routers / auth deps consult.
    # ------------------------------------------------------------------
    from rag_service.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "internal_token", _TOKEN)
    monkeypatch.setattr(settings, "internal_tokens_legacy", [])
    monkeypatch.setattr(settings, "onyx_backend_allowed_cidrs", [])

    # ------------------------------------------------------------------
    # 2) Fresh in-memory DB + sessionmaker.
    # ------------------------------------------------------------------
    from rag_service.db.base import Base
    from rag_service.db import models

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    # ------------------------------------------------------------------
    # 3) Build app via create_app() so the real middleware chain runs,
    #    but with a no-op lifespan so startup doesn't try to open a real
    #    Redis. We monkeypatch the lifespan attr on the imported module
    #    BEFORE calling create_app(); FastAPI captures the lifespan at
    #    construction time.
    # ------------------------------------------------------------------
    from rag_service.api import app as app_mod

    @asynccontextmanager
    async def _noop_lifespan(app):  # noqa: ARG001
        yield

    monkeypatch.setattr(app_mod, "lifespan", _noop_lifespan)
    app = app_mod.create_app()

    # ------------------------------------------------------------------
    # 4) Override the DB session + RAG cache deps.
    # ------------------------------------------------------------------
    from rag_service.api.deps import get_db_session, get_rag_cache
    from rag_service.api.routers import onyx_documents as onyx_docs_mod
    from rag_service.services import ingest as ingest_svc

    async def _db_override():
        async with SessionLocal() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    fake_rag = AsyncMock()
    canned_answer = "mocked answer"

    async def _aquery(*args, **kwargs):  # noqa: ARG001
        return {
            "answer": canned_answer,
            "sources": [
                {
                    "document_id": "deadbeef-dead-beef-dead-beefdeadbeef",
                    "file_name": "hello.txt",
                    "chunk_id": "c1",
                    "score": 0.9,
                }
            ],
            # cost_usd is omitted because OnyxQuerySyncResponse types tokens
            # as ``dict[str, int]``; including a float would 500 the response.
            "tokens": {"in": 5, "out": 7},
        }

    fake_rag.aquery = AsyncMock(side_effect=_aquery)

    class _FakeCache:
        async def get(self, kb_id):  # noqa: ARG002
            return fake_rag

    fake_cache = _FakeCache()

    async def _cache_override():
        return fake_cache

    app.dependency_overrides[get_db_session] = _db_override
    app.dependency_overrides[get_rag_cache] = _cache_override

    # ------------------------------------------------------------------
    # 5) Stub the arq enqueue helpers — neither path needs a real Redis.
    # ------------------------------------------------------------------
    async def _noop_ingest(tenant_id, document_id):  # noqa: ARG001
        return None

    async def _noop_rebuild(tenant_id):  # noqa: ARG001
        return None

    monkeypatch.setattr(ingest_svc, "enqueue_ingest", _noop_ingest)
    monkeypatch.setattr(onyx_docs_mod, "enqueue_rebuild", _noop_rebuild)

    # ------------------------------------------------------------------
    # 6) Drive the HTTP surface.
    # ------------------------------------------------------------------
    HEADERS = {"Authorization": f"Bearer {_TOKEN}"}
    HEADERS_NO_KB = {**HEADERS, "X-Onyx-User-Id": "u_test"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        # ----- Step 1: Create KB ----------------------------------------
        r = await ac.post(
            "/v1/onyx/kb",
            headers=HEADERS_NO_KB,
            json={"display_name": "E2E KB"},
        )
        assert r.status_code == 201, r.text
        kb_body = r.json()
        kb_id = kb_body["kb_id"]
        assert _KB_ID_RE.match(kb_id), kb_id
        assert kb_body["display_name"] == "E2E KB"

        # KB-scoped headers are derivable now.
        HEADERS_KB = {**HEADERS_NO_KB, "X-Onyx-KB-Id": kb_id}

        # ----- Step 2: List KBs -----------------------------------------
        r = await ac.get("/v1/onyx/kb", headers=HEADERS_NO_KB)
        assert r.status_code == 200, r.text
        list_body = r.json()
        assert any(item["kb_id"] == kb_id for item in list_body["items"])

        # ----- Step 3: Get KB by id -------------------------------------
        r = await ac.get(f"/v1/onyx/kb/{kb_id}", headers=HEADERS_KB)
        assert r.status_code == 200, r.text
        get_kb_body = r.json()
        assert get_kb_body["kb_id"] == kb_id
        assert get_kb_body["display_name"] == "E2E KB"

        # ----- Step 4: Upload document ----------------------------------
        # PDF magic header keeps the MIME sniffer happy without pulling
        # python-magic into the test environment.
        pdf_bytes = b"%PDF-1.4\n%onyx-e2e\n%%EOF\n"
        r = await ac.post(
            "/v1/onyx/documents",
            headers=HEADERS_KB,
            files={
                "file": ("hello.pdf", pdf_bytes, "application/pdf"),
            },
        )
        assert r.status_code == 202, r.text
        upload_body = r.json()
        document_id = upload_body["document_id"]
        job_id = upload_body["job_id"]
        # Both ids must parse as UUIDs.
        uuid.UUID(document_id)
        assert job_id is not None
        uuid.UUID(job_id)
        assert upload_body["deduplicated"] is False
        assert upload_body["status"] == "queued"
        assert upload_body["file_name"] == "hello.pdf"

        # ----- Step 5: Poll job -----------------------------------------
        # Worker isn't running — the row stays ``queued``.
        r = await ac.get(f"/v1/onyx/jobs/{job_id}", headers=HEADERS_KB)
        assert r.status_code == 200, r.text
        job_body = r.json()
        assert job_body["status"] == "queued"
        assert job_body["job_type"] == "ingest"
        assert job_body["document_id"] == document_id

        # ----- Step 6: List documents -----------------------------------
        r = await ac.get("/v1/onyx/documents", headers=HEADERS_KB)
        assert r.status_code == 200, r.text
        docs_body = r.json()
        assert len(docs_body["items"]) == 1
        assert docs_body["items"][0]["document_id"] == document_id

        # ----- Step 7: Get document -------------------------------------
        r = await ac.get(
            f"/v1/onyx/documents/{document_id}", headers=HEADERS_KB
        )
        assert r.status_code == 200, r.text
        doc_body = r.json()
        assert doc_body["document_id"] == document_id
        assert doc_body["file_name"] == "hello.pdf"

        # ----- Step 8: Stateless query (sync) ---------------------------
        r = await ac.post(
            "/v1/onyx/query/sync",
            headers={**HEADERS_KB, "X-Request-Id": "req-e2e-1"},
            json={
                "question": "What is X?",
                "history": [],
                "mode": "hybrid",
                "top_k": 5,
                "include_sources": True,
            },
        )
        assert r.status_code == 200, r.text
        q_body = r.json()
        assert q_body["answer"] == canned_answer
        assert q_body["request_id"] == "req-e2e-1"
        # Sources are echoed through the OnyxQuerySource shape.
        assert len(q_body["sources"]) == 1
        assert q_body["sources"][0]["chunk_id"] == "c1"
        fake_rag.aquery.assert_awaited()

        # ----- Step 9: KG stats -----------------------------------------
        from rag_service.kg import repository as kg_repo

        stats_mock = AsyncMock(
            return_value={"entities": 42, "relations": 7, "chunks": 1}
        )
        monkeypatch.setattr(kg_repo, "stats", stats_mock)

        r = await ac.get("/v1/onyx/kg/stats", headers=HEADERS_KB)
        assert r.status_code == 200, r.text
        stats_body = r.json()
        assert stats_body == {"entities": 42, "relations": 7, "chunks": 1}

        # ----- Step 10: List entities -----------------------------------
        list_entities_mock = AsyncMock(
            return_value={
                "items": [{"id": "e1", "entity_name": "Foo"}],
                "next_cursor": None,
            }
        )
        monkeypatch.setattr(kg_repo, "list_entities", list_entities_mock)

        r = await ac.get("/v1/onyx/kg/entities", headers=HEADERS_KB)
        assert r.status_code == 200, r.text
        entities_body = r.json()
        assert entities_body["next_cursor"] is None
        assert len(entities_body["items"]) == 1
        assert entities_body["items"][0]["id"] == "e1"
        assert entities_body["items"][0]["entity_name"] == "Foo"

        # ----- Step 11: DELETE document ---------------------------------
        r = await ac.delete(
            f"/v1/onyx/documents/{document_id}", headers=HEADERS_KB
        )
        assert r.status_code == 204, r.text

        # Verify the row is soft-deleted (status flipped) — direct DB peek.
        async with SessionLocal() as s:
            row = (
                await s.execute(
                    select(models.Document).where(
                        models.Document.document_id == uuid.UUID(document_id)
                    )
                )
            ).scalar_one_or_none()
            assert row is not None
            assert row.status == "deleted"

        # ----- Step 12: DELETE KB (cascade) -----------------------------
        r = await ac.delete(f"/v1/onyx/kb/{kb_id}", headers=HEADERS_KB)
        assert r.status_code == 204, r.text

        # The cascade should have wiped the Tenant + Document + Job rows.
        async with SessionLocal() as s:
            tenant_row = (
                await s.execute(
                    select(models.Tenant).where(
                        models.Tenant.tenant_id == kb_id
                    )
                )
            ).scalar_one_or_none()
            assert tenant_row is None

            doc_rows = (
                await s.execute(
                    select(models.Document).where(
                        models.Document.tenant_id == kb_id
                    )
                )
            ).scalars().all()
            assert doc_rows == []

            job_rows = (
                await s.execute(
                    select(models.Job).where(models.Job.tenant_id == kb_id)
                )
            ).scalars().all()
            assert job_rows == []

    # ------------------------------------------------------------------
    # 7) Cleanup.
    # ------------------------------------------------------------------
    await engine.dispose()
