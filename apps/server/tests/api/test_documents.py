"""Tests for ``rag_service.api.routers.documents`` — /v1/documents.

The DB session and the arq enqueue helper are stubbed via
``app.dependency_overrides`` and ``monkeypatch`` so the suite needs no
real Postgres or Redis. We exercise:

* list happy path — 3 seeded docs, default params, all returned;
* status filter — only matching docs returned;
* cursor pagination — limit=2 across 5 docs returns 2+cursor, then
  cursor=... returns the next 2;
* single-doc happy path;
* cross-tenant lookup → 404 (never 403/200);
* DELETE flips status to ``"deleted"`` and calls the rebuild enqueue;
* DELETE on a missing id → 404 and no enqueue.
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
os.environ.setdefault("DATA_DIR", "/tmp/rag_documents_api_test")

import datetime as _dt  # noqa: E402
import uuid  # noqa: E402
from typing import Any  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from rag_service.api.auth import current_tenant, current_user  # noqa: E402
from rag_service.api.deps import get_db_session  # noqa: E402
from rag_service.api.routers import documents as documents_mod  # noqa: E402
from rag_service.db.models import Document  # noqa: E402


class _MockUser:
    """Minimal stand-in for the ``User`` row that ``current_user`` returns."""

    def __init__(self, user_id: uuid.UUID | None = None) -> None:
        self.user_id = user_id or uuid.uuid4()
        self.is_active = True


# ---------------------------------------------------------------------------
# Fake DB session
# ---------------------------------------------------------------------------


class _ScalarsView:
    """Mimic the slice of ``Result.scalars()`` we use: ``.all()``."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)


class _FakeListResult:
    """Mimic ``Result`` for SELECT-many: ``.scalars().all()``."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _ScalarsView:
        return _ScalarsView(self._rows)


class _FakeScalarOneResult:
    """Mimic ``Result`` for SELECT-one: ``.scalar_one_or_none()``."""

    def __init__(self, obj: Any | None) -> None:
        self._obj = obj

    def scalar_one_or_none(self) -> Any | None:
        return self._obj


class _FakeSession:
    """In-memory stand-in for an :class:`AsyncSession`.

    Holds a list of seeded ``Document`` rows. ``execute()`` inspects the
    statement type:

    * ``SELECT`` — returns rows that match ``tenant_id`` (via bound
      params) and the optional ``status`` filter (rendered into the
      compiled SQL). Order/limit/cursor logic is replayed in Python so
      the tests exercise the router's pagination math, not SQLAlchemy's.
    * ``UPDATE`` (DELETE endpoint) — finds the matching row, flips its
      status to ``"deleted"``, and returns its id; or returns ``None``
      when no row matches.
    """

    def __init__(self, rows: list[Document]) -> None:
        self.rows = list(rows)
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None

    async def execute(self, stmt: Any) -> Any:
        # Distinguish by class name so we don't have to import the
        # SQLAlchemy DML/DQL constructor types here.
        cls_name = type(stmt).__name__

        # Bound parameter values give us tenant / id matches without
        # parsing the rendered SQL.
        params = stmt.compile().params
        bound_values = list(params.values())

        if cls_name == "Update":
            # Soft-delete path. Find the unique row matching tenant + id;
            # returning(...) is a single column → scalar_one_or_none.
            # Use named params (``tenant_id_1`` / ``document_id_1``) so we
            # don't confuse the bound string for the new status value with
            # the tenant id.
            tenant_match = params.get("tenant_id_1")
            id_match = params.get("document_id_1")
            for row in self.rows:
                if (
                    row.tenant_id == tenant_match
                    and row.document_id == id_match
                ):
                    row.status = "deleted"
                    return _FakeScalarOneResult(row.document_id)
            return _FakeScalarOneResult(None)

        # SELECT — figure out whether single-row or list. The single-row
        # query filters by ``document_id =`` (no ORDER BY / LIMIT). The
        # list query always has ORDER BY + LIMIT.
        compiled_sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        is_single = "ORDER BY" not in compiled_sql.upper()

        # Tenant filter (every query has one).
        tenant_id = next(
            (v for v in bound_values if isinstance(v, str) and v.startswith("tnt-")),
            None,
        )
        if tenant_id is None:
            # Fallback: pick the first string we see (could be any tenant id).
            tenant_id = next((v for v in bound_values if isinstance(v, str)), None)

        candidates = [r for r in self.rows if r.tenant_id == tenant_id]

        # Single-row GET: filter by document_id too.
        if is_single:
            doc_id = next(
                (v for v in bound_values if isinstance(v, uuid.UUID)),
                None,
            )
            match = next(
                (r for r in candidates if r.document_id == doc_id),
                None,
            )
            return _FakeScalarOneResult(match)

        # List-many: replay status / cursor / limit purely in Python.
        # Status filter: detect by looking at bound string values that
        # aren't a tenant id and aren't an iso timestamp.
        status_values = [
            v
            for v in bound_values
            if isinstance(v, str)
            and v != tenant_id
            and not v.startswith("tnt-")
        ]
        if status_values:
            status_filter = status_values[0]
            candidates = [r for r in candidates if r.status == status_filter]

        # Cursor: a bound datetime + a bound UUID that *isn't* one of the
        # seeded document_ids' tenant. We replicate the strict keyset:
        # uploaded_at < cur_dt OR (uploaded_at == cur_dt AND id < cur_id).
        cur_dt = next(
            (v for v in bound_values if isinstance(v, _dt.datetime)),
            None,
        )
        if cur_dt is not None:
            cur_id = next(
                (v for v in bound_values if isinstance(v, uuid.UUID)),
                None,
            )
            assert cur_id is not None, "cursor decoded a datetime but no uuid"
            candidates = [
                r
                for r in candidates
                if (
                    (r.uploaded_at is not None and r.uploaded_at < cur_dt)
                    or (
                        r.uploaded_at == cur_dt and r.document_id < cur_id
                    )
                )
            ]

        # Sort: uploaded_at DESC, document_id DESC.
        candidates.sort(
            key=lambda r: (r.uploaded_at, r.document_id),
            reverse=True,
        )

        # Limit (the router asks for limit+1 to detect a next page).
        limit_val: int | None = None
        # SQLAlchemy renders LIMIT as an int literal under literal_binds —
        # extract the trailing integer.
        import re

        m = re.search(r"LIMIT\s+(\d+)", compiled_sql)
        if m:
            limit_val = int(m.group(1))
        if limit_val is not None:
            candidates = candidates[:limit_val]

        return _FakeListResult(candidates)


# ---------------------------------------------------------------------------
# App + test-client wiring
# ---------------------------------------------------------------------------


def _make_app(
    fake_session: _FakeSession,
    enqueue_mock: AsyncMock | None = None,
    monkeypatch: pytest.MonkeyPatch | None = None,
    *,
    tenant: str = "tnt-1",
) -> FastAPI:
    """Build a FastAPI app with the documents router and fakes wired in.

    Auth is overridden to a no-op pair: ``current_user`` returns a fresh
    in-memory ``User`` and ``current_tenant`` returns ``tenant``. Tests
    that need cross-tenant behaviour pass a different ``tenant``.
    """
    if enqueue_mock is not None:
        assert monkeypatch is not None, "monkeypatch required to patch enqueue"
        monkeypatch.setattr(documents_mod, "enqueue_rebuild", enqueue_mock)

    app = FastAPI()
    app.include_router(documents_mod.router)

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


def _make_doc(
    tenant_id: str = "tnt-1",
    *,
    uploaded_at: _dt.datetime | None = None,
    status_: str = "indexed",
    **overrides: Any,
) -> Document:
    """Build a fully-populated transient :class:`Document` instance.

    The router serialises ORM rows directly via ``from_attributes``, so
    every field the schema reads must be set — server defaults are not
    applied for un-flushed rows.
    """
    base_time = _dt.datetime(2026, 5, 4, 12, 0, 0, tzinfo=_dt.timezone.utc)
    defaults: dict[str, Any] = {
        "document_id": uuid.uuid4(),
        "tenant_id": tenant_id,
        "file_name": "doc.pdf",
        "file_size": 1234,
        "content_hash": uuid.uuid4().hex,
        "mime_type": "application/pdf",
        "storage_path": "/tmp/x.pdf",
        "status": status_,
        "uploaded_at": uploaded_at or base_time,
        "indexed_at": None,
        "error_message": None,
    }
    defaults.update(overrides)
    return Document(**defaults)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Tests — list
# ---------------------------------------------------------------------------


def test_list_documents_basic():
    """Three seeded docs round-trip through ``GET /v1/documents``."""
    base = _dt.datetime(2026, 5, 4, 12, 0, 0, tzinfo=_dt.timezone.utc)
    docs = [
        _make_doc(uploaded_at=base - _dt.timedelta(seconds=i), file_name=f"d{i}.pdf")
        for i in range(3)
    ]
    session = _FakeSession(rows=docs)
    app = _make_app(session)

    client = TestClient(app)
    r = client.get("/v1/documents")

    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["items"]) == 3
    # All three doc ids surface, regardless of order.
    seen = {item["document_id"] for item in body["items"]}
    assert seen == {str(d.document_id) for d in docs}
    # Only 3 rows < limit (50) → no next cursor.
    assert body["next_cursor"] is None


def test_list_documents_filter_status():
    """``status=indexed`` filters mixed-status seed down to indexed rows."""
    base = _dt.datetime(2026, 5, 4, 12, 0, 0, tzinfo=_dt.timezone.utc)
    indexed_a = _make_doc(uploaded_at=base, status_="indexed", file_name="a.pdf")
    indexed_b = _make_doc(
        uploaded_at=base - _dt.timedelta(seconds=1),
        status_="indexed",
        file_name="b.pdf",
    )
    pending = _make_doc(
        uploaded_at=base - _dt.timedelta(seconds=2),
        status_="pending",
        file_name="c.pdf",
    )
    failed = _make_doc(
        uploaded_at=base - _dt.timedelta(seconds=3),
        status_="failed",
        file_name="d.pdf",
    )
    session = _FakeSession(rows=[indexed_a, indexed_b, pending, failed])
    app = _make_app(session)

    client = TestClient(app)
    r = client.get("/v1/documents", params={"status": "indexed"})

    assert r.status_code == 200, r.text
    body = r.json()
    returned_ids = {item["document_id"] for item in body["items"]}
    assert returned_ids == {str(indexed_a.document_id), str(indexed_b.document_id)}
    for item in body["items"]:
        assert item["status"] == "indexed"


def test_list_documents_cursor_paginates():
    """``limit=2`` over 5 docs returns 2 + cursor; cursor returns next 2."""
    base = _dt.datetime(2026, 5, 4, 12, 0, 0, tzinfo=_dt.timezone.utc)
    # Distinct timestamps so the keyset cursor is unambiguous.
    docs = [
        _make_doc(
            uploaded_at=base - _dt.timedelta(seconds=i),
            file_name=f"d{i}.pdf",
        )
        for i in range(5)
    ]
    session = _FakeSession(rows=docs)
    app = _make_app(session)

    client = TestClient(app)

    # First page.
    r1 = client.get("/v1/documents", params={"limit": 2})
    assert r1.status_code == 200, r1.text
    page1 = r1.json()
    assert len(page1["items"]) == 2
    assert page1["next_cursor"] is not None
    page1_ids = [item["document_id"] for item in page1["items"]]
    # Newest first (DESC by uploaded_at).
    assert page1_ids[0] == str(docs[0].document_id)
    assert page1_ids[1] == str(docs[1].document_id)

    # Second page using the cursor.
    r2 = client.get(
        "/v1/documents",
        params={"limit": 2, "cursor": page1["next_cursor"]},
    )
    assert r2.status_code == 200, r2.text
    page2 = r2.json()
    assert len(page2["items"]) == 2
    page2_ids = [item["document_id"] for item in page2["items"]]
    assert page2_ids[0] == str(docs[2].document_id)
    assert page2_ids[1] == str(docs[3].document_id)
    # No overlap between the two pages.
    assert set(page1_ids).isdisjoint(set(page2_ids))


# ---------------------------------------------------------------------------
# Tests — get single
# ---------------------------------------------------------------------------


def test_get_document_own():
    """A document owned by the caller's tenant is returned with full shape."""
    doc = _make_doc(tenant_id="tnt-1", file_name="hello.pdf")
    session = _FakeSession(rows=[doc])
    app = _make_app(session, tenant="tnt-1")

    client = TestClient(app)
    r = client.get(f"/v1/documents/{doc.document_id}")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["document_id"] == str(doc.document_id)
    assert body["tenant_id"] == "tnt-1"
    assert body["file_name"] == "hello.pdf"
    assert body["status"] == "indexed"
    assert body["mime_type"] == "application/pdf"
    assert body["uploaded_at"].startswith("2026-05-04")


def test_get_document_cross_tenant_404():
    """Document exists for tenant A; tenant B asking for it sees 404."""
    doc = _make_doc(tenant_id="tnt-A")
    session = _FakeSession(rows=[doc])
    app = _make_app(session, tenant="tnt-B")

    client = TestClient(app)
    r = client.get(f"/v1/documents/{doc.document_id}")

    assert r.status_code == 404
    assert r.json() == {"detail": "document not found"}


# ---------------------------------------------------------------------------
# Tests — delete
# ---------------------------------------------------------------------------


def test_delete_document_soft_deletes_and_enqueues(monkeypatch):
    """DELETE flips status to 'deleted' and calls ``enqueue_rebuild`` once."""
    doc = _make_doc(tenant_id="tnt-1", status_="indexed")
    session = _FakeSession(rows=[doc])
    enqueue = AsyncMock(return_value=None)
    app = _make_app(
        session, enqueue_mock=enqueue, monkeypatch=monkeypatch, tenant="tnt-1"
    )

    client = TestClient(app)
    r = client.delete(f"/v1/documents/{doc.document_id}")

    assert r.status_code == 204, r.text
    assert r.content == b""
    # Soft-delete flag flipped on the in-memory row.
    assert doc.status == "deleted"
    # Commit happened before the enqueue.
    assert session.commits == 1
    enqueue.assert_awaited_once_with("tnt-1")


def test_delete_missing_404(monkeypatch):
    """DELETE on a random uuid yields 404 and does not enqueue rebuild."""
    session = _FakeSession(rows=[])
    enqueue = AsyncMock(return_value=None)
    app = _make_app(
        session, enqueue_mock=enqueue, monkeypatch=monkeypatch, tenant="tnt-1"
    )

    client = TestClient(app)
    r = client.delete(f"/v1/documents/{uuid.uuid4()}")

    assert r.status_code == 404
    assert r.json() == {"detail": "document not found"}
    enqueue.assert_not_awaited()
    # No commit on the failed path.
    assert session.commits == 0
