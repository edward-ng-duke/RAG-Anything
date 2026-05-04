"""Tests for ``rag_service.api.routers.onyx_documents`` — /v1/onyx/documents.

Service-to-service document CRUD for the ONYX integration:

* ``POST   /v1/onyx/documents``                   — multipart upload (202).
* ``GET    /v1/onyx/documents``                   — list w/ cursor + status filter.
* ``GET    /v1/onyx/documents/{document_id}``     — single-row inspect.
* ``DELETE /v1/onyx/documents/{document_id}``     — soft-delete + rebuild enqueue.

Each test stands up a fresh in-memory SQLite engine with the same
metadata patches the rest of the onyx suite applies, mounts the new
router with a ``get_db_session`` override, and hits the resulting app
via ``httpx.ASGITransport``.

The arq enqueue helpers are monkey-patched on the router and service
modules so no Redis is required.
"""

from __future__ import annotations

# conftest.py at tests/ already populated the env vars. Override DATA_DIR
# locally so the upload pipeline writes into a tmp tree that's specific
# to this test module — keeps cleanup trivial and avoids cross-test
# pollution if other tests pick the same default.
import os  # noqa: E402

os.environ.setdefault("DATA_DIR", "/tmp/rag_onyx_documents_test")

import datetime as _dt  # noqa: E402
import hashlib  # noqa: E402
import uuid  # noqa: E402

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import select  # noqa: E402
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
# Auth fixture — known token, no legacy, no CIDR allowlist
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _auth_setup(monkeypatch, tmp_path):
    """Pin a known internal token + redirect uploads to a tmp tree.

    Each test gets its own ``tmp_path`` for ``settings.data_dir`` so
    streaming writes don't collide across tests.
    """
    monkeypatch.setattr("rag_service.config.settings.internal_token", "a" * 96)
    monkeypatch.setattr("rag_service.config.settings.internal_tokens_legacy", [])
    monkeypatch.setattr(
        "rag_service.config.settings.onyx_backend_allowed_cidrs", []
    )
    monkeypatch.setattr("rag_service.config.settings.data_dir", tmp_path)


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
# Per-test SQLite engine + tenant seeding
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
    """Insert a ``source=onyx`` Tenant row directly and return its kb_id.

    Goes around the KB router so this test module doesn't have to
    import / mount it. The shape mirrors what
    :func:`create_onyx_kb` would have produced (kb_id ``onyx-<uuid>``,
    ``config_json={"source": "onyx"}``) so :func:`onyx_service_auth`
    accepts the resulting id.
    """
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


def _build_app(session_maker, monkeypatch) -> FastAPI:
    """Mount the onyx_documents router with the SQLite session override.

    Also patches the service-module ``enqueue_ingest`` and the
    router-module ``enqueue_rebuild`` to no-ops so tests don't need
    Redis. Individual tests can replace these with mocks via their
    own monkeypatches.
    """
    from rag_service.api.deps import get_db_session
    from rag_service.api.routers import onyx_documents as onyx_docs_mod
    from rag_service.services import ingest as ingest_svc

    async def _noop_ingest(tenant_id: str, document_id: str) -> None:  # noqa: ARG001
        return None

    async def _noop_rebuild(tenant_id: str) -> None:  # noqa: ARG001
        return None

    monkeypatch.setattr(ingest_svc, "enqueue_ingest", _noop_ingest)
    monkeypatch.setattr(onyx_docs_mod, "enqueue_rebuild", _noop_rebuild)

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


# ===========================================================================
# POST /v1/onyx/documents — multipart upload
# ===========================================================================


_TXT_BODY = b"hello world from onyx\n"


async def test_upload_returns_202_with_document_id(session_maker, monkeypatch):
    """Happy path: POST a tiny text/plain file → 202 + queued + document_id."""
    kb_id = await _seed_kb(session_maker)
    app = _build_app(session_maker, monkeypatch)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.post(
            "/v1/onyx/documents",
            headers=_headers(kb_id),
            files={"file": ("hello.txt", _TXT_BODY, "text/plain")},
        )
    assert r.status_code == 202, r.text
    body = r.json()
    # document_id must parse as a UUID.
    uuid.UUID(body["document_id"])
    assert body["status"] == "queued"
    assert body["deduplicated"] is False
    assert body["file_name"] == "hello.txt"
    assert body["file_size"] == len(_TXT_BODY)
    assert body["content_hash"] == hashlib.sha256(_TXT_BODY).hexdigest()
    assert body["mime_type"] == "text/plain"
    # job_id should be present and parse as UUID.
    assert body["job_id"] is not None
    uuid.UUID(body["job_id"])


async def test_upload_dedup_returns_existing_document_id(session_maker, monkeypatch):
    """An identical-content second upload returns deduplicated=True with the same id."""
    from rag_service.db.models import Document

    kb_id = await _seed_kb(session_maker)
    body = b"deduplicate me\n"
    content_hash = hashlib.sha256(body).hexdigest()
    existing_doc_id = uuid.uuid4()

    # Pre-seed a Document with a matching (kb_id, content_hash).
    async with session_maker() as s:
        s.add(
            Document(
                document_id=existing_doc_id,
                tenant_id=kb_id,
                file_name="prior.txt",
                file_size=len(body),
                content_hash=content_hash,
                mime_type="text/plain",
                storage_path="/tmp/prior.txt",
                status="indexed",
            )
        )
        await s.commit()

    app = _build_app(session_maker, monkeypatch)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.post(
            "/v1/onyx/documents",
            headers=_headers(kb_id),
            files={"file": ("dup.txt", body, "text/plain")},
        )
    assert r.status_code == 202, r.text
    out = r.json()
    assert out["deduplicated"] is True
    assert out["status"] == "dedup"
    assert out["document_id"] == str(existing_doc_id)


async def test_upload_unsupported_mime_returns_415(session_maker, monkeypatch):
    """A non-allow-list payload (HTML w/ NUL byte) → 415."""
    kb_id = await _seed_kb(session_maker)
    app = _build_app(session_maker, monkeypatch)
    transport = ASGITransport(app=app)
    # NUL byte defeats the printable-text fallback; ``<html>`` doesn't
    # match any other magic signature, so the sniffer returns
    # (None, None) and the router yields 415.
    payload = b"<html>\x00<body>nope</body></html>"
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.post(
            "/v1/onyx/documents",
            headers=_headers(kb_id),
            files={"file": ("evil.html", payload, "text/html")},
        )
    assert r.status_code == 415, r.text


async def test_upload_missing_token_returns_401(session_maker, monkeypatch):
    """No Authorization header → 401 from the auth dep."""
    kb_id = await _seed_kb(session_maker)
    app = _build_app(session_maker, monkeypatch)
    transport = ASGITransport(app=app)
    # Strip the bearer header, keep X-Onyx-KB-Id so we exercise the
    # missing-token branch specifically (not the missing-kb one).
    headers = {"X-Onyx-KB-Id": kb_id, "X-Onyx-User-Id": "u_test"}
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.post(
            "/v1/onyx/documents",
            headers=headers,
            files={"file": ("hello.txt", b"x", "text/plain")},
        )
    assert r.status_code == 401, r.text


async def test_upload_missing_kb_header_returns_400(session_maker, monkeypatch):
    """Authorization OK but no X-Onyx-KB-Id → 400 from the auth dep."""
    app = _build_app(session_maker, monkeypatch)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.post(
            "/v1/onyx/documents",
            headers=_headers(),  # no kb_id
            files={"file": ("hello.txt", b"x", "text/plain")},
        )
    assert r.status_code == 400, r.text
    assert "X-Onyx-KB-Id" in r.json()["detail"]


async def test_upload_unknown_kb_returns_404(session_maker, monkeypatch):
    """Well-formed kb_id with no row → 404 from the auth dep."""
    app = _build_app(session_maker, monkeypatch)
    transport = ASGITransport(app=app)
    bogus = f"onyx-{uuid.uuid4()}"
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.post(
            "/v1/onyx/documents",
            headers=_headers(bogus),
            files={"file": ("hello.txt", b"x", "text/plain")},
        )
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "kb not found"


# ===========================================================================
# GET /v1/onyx/documents — list
# ===========================================================================


async def _seed_doc(
    session_maker,
    kb_id: str,
    *,
    status: str = "indexed",
    uploaded_at: _dt.datetime | None = None,
    file_name: str | None = None,
) -> uuid.UUID:
    """Insert a Document row with controllable status / uploaded_at."""
    from rag_service.db.models import Document

    doc_id = uuid.uuid4()
    async with session_maker() as s:
        d = Document(
            document_id=doc_id,
            tenant_id=kb_id,
            file_name=file_name or f"{doc_id.hex[:8]}.txt",
            file_size=42,
            content_hash=uuid.uuid4().hex,
            mime_type="text/plain",
            storage_path=f"/tmp/{doc_id}.txt",
            status=status,
        )
        if uploaded_at is not None:
            d.uploaded_at = uploaded_at
        s.add(d)
        await s.commit()
    return doc_id


async def test_list_documents_empty(session_maker, monkeypatch):
    """No docs in this KB → 200 + empty items + null cursor."""
    kb_id = await _seed_kb(session_maker)
    app = _build_app(session_maker, monkeypatch)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get("/v1/onyx/documents", headers=_headers(kb_id))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["items"] == []
    assert body["next_cursor"] is None


async def test_list_documents_filter_by_status(session_maker, monkeypatch):
    """`?status=indexed` returns only indexed rows."""
    kb_id = await _seed_kb(session_maker)
    await _seed_doc(session_maker, kb_id, status="indexed")
    await _seed_doc(session_maker, kb_id, status="pending")

    app = _build_app(session_maker, monkeypatch)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/v1/onyx/documents?status=indexed", headers=_headers(kb_id)
        )
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["status"] == "indexed"


async def test_list_documents_pagination(session_maker, monkeypatch):
    """3 docs with strictly monotonic uploaded_at, limit=2 → 2 + cursor → 1."""
    kb_id = await _seed_kb(session_maker)
    base = _dt.datetime(2026, 5, 4, 12, 0, 0)
    seeded_ids = []
    for i in range(3):
        seeded_ids.append(
            await _seed_doc(
                session_maker,
                kb_id,
                uploaded_at=base + _dt.timedelta(seconds=i),
            )
        )

    app = _build_app(session_maker, monkeypatch)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/v1/onyx/documents?limit=2", headers=_headers(kb_id)
        )
        assert r.status_code == 200, r.text
        page1 = r.json()
        assert len(page1["items"]) == 2
        assert page1["next_cursor"] is not None

        r = await ac.get(
            f"/v1/onyx/documents?limit=2&cursor={page1['next_cursor']}",
            headers=_headers(kb_id),
        )
        assert r.status_code == 200, r.text
        page2 = r.json()
        assert len(page2["items"]) == 1
        assert page2["next_cursor"] is None

    seen = {i["document_id"] for i in page1["items"]} | {
        i["document_id"] for i in page2["items"]
    }
    assert seen == {str(d) for d in seeded_ids}


async def test_list_documents_cross_kb_isolation(session_maker, monkeypatch):
    """Docs in KB-B don't leak into a KB-A list."""
    kb_a = await _seed_kb(session_maker, suffix="A")
    kb_b = await _seed_kb(session_maker, suffix="B")
    doc_a = await _seed_doc(session_maker, kb_a)
    await _seed_doc(session_maker, kb_b)

    app = _build_app(session_maker, monkeypatch)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get("/v1/onyx/documents", headers=_headers(kb_a))
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["document_id"] == str(doc_a)


async def test_list_documents_invalid_cursor_returns_400(session_maker, monkeypatch):
    """A garbage cursor → 400 ``invalid cursor``."""
    kb_id = await _seed_kb(session_maker)
    app = _build_app(session_maker, monkeypatch)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/v1/onyx/documents?cursor=garbage", headers=_headers(kb_id)
        )
    assert r.status_code == 400, r.text


# ===========================================================================
# GET /v1/onyx/documents/{document_id} — read by id
# ===========================================================================


async def test_get_document_returns_full_row(session_maker, monkeypatch):
    """Insert a doc, GET it → 200 with full payload."""
    kb_id = await _seed_kb(session_maker)
    doc_id = await _seed_doc(
        session_maker, kb_id, status="indexed", file_name="a.pdf"
    )
    app = _build_app(session_maker, monkeypatch)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            f"/v1/onyx/documents/{doc_id}", headers=_headers(kb_id)
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["document_id"] == str(doc_id)
    assert body["file_name"] == "a.pdf"
    assert body["status"] == "indexed"
    assert body["mime_type"] == "text/plain"


async def test_get_document_404_for_unknown(session_maker, monkeypatch):
    """Random UUID with no row → 404."""
    kb_id = await _seed_kb(session_maker)
    app = _build_app(session_maker, monkeypatch)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            f"/v1/onyx/documents/{uuid.uuid4()}", headers=_headers(kb_id)
        )
    assert r.status_code == 404, r.text


async def test_get_document_404_for_cross_kb(session_maker, monkeypatch):
    """A doc owned by KB-A is not visible when calling with X-Onyx-KB-Id=KB-B."""
    kb_a = await _seed_kb(session_maker, suffix="A")
    kb_b = await _seed_kb(session_maker, suffix="B")
    doc_a = await _seed_doc(session_maker, kb_a)

    app = _build_app(session_maker, monkeypatch)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            f"/v1/onyx/documents/{doc_a}", headers=_headers(kb_b)
        )
    assert r.status_code == 404, r.text


# ===========================================================================
# DELETE /v1/onyx/documents/{document_id}
# ===========================================================================


async def test_delete_document_returns_204(session_maker, monkeypatch):
    """DELETE → 204; row's status flips to "deleted" in the DB."""
    from rag_service.db.models import Document

    kb_id = await _seed_kb(session_maker)
    doc_id = await _seed_doc(session_maker, kb_id, status="indexed")

    app = _build_app(session_maker, monkeypatch)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.delete(
            f"/v1/onyx/documents/{doc_id}", headers=_headers(kb_id)
        )
    assert r.status_code == 204, r.text

    async with session_maker() as s:
        row = (
            await s.execute(
                select(Document).where(Document.document_id == doc_id)
            )
        ).scalar_one()
        assert row.status == "deleted"


async def test_delete_document_enqueues_rebuild(session_maker, monkeypatch):
    """After DELETE, rebuild is enqueued with the caller's kb_id."""
    from rag_service.api.routers import onyx_documents as onyx_docs_mod

    kb_id = await _seed_kb(session_maker)
    doc_id = await _seed_doc(session_maker, kb_id, status="indexed")

    # Build the app first (which patches enqueue_rebuild to a no-op),
    # *then* override with our recording stub. The recording must
    # happen on the same module attribute the route invokes.
    app = _build_app(session_maker, monkeypatch)
    enqueued: list[str] = []

    async def _record_rebuild(tenant_id: str) -> None:
        enqueued.append(tenant_id)

    monkeypatch.setattr(onyx_docs_mod, "enqueue_rebuild", _record_rebuild)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.delete(
            f"/v1/onyx/documents/{doc_id}", headers=_headers(kb_id)
        )
    assert r.status_code == 204, r.text
    assert enqueued == [kb_id]


async def test_delete_document_404_for_unknown(session_maker, monkeypatch):
    """DELETE a non-existent UUID → 404 + no enqueue."""
    from rag_service.api.routers import onyx_documents as onyx_docs_mod

    kb_id = await _seed_kb(session_maker)
    app = _build_app(session_maker, monkeypatch)

    enqueued: list[str] = []

    async def _record_rebuild(tenant_id: str) -> None:
        enqueued.append(tenant_id)

    monkeypatch.setattr(onyx_docs_mod, "enqueue_rebuild", _record_rebuild)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.delete(
            f"/v1/onyx/documents/{uuid.uuid4()}", headers=_headers(kb_id)
        )
    assert r.status_code == 404, r.text
    assert enqueued == []
