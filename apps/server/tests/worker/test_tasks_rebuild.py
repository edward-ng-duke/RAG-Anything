"""Tests for ``rag_service.worker.tasks.rebuild_index``.

The rebuild task touches three external systems: a per-tenant Redis lock,
the documents/jobs tables, and the on-disk working_dir tree. We exercise the
lock against fakeredis (same as ``test_tasks_ingest``) and stub the DB
helpers with ``AsyncMock`` — the tests are about the rebuild flow, not the
SQL. The filesystem half *is* exercised: ``settings.data_dir`` is pointed
at a ``tmp_path`` so the backup-and-rmtree dance lives on a real (but
disposable) directory tree.
"""

from __future__ import annotations

# Required env vars must be set before importing ``rag_service`` modules.
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
os.environ.setdefault("PARSER_MODE", "default")
os.environ.setdefault("DATA_DIR", "/tmp/rag_tasks_test_data")

import contextlib  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402
from unittest.mock import AsyncMock, MagicMock  # noqa: E402

import fakeredis.aioredis as fake_aioredis  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from rag_service import config as _config  # noqa: E402
from rag_service.worker import tasks as tasks_mod  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def redis_client():
    """Fresh fakeredis client per test."""
    server = fake_aioredis.FakeRedis(decode_responses=True)
    try:
        await server.flushdb()
    except Exception:
        pass
    try:
        yield server
    finally:
        try:
            await server.aclose()
        except AttributeError:
            await server.close()


@pytest.fixture
def tmp_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``settings.data_dir`` at a writable tmp_path for the test.

    The rebuild task computes ``tenant_working_dir`` from ``settings.data_dir``
    via ``rag_service.core.paths``; nudging the singleton's attribute is the
    least invasive way to redirect that path.
    """
    settings = _config.settings  # triggers lazy construction once
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    return tmp_path


class _FakeSession:
    """Minimal stand-in :class:`AsyncSession` for the rebuild tests."""

    def __init__(self) -> None:
        self.executed: list[Any] = []

    async def execute(self, stmt: Any) -> Any:  # pragma: no cover - guard
        self.executed.append(stmt)
        return MagicMock()


@pytest.fixture
def fake_session_factory(monkeypatch: pytest.MonkeyPatch) -> _FakeSession:
    """Patch ``_open_session`` so the task uses an in-memory session."""
    session = _FakeSession()

    @contextlib.asynccontextmanager
    async def _factory():
        yield session

    monkeypatch.setattr(tasks_mod, "_open_session", lambda: _factory())
    return session


@pytest.fixture
def patch_db_helpers(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub the rebuild's DB helpers and surface their state via the dict.

    ``documents`` is an in-memory list[dict] shared by ``_purge_deleted...``
    (which removes ``status=='deleted'`` rows) and ``_list_rebuildable...``
    (which returns the survivors). Tests pre-seed it. The two doc-state
    helpers (`_mark_document_indexed`, `_mark_document_failed`) are
    AsyncMocks so we can assert per-doc outcomes.
    """
    documents: list[dict[str, Any]] = []
    state: dict[str, Any] = {
        "documents": documents,
        "purged": [],  # tenant_ids that were purged
    }

    async def fake_purge(session: Any, tenant_id: str) -> int:
        before = len(documents)
        # Remove in place to mirror DELETE semantics.
        documents[:] = [d for d in documents if d.get("status") != "deleted"]
        removed = before - len(documents)
        state["purged"].append((tenant_id, removed))
        return removed

    async def fake_list(session: Any, tenant_id: str) -> list[dict[str, Any]]:
        # Mirror the production query: only indexed/failed.
        return [
            {"document_id": d["document_id"], "storage_path": d["storage_path"]}
            for d in documents
            if d.get("status") in ("indexed", "failed")
        ]

    state["doc_indexed"] = AsyncMock()
    state["doc_failed"] = AsyncMock()
    state["job_failed"] = AsyncMock()

    monkeypatch.setattr(tasks_mod, "_purge_deleted_documents", fake_purge)
    monkeypatch.setattr(tasks_mod, "_list_rebuildable_documents", fake_list)
    monkeypatch.setattr(tasks_mod, "_mark_document_indexed", state["doc_indexed"])
    monkeypatch.setattr(tasks_mod, "_mark_document_failed", state["doc_failed"])
    monkeypatch.setattr(tasks_mod, "_mark_rebuild_job_failed", state["job_failed"])
    return state


@pytest.fixture
def patch_rag_cache(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch ``rag_factory.get_cache`` to return a fake cache.

    Records ``evict(tenant_id)`` calls so tests can assert the eviction
    happened before the rebuild ran. Each ``.get()`` returns the same
    ``fake_rag`` so tests can preset side_effects on
    ``process_document_complete``.
    """
    fake_rag = MagicMock(name="FakeRAGAnything")
    fake_rag.process_document_complete = AsyncMock()

    fake_cache = MagicMock(name="FakeRAGAnythingCache")
    fake_cache.get = AsyncMock(return_value=fake_rag)
    fake_cache.evict = AsyncMock()

    monkeypatch.setattr(tasks_mod.rag_factory, "get_cache", lambda: fake_cache)
    fake_cache._rag = fake_rag
    return fake_cache


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_working_dir(data_dir: Path, tenant_id: str) -> Path:
    """Create a non-empty working_dir for ``tenant_id`` and return its path."""
    wd = data_dir / "working_dirs" / tenant_id
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "marker.txt").write_text("original")
    return wd


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_rebuild_after_deletion_keeps_others(
    redis_client,
    tmp_data_dir,
    fake_session_factory,
    patch_db_helpers,
    patch_rag_cache,
):
    """3 indexed docs, one soft-deleted -> deleted purged, others reprocessed."""
    _seed_working_dir(tmp_data_dir, "tenant-A")

    patch_db_helpers["documents"].extend(
        [
            {"document_id": "doc-1", "storage_path": "/x/1.pdf", "status": "indexed"},
            {"document_id": "doc-2", "storage_path": "/x/2.pdf", "status": "deleted"},
            {"document_id": "doc-3", "storage_path": "/x/3.pdf", "status": "indexed"},
        ]
    )

    ctx = {"redis": redis_client}
    result = await tasks_mod.rebuild_index(ctx, "tenant-A")

    assert result["status"] == "rebuilt"
    assert result["indexed"] == 2
    assert result["failed"] == 0
    assert result["purged"] == 1

    # The deleted row is gone from the in-memory snapshot.
    surviving_ids = {d["document_id"] for d in patch_db_helpers["documents"]}
    assert surviving_ids == {"doc-1", "doc-3"}

    # The two survivors got re-processed exactly once each.
    assert (
        patch_rag_cache._rag.process_document_complete.await_count == 2
    )
    indexed_ids = [
        call.kwargs["document_id"]
        if "document_id" in call.kwargs
        else None
        for call in patch_db_helpers["doc_indexed"].await_args_list
    ]
    # _mark_document_indexed receives positional args (session, tenant, doc_id)
    positional_ids = [
        call.args[2] for call in patch_db_helpers["doc_indexed"].await_args_list
    ]
    seen = set(indexed_ids) | set(positional_ids)
    seen.discard(None)
    assert seen == {"doc-1", "doc-3"}

    # Cache eviction happened.
    patch_rag_cache.evict.assert_awaited_once_with("tenant-A")
    # Backup directory cleaned up on happy path.
    assert not (tmp_data_dir / "working_dirs" / "tenant-A.bak").exists()


async def test_existing_bak_skips_backup(
    redis_client,
    tmp_data_dir,
    fake_session_factory,
    patch_db_helpers,
    patch_rag_cache,
):
    """A pre-existing .bak dir is treated as a resume — backup step skipped."""
    # Seed a .bak from a prior aborted run, plus a (possibly partial) new
    # working_dir from that prior attempt.
    bak_dir = tmp_data_dir / "working_dirs" / "tenant-A.bak"
    bak_dir.mkdir(parents=True, exist_ok=True)
    (bak_dir / "old.txt").write_text("from-prior-run")
    bak_mtime_before = bak_dir.stat().st_mtime_ns

    new_wd = tmp_data_dir / "working_dirs" / "tenant-A"
    new_wd.mkdir(parents=True, exist_ok=True)
    (new_wd / "in_progress.txt").write_text("partial")

    # No documents — keep the test focused on the backup-skip behaviour.
    patch_db_helpers["documents"].clear()

    # Make process_document_complete fail so the rebuild aborts BEFORE the
    # happy-path rmtree of bak_dir; that lets us assert "untouched". Easier:
    # we use zero documents so the loop is a no-op; on success the .bak is
    # removed. Test the *skip backup* claim by checking bak contents are
    # the original ones (i.e. NOT overwritten by the new working_dir's
    # in-progress.txt).
    ctx = {"redis": redis_client}

    # Patch _remove_dir_safely on the module so we can observe whether the
    # post-success cleanup ran without actually nuking our seeded bak_dir
    # — we want to inspect it after the call.
    removed: list[Path] = []

    def _spy_remove(path: Path) -> None:
        removed.append(Path(path))

    import rag_service.worker.tasks as tasks_module

    original_remove = tasks_module._remove_dir_safely
    tasks_module._remove_dir_safely = _spy_remove
    try:
        result = await tasks_mod.rebuild_index(ctx, "tenant-A")
    finally:
        tasks_module._remove_dir_safely = original_remove

    assert result["status"] == "rebuilt"

    # The original .bak survives unchanged: same mtime, same contents.
    assert bak_dir.exists()
    assert (bak_dir / "old.txt").read_text() == "from-prior-run"
    # NO new file from the in-progress working_dir leaked into bak
    # (which would have happened if the backup step ran a second time).
    assert not (bak_dir / "in_progress.txt").exists()
    assert bak_dir.stat().st_mtime_ns == bak_mtime_before

    # The post-success cleanup tried to remove the bak_dir (we stubbed it).
    assert removed and removed[-1] == bak_dir


async def test_per_doc_failure_continues_with_rest(
    redis_client,
    tmp_data_dir,
    fake_session_factory,
    patch_db_helpers,
    patch_rag_cache,
):
    """A per-document failure marks that doc failed and continues with others."""
    _seed_working_dir(tmp_data_dir, "tenant-A")

    patch_db_helpers["documents"].extend(
        [
            {"document_id": "doc-1", "storage_path": "/x/1.pdf", "status": "indexed"},
            {"document_id": "doc-2", "storage_path": "/x/2.pdf", "status": "indexed"},
        ]
    )

    # First call fails (doc-1), second succeeds (doc-2).
    side_effects: list[Any] = [RuntimeError("boom on doc-1"), None]
    patch_rag_cache._rag.process_document_complete = AsyncMock(
        side_effect=side_effects
    )

    ctx = {"redis": redis_client}
    result = await tasks_mod.rebuild_index(ctx, "tenant-A")

    assert result["status"] == "rebuilt"
    assert result["indexed"] == 1
    assert result["failed"] == 1

    # doc-1 marked failed exactly once.
    assert patch_db_helpers["doc_failed"].await_count == 1
    failed_call = patch_db_helpers["doc_failed"].await_args
    # _mark_document_failed signature: (session, tenant, doc_id, error_message=...)
    assert failed_call.args[2] == "doc-1"
    err = failed_call.kwargs.get("error_message") or failed_call.args[3]
    assert "boom" in err

    # doc-2 marked indexed exactly once.
    assert patch_db_helpers["doc_indexed"].await_count == 1
    indexed_call = patch_db_helpers["doc_indexed"].await_args
    assert indexed_call.args[2] == "doc-2"

    # Rebuild job NOT marked failed — partial failures are tolerated.
    patch_db_helpers["job_failed"].assert_not_awaited()

    # Backup cleaned up on overall success.
    assert not (tmp_data_dir / "working_dirs" / "tenant-A.bak").exists()


async def test_rebuild_clean_removes_bak_on_success(
    redis_client,
    tmp_data_dir,
    fake_session_factory,
    patch_db_helpers,
    patch_rag_cache,
):
    """Happy path with documents present: bak_dir removed after success."""
    _seed_working_dir(tmp_data_dir, "tenant-A")
    patch_db_helpers["documents"].extend(
        [
            {"document_id": "doc-1", "storage_path": "/x/1.pdf", "status": "indexed"},
        ]
    )

    bak_dir = tmp_data_dir / "working_dirs" / "tenant-A.bak"
    # Sanity: bak_dir does NOT exist yet — the rebuild should create it.
    assert not bak_dir.exists()

    ctx = {"redis": redis_client}
    result = await tasks_mod.rebuild_index(ctx, "tenant-A")

    assert result["status"] == "rebuilt"
    assert result["indexed"] == 1
    assert result["failed"] == 0

    # Backup is gone.
    assert not bak_dir.exists()
    # The fresh working_dir exists (recreated).
    assert (tmp_data_dir / "working_dirs" / "tenant-A").exists()
    # Cache was evicted before the new build.
    patch_rag_cache.evict.assert_awaited_once_with("tenant-A")
