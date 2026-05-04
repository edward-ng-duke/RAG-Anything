"""Tests for ``rag_service.db.repositories.tenants`` — onyx KB CRUD.

Mirrors the in-memory SQLite engine pattern from
``tests/api/test_auth_onyx.py``: we patch the ``JSONB`` / ``UUID`` /
identity / server-default columns so ``Base.metadata.create_all`` works
on SQLite, then exercise the four repo entry-points end-to-end.

Coverage matrix:

* ``create_onyx_kb`` — id-prefix, workspace/owner persistence, default
  + custom quota.
* ``list_onyx_kbs`` — empty DB, source filter, workspace + owner filters,
  pagination, ``limit`` clamp.
* ``get_onyx_kb`` — full aggregate dict, deleted-doc exclusion, missing
  tenant, non-onyx tenant.
* ``delete_kb_cascade`` — tenant row removal, child document/job rows
  removed, return value on missing tenant, on-disk upload dir cleanup,
  graceful handling of missing ``lightrag.*`` schema (SQLite test env).
"""

from __future__ import annotations

# ``conftest.py`` at ``tests/`` already populated the env vars. Override
# ``DATA_DIR`` locally so a stray Path resolution doesn't pollute another
# test's dir.
import os  # noqa: E402

os.environ.setdefault("DATA_DIR", "/tmp/rag_db_repo_tenants_test")

import datetime as _dt  # noqa: E402
import re  # noqa: E402
import uuid  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB, UUID  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.ext.compiler import compiles  # noqa: E402
from sqlalchemy.schema import ColumnDefault  # noqa: E402


# ---------------------------------------------------------------------------
# PG → SQLite schema patches (mirrors tests/api/test_auth_onyx.py)
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
# Per-test SQLite engine + session
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


@pytest.fixture
async def db(session_maker):
    """Yield one fresh ``AsyncSession`` per test (auto-commit, rollback on err)."""
    async with session_maker() as s:
        try:
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            raise


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_MB = 1024 * 1024


async def _seed_tenant(
    db,
    *,
    tenant_id: str,
    config_json: dict | None,
    storage_quota_mb: int = 1024,
    display_name: str | None = None,
    created_at: _dt.datetime | None = None,
) -> None:
    from rag_service.db.models import Tenant

    db.add(
        Tenant(
            tenant_id=tenant_id,
            display_name=display_name or tenant_id,
            config_json=config_json,
            storage_quota_mb=storage_quota_mb,
            created_at=created_at,
        )
    )
    await db.commit()


async def _seed_document(
    db,
    *,
    tenant_id: str,
    file_size: int,
    status_: str = "indexed",
    file_name: str | None = None,
) -> None:
    from rag_service.db.models import Document

    db.add(
        Document(
            document_id=uuid.uuid4(),
            tenant_id=tenant_id,
            file_name=file_name or f"{uuid.uuid4().hex[:6]}.pdf",
            file_size=file_size,
            content_hash=uuid.uuid4().hex,
            mime_type="application/pdf",
            storage_path=f"/tmp/{uuid.uuid4().hex}.pdf",
            status=status_,
        )
    )
    await db.commit()


async def _seed_job(db, *, tenant_id: str) -> None:
    from rag_service.db.models import Job

    db.add(
        Job(
            job_id=uuid.uuid4(),
            tenant_id=tenant_id,
            job_type="ingest",
            status="queued",
        )
    )
    await db.commit()


# ===========================================================================
# create_onyx_kb
# ===========================================================================


async def test_create_onyx_kb_inserts_row_with_onyx_prefix(db):
    from rag_service.db.repositories.tenants import create_onyx_kb

    t = await create_onyx_kb(db, display_name="X")
    await db.commit()

    assert re.fullmatch(r"onyx-[a-f0-9-]{36}", t.tenant_id), t.tenant_id
    assert t.display_name == "X"
    assert (t.config_json or {}).get("source") == "onyx"


async def test_create_onyx_kb_persists_workspace_owner(db):
    from rag_service.db.repositories.tenants import create_onyx_kb

    t = await create_onyx_kb(
        db,
        display_name="X",
        onyx_workspace_id="ws_42",
        onyx_owner_user_id="u_alice",
    )
    await db.commit()

    cfg = t.config_json or {}
    assert cfg.get("onyx_workspace_id") == "ws_42"
    assert cfg.get("onyx_owner_user_id") == "u_alice"


async def test_create_onyx_kb_default_quota_1024(db):
    from rag_service.db.repositories.tenants import create_onyx_kb

    t = await create_onyx_kb(db, display_name="X")
    await db.commit()
    assert t.storage_quota_mb == 1024


async def test_create_onyx_kb_custom_quota(db):
    from rag_service.db.repositories.tenants import create_onyx_kb

    t = await create_onyx_kb(db, display_name="X", storage_quota_mb=2048)
    await db.commit()
    assert t.storage_quota_mb == 2048


# ===========================================================================
# list_onyx_kbs
# ===========================================================================


async def test_list_onyx_kbs_empty(db):
    from rag_service.db.repositories.tenants import list_onyx_kbs

    rows, cursor = await list_onyx_kbs(db)
    assert rows == []
    assert cursor is None


async def test_list_onyx_kbs_filters_by_source_onyx(db):
    from rag_service.db.repositories.tenants import (
        create_onyx_kb,
        list_onyx_kbs,
    )

    await create_onyx_kb(db, display_name="onyx-one")
    await db.commit()
    await _seed_tenant(
        db, tenant_id="tnt-standalone", config_json={"source": "standalone"}
    )

    rows, cursor = await list_onyx_kbs(db)
    assert len(rows) == 1
    assert rows[0].tenant_id.startswith("onyx-")
    assert cursor is None


async def test_list_onyx_kbs_filters_by_workspace(db):
    from rag_service.db.repositories.tenants import (
        create_onyx_kb,
        list_onyx_kbs,
    )

    await create_onyx_kb(db, display_name="A", onyx_workspace_id="ws_a")
    await create_onyx_kb(db, display_name="B", onyx_workspace_id="ws_b")
    await db.commit()

    rows, _ = await list_onyx_kbs(db, onyx_workspace_id="ws_a")
    assert len(rows) == 1
    assert rows[0].display_name == "A"


async def test_list_onyx_kbs_filters_by_owner(db):
    from rag_service.db.repositories.tenants import (
        create_onyx_kb,
        list_onyx_kbs,
    )

    await create_onyx_kb(db, display_name="A", onyx_owner_user_id="u_alice")
    await create_onyx_kb(db, display_name="B", onyx_owner_user_id="u_bob")
    await db.commit()

    rows, _ = await list_onyx_kbs(db, onyx_owner_user_id="u_alice")
    assert len(rows) == 1
    assert rows[0].display_name == "A"


async def test_list_onyx_kbs_pagination(db):
    from rag_service.db.repositories.tenants import list_onyx_kbs

    # Stagger created_at so ordering is deterministic — SQLite's
    # ``CURRENT_TIMESTAMP`` only has 1-second resolution which trips
    # tie-breakers when 5 inserts land in the same wall-clock second.
    base = _dt.datetime(2026, 5, 4, 12, 0, 0, tzinfo=_dt.timezone.utc)
    for i in range(5):
        await _seed_tenant(
            db,
            tenant_id=f"onyx-{i:032x}",
            config_json={"source": "onyx"},
            created_at=base + _dt.timedelta(seconds=i),
        )

    page1, c1 = await list_onyx_kbs(db, limit=2)
    assert len(page1) == 2
    assert c1 is not None

    page2, c2 = await list_onyx_kbs(db, cursor=c1, limit=2)
    assert len(page2) == 2
    assert c2 is not None

    page3, c3 = await list_onyx_kbs(db, cursor=c2, limit=2)
    assert len(page3) == 1
    assert c3 is None

    # Pages must be disjoint and together cover all 5 rows.
    seen = {r.tenant_id for r in page1 + page2 + page3}
    assert len(seen) == 5


async def test_list_onyx_kbs_limit_clamp(db):
    from rag_service.db.repositories.tenants import (
        create_onyx_kb,
        list_onyx_kbs,
    )

    await create_onyx_kb(db, display_name="X")
    await db.commit()

    # 300 → clamped to 200 internally; call should not raise.
    rows, _ = await list_onyx_kbs(db, limit=300)
    assert len(rows) == 1


# ===========================================================================
# get_onyx_kb
# ===========================================================================


async def test_get_onyx_kb_returns_full_dict(db):
    from rag_service.db.repositories.tenants import (
        create_onyx_kb,
        get_onyx_kb,
    )

    t = await create_onyx_kb(
        db,
        display_name="KB",
        onyx_workspace_id="ws_1",
        onyx_owner_user_id="u_x",
    )
    await db.commit()
    await _seed_document(db, tenant_id=t.tenant_id, file_size=1 * _MB)
    await _seed_document(db, tenant_id=t.tenant_id, file_size=2 * _MB)

    info = await get_onyx_kb(db, t.tenant_id)
    assert info is not None
    assert info["tenant_id"] == t.tenant_id
    assert info["display_name"] == "KB"
    assert info["storage_quota_mb"] == 1024
    assert info["onyx_workspace_id"] == "ws_1"
    assert info["onyx_owner_user_id"] == "u_x"
    assert info["document_count"] == 2
    assert info["storage_used_mb"] == pytest.approx(3.0, abs=0.01)


async def test_get_onyx_kb_excludes_soft_deleted(db):
    from rag_service.db.repositories.tenants import (
        create_onyx_kb,
        get_onyx_kb,
    )

    t = await create_onyx_kb(db, display_name="KB")
    await db.commit()
    await _seed_document(db, tenant_id=t.tenant_id, file_size=1 * _MB)
    await _seed_document(
        db, tenant_id=t.tenant_id, file_size=10 * _MB, status_="deleted"
    )

    info = await get_onyx_kb(db, t.tenant_id)
    assert info is not None
    assert info["document_count"] == 1
    assert info["storage_used_mb"] == pytest.approx(1.0, abs=0.01)


async def test_get_onyx_kb_returns_none_for_unknown(db):
    from rag_service.db.repositories.tenants import get_onyx_kb

    assert await get_onyx_kb(db, "onyx-xxx") is None


async def test_get_onyx_kb_returns_none_for_non_onyx_source(db):
    from rag_service.db.repositories.tenants import get_onyx_kb

    await _seed_tenant(
        db, tenant_id="tnt-other", config_json={"source": "other"}
    )
    assert await get_onyx_kb(db, "tnt-other") is None


# ===========================================================================
# delete_kb_cascade
# ===========================================================================


async def test_delete_kb_cascade_removes_tenant_row(db, tmp_path):
    from sqlalchemy import select

    from rag_service.db.models import Tenant
    from rag_service.db.repositories.tenants import (
        create_onyx_kb,
        delete_kb_cascade,
    )

    t = await create_onyx_kb(db, display_name="KB")
    await db.commit()

    ok = await delete_kb_cascade(db, t.tenant_id, data_dir=tmp_path)
    await db.commit()
    assert ok is True

    row = (
        await db.execute(select(Tenant).where(Tenant.tenant_id == t.tenant_id))
    ).scalar_one_or_none()
    assert row is None


async def test_delete_kb_cascade_removes_documents(db, tmp_path):
    from sqlalchemy import func, select

    from rag_service.db.models import Document
    from rag_service.db.repositories.tenants import (
        create_onyx_kb,
        delete_kb_cascade,
    )

    t = await create_onyx_kb(db, display_name="KB")
    await db.commit()
    await _seed_document(db, tenant_id=t.tenant_id, file_size=1 * _MB)
    await _seed_document(db, tenant_id=t.tenant_id, file_size=2 * _MB)

    await delete_kb_cascade(db, t.tenant_id, data_dir=tmp_path)
    await db.commit()

    n = (
        await db.execute(
            select(func.count(Document.document_id)).where(
                Document.tenant_id == t.tenant_id
            )
        )
    ).scalar_one()
    assert n == 0


async def test_delete_kb_cascade_removes_jobs(db, tmp_path):
    from sqlalchemy import func, select

    from rag_service.db.models import Job
    from rag_service.db.repositories.tenants import (
        create_onyx_kb,
        delete_kb_cascade,
    )

    t = await create_onyx_kb(db, display_name="KB")
    await db.commit()
    await _seed_job(db, tenant_id=t.tenant_id)

    await delete_kb_cascade(db, t.tenant_id, data_dir=tmp_path)
    await db.commit()

    n = (
        await db.execute(
            select(func.count(Job.job_id)).where(Job.tenant_id == t.tenant_id)
        )
    ).scalar_one()
    assert n == 0


async def test_delete_kb_cascade_returns_false_for_unknown_tenant(db, tmp_path):
    from rag_service.db.repositories.tenants import delete_kb_cascade

    ok = await delete_kb_cascade(db, "onyx-does-not-exist", data_dir=tmp_path)
    assert ok is False


async def test_delete_kb_cascade_removes_upload_dir(db, tmp_path):
    from rag_service.db.repositories.tenants import (
        create_onyx_kb,
        delete_kb_cascade,
    )

    t = await create_onyx_kb(db, display_name="KB")
    await db.commit()

    upload_dir = tmp_path / "uploads" / t.tenant_id
    upload_dir.mkdir(parents=True)
    (upload_dir / "somefile").write_text("hello")

    await delete_kb_cascade(db, t.tenant_id, data_dir=tmp_path)
    await db.commit()

    assert not upload_dir.exists()


async def test_delete_kb_cascade_lightrag_tables_skipped_in_sqlite(db, tmp_path):
    """The 11 ``lightrag.*`` deletes are best-effort — missing tables in
    the SQLite test env must not crash the cascade."""
    from rag_service.db.repositories.tenants import (
        create_onyx_kb,
        delete_kb_cascade,
    )

    t = await create_onyx_kb(db, display_name="KB")
    await db.commit()

    ok = await delete_kb_cascade(db, t.tenant_id, data_dir=tmp_path)
    await db.commit()
    assert ok is True
