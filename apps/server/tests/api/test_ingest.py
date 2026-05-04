"""Tests for ``rag_service.api.routers.ingest`` — POST /v1/ingest.

The DB and the arq enqueue helper are stubbed via ``app.dependency_overrides``
and ``monkeypatch`` so the tests don't need Postgres or Redis. We exercise:

* happy path with a tiny in-memory PDF (``%PDF-1.4...``);
* size enforcement when ``max_upload_mb`` is monkey-patched very low;
* dedup behaviour when a row with the same content_hash already exists;
* MIME rejection for HTML payloads (``<html>...``);
* missing-Authorization-header → 401 (no auth override applied);
* missing-active-tenant → 400 (current_tenant missing the JWT ``tenant`` claim).

The auth deps (``current_user`` / ``current_tenant``) are overridden
directly with mock-yielding helpers — no JWT decode happens — so the
tests focus on the router/DB contract rather than re-litigating
``test_auth_basic.py``.
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
os.environ.setdefault("DATA_DIR", "/tmp/rag_ingest_api_test")

import io  # noqa: E402
import uuid  # noqa: E402
from typing import Any  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from rag_service.api.auth import current_tenant, current_user  # noqa: E402
from rag_service.api.deps import get_db_session  # noqa: E402
from rag_service.api.routers import ingest as ingest_mod  # noqa: E402


# ---------------------------------------------------------------------------
# Fake DB session
# ---------------------------------------------------------------------------


class _FakeResult:
    """Mimic the slice of ``Result`` we use: ``.first()``."""

    def __init__(self, row: Any | None) -> None:
        self._row = row

    def first(self) -> Any | None:
        return self._row


class _FakeSession:
    """Tiny stand-in for an :class:`AsyncSession`.

    Records every ``.add()``-ed object and answers ``execute()`` with a
    pre-seeded "existing document" row when the SELECT carries a matching
    ``content_hash`` — otherwise returns an empty result. This is enough
    to exercise both the fresh-upload and dedup branches.
    """

    def __init__(self, dedup_for_hash: str | None = None, dedup_doc_id: uuid.UUID | None = None) -> None:
        self.added: list[Any] = []
        self._dedup_for_hash = dedup_for_hash
        self._dedup_doc_id = dedup_doc_id
        self.commits = 0

    def add(self, obj: Any) -> None:
        # Mimic the server-default for primary keys so the router can read
        # ``job.job_id`` after ``flush()``.
        if hasattr(obj, "job_id") and getattr(obj, "job_id", None) is None:
            obj.job_id = uuid.uuid4()
        self.added.append(obj)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None

    async def execute(self, stmt: Any) -> _FakeResult:
        # Inspect the compiled SQL crudely: the dedup query filters on
        # ``content_hash``. If we have a seeded hash AND the rendered SQL
        # references it, return a hit.
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        if (
            self._dedup_for_hash is not None
            and self._dedup_for_hash in compiled
        ):
            return _FakeResult((self._dedup_doc_id,))
        return _FakeResult(None)


# ---------------------------------------------------------------------------
# App + test-client wiring
# ---------------------------------------------------------------------------


class _MockUser:
    """Minimal stand-in for the ``User`` ORM row that ``current_user`` returns."""

    def __init__(self, user_id: uuid.UUID | None = None) -> None:
        self.user_id = user_id or uuid.uuid4()
        self.is_active = True


def _make_app(
    fake_session: _FakeSession,
    enqueue_mock: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    *,
    tenant: str | None = "tnt-1",
    skip_auth_overrides: bool = False,
) -> FastAPI:
    """Build a FastAPI app with the ingest router and fakes wired in.

    ``tenant=None`` simulates an authenticated user with no active tenant
    selected — ``current_tenant`` raises 400. ``skip_auth_overrides=True``
    leaves the real JWT-based deps in place so the missing-auth case is
    exercised against the production code path.
    """
    monkeypatch.setattr(ingest_mod, "enqueue_ingest", enqueue_mock)

    app = FastAPI()
    app.include_router(ingest_mod.router)

    async def _db_override():
        # Mimic the get_db_session contract: yield a session, rely on
        # caller's commit semantics.
        try:
            yield fake_session
        except Exception:
            await fake_session.rollback()
            raise

    app.dependency_overrides[get_db_session] = _db_override

    if not skip_auth_overrides:
        async def _user_override() -> _MockUser:
            return _MockUser()

        async def _tenant_override() -> str:
            if tenant is None:
                from fastapi import HTTPException, status

                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "no active tenant; call select_tenant",
                )
            return tenant

        app.dependency_overrides[current_user] = _user_override
        app.dependency_overrides[current_tenant] = _tenant_override

    return app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    """Point ``settings.data_dir`` at a tmp path.

    Auth is exercised via dependency overrides on ``current_user`` /
    ``current_tenant`` rather than a real bearer token, so the legacy
    ``settings.internal_token`` pin is no longer needed.
    """
    from rag_service.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_ingest_valid_pdf(tmp_data_dir, monkeypatch):
    """Tiny PDF round-trips: 200 + deduplicated=False + enqueue called once."""
    session = _FakeSession()
    enqueue = AsyncMock(return_value=None)
    app = _make_app(session, enqueue, monkeypatch)

    pdf_bytes = b"%PDF-1.4\n%%EOF\n"
    client = TestClient(app)
    r = client.post(
        "/v1/ingest",
        files={"file": ("hello.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "queued"
    assert body["deduplicated"] is False
    # Both UUIDs must parse.
    uuid.UUID(body["document_id"])
    uuid.UUID(body["job_id"])
    enqueue.assert_awaited_once()
    args = enqueue.await_args.args
    assert args[0] == "tnt-1"
    assert args[1] == body["document_id"]
    # Two rows added: document + job.
    assert len(session.added) == 2


def test_ingest_oversized_413(tmp_data_dir, monkeypatch):
    """When the body exceeds max_upload_mb, the file is removed and 413 is returned."""
    from rag_service.config import settings

    # 1 MiB cap → a 2 MiB upload trips it.
    monkeypatch.setattr(settings, "max_upload_mb", 1)

    session = _FakeSession()
    enqueue = AsyncMock(return_value=None)
    app = _make_app(session, enqueue, monkeypatch)

    big = b"%PDF-1.4\n" + (b"a" * (2 * 1024 * 1024))
    client = TestClient(app)
    r = client.post(
        "/v1/ingest",
        files={"file": ("big.pdf", io.BytesIO(big), "application/pdf")},
    )

    assert r.status_code == 413
    enqueue.assert_not_awaited()
    # No DB rows should have been added on the rejected path.
    assert session.added == []
    # The tmp upload should have been cleaned up.
    uploads = list((tmp_data_dir / "uploads" / "tnt-1").glob("*"))
    assert uploads == []


def test_ingest_dedup_returns_existing(tmp_data_dir, monkeypatch):
    """A second upload of identical bytes returns deduplicated=True with the existing id."""
    import hashlib

    pdf_bytes = b"%PDF-1.4\n%%EOF\n"
    existing_doc_id = uuid.uuid4()
    content_hash = hashlib.sha256(pdf_bytes).hexdigest()

    session = _FakeSession(dedup_for_hash=content_hash, dedup_doc_id=existing_doc_id)
    enqueue = AsyncMock(return_value=None)
    app = _make_app(session, enqueue, monkeypatch)

    client = TestClient(app)
    r = client.post(
        "/v1/ingest",
        files={"file": ("dup.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deduplicated"] is True
    assert body["status"] == "dedup"
    assert body["document_id"] == str(existing_doc_id)
    enqueue.assert_not_awaited()
    # No new rows on the dedup branch.
    assert session.added == []
    # The on-disk file should have been removed.
    uploads = list((tmp_data_dir / "uploads" / "tnt-1").glob("*"))
    assert uploads == []


def test_ingest_bad_mime_415(tmp_data_dir, monkeypatch):
    """An HTML payload doesn't match any allow-listed magic bytes → 415."""
    session = _FakeSession()
    enqueue = AsyncMock(return_value=None)
    app = _make_app(session, enqueue, monkeypatch)

    # HTML, but with a non-printable byte to defeat the plain-text fallback.
    html = b"<html>\x00<body>nope</body></html>"
    client = TestClient(app)
    r = client.post(
        "/v1/ingest",
        files={"file": ("evil.html", io.BytesIO(html), "text/html")},
    )

    assert r.status_code == 415
    enqueue.assert_not_awaited()
    assert session.added == []
    uploads = list((tmp_data_dir / "uploads" / "tnt-1").glob("*"))
    assert uploads == []


def test_ingest_missing_auth_401(tmp_data_dir, monkeypatch):
    """No Authorization header → 401 from ``current_user``.

    Skips the auth dep overrides so the production JWT-aware ``current_user``
    runs against the request and rejects it for the missing bearer.
    """
    session = _FakeSession()
    enqueue = AsyncMock(return_value=None)
    app = _make_app(session, enqueue, monkeypatch, skip_auth_overrides=True)

    client = TestClient(app)
    r = client.post(
        "/v1/ingest",
        files={"file": ("x.pdf", io.BytesIO(b"%PDF-1.4\n%%EOF\n"), "application/pdf")},
    )
    assert r.status_code == 401
    enqueue.assert_not_awaited()


def test_ingest_missing_tenant_400(tmp_data_dir, monkeypatch):
    """Authenticated user with no active tenant claim → 400 from ``current_tenant``."""
    session = _FakeSession()
    enqueue = AsyncMock(return_value=None)
    app = _make_app(session, enqueue, monkeypatch, tenant=None)

    client = TestClient(app)
    r = client.post(
        "/v1/ingest",
        files={"file": ("x.pdf", io.BytesIO(b"%PDF-1.4\n%%EOF\n"), "application/pdf")},
    )
    assert r.status_code == 400
    enqueue.assert_not_awaited()
