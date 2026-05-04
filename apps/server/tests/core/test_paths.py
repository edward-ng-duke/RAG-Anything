"""Tests for ``rag_service.core.paths``: tenant_id validation + path safety."""

from __future__ import annotations

# Set required env vars before any rag_service import so the lazy
# ``settings`` singleton can be constructed without a real .env file.
import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x@x/x")
os.environ.setdefault("REDIS_URL", "redis://x")
os.environ.setdefault("INTERNAL_TOKEN", "x" * 64)
os.environ.setdefault("LLM_BASE_URL", "x")
os.environ.setdefault("LLM_API_KEY", "x")
os.environ.setdefault("LLM_MODEL", "x")
os.environ.setdefault("EMBEDDING_BASE_URL", "x")
os.environ.setdefault("EMBEDDING_API_KEY", "x")
os.environ.setdefault("EMBEDDING_MODEL", "x")

from pathlib import Path  # noqa: E402
from uuid import UUID, uuid4  # noqa: E402

import pytest  # noqa: E402

from rag_service.config import settings  # noqa: E402
from rag_service.core.paths import (  # noqa: E402
    InvalidTenantIdError,
    document_upload_path,
    tenant_upload_dir,
    tenant_working_dir,
    validate_tenant_id,
)


@pytest.mark.parametrize(
    "tid",
    [
        "acme",
        "a",
        "_",
        "a-b_c-1",
        "A1",
        "a" * 64,
    ],
)
def test_valid_tenant_ids(tid: str) -> None:
    assert validate_tenant_id(tid) == tid


@pytest.mark.parametrize(
    "tid",
    [
        "",
        "a" * 65,
        "acme/foo",
        "../etc",
        "a b",
        "foo;bar",
        ".",
        "..",
        None,
    ],
)
def test_invalid_tenant_ids(tid: object) -> None:
    with pytest.raises(InvalidTenantIdError):
        validate_tenant_id(tid)  # type: ignore[arg-type]


def _data_root() -> Path:
    return Path(settings.data_dir).resolve()


def test_tenant_upload_dir_under_data_root() -> None:
    p = tenant_upload_dir("acme")
    assert p.is_relative_to(_data_root())
    assert p.name == "acme"
    assert p.parent.name == "uploads"


def test_tenant_working_dir_under_data_root() -> None:
    p = tenant_working_dir("acme")
    assert p.is_relative_to(_data_root())
    assert p.name == "acme"
    assert p.parent.name == "working_dirs"


def test_document_upload_path_basic() -> None:
    doc_id = uuid4()
    p = document_upload_path("acme", doc_id, "pdf")
    assert p.is_relative_to(_data_root())
    assert p.name == f"{doc_id}.pdf"
    assert p.parent == tenant_upload_dir("acme")


def test_document_upload_path_strips_leading_dot_and_lowercases() -> None:
    doc_id = uuid4()
    p = document_upload_path("acme", doc_id, ".PDF")
    assert p.name == f"{doc_id}.pdf"


@pytest.mark.parametrize("ext", ["../sh", "a" * 9, "", "pdf!", "p df", ".."])
def test_document_upload_path_invalid_ext(ext: str) -> None:
    with pytest.raises(ValueError):
        document_upload_path("acme", uuid4(), ext)


def test_document_upload_path_uuid_required() -> None:
    with pytest.raises(TypeError):
        document_upload_path("acme", "not-a-uuid", "pdf")  # type: ignore[arg-type]


def test_document_upload_path_rejects_invalid_tenant() -> None:
    with pytest.raises(InvalidTenantIdError):
        document_upload_path("../etc", uuid4(), "pdf")


def test_tenant_upload_dir_rejects_invalid_tenant() -> None:
    with pytest.raises(InvalidTenantIdError):
        tenant_upload_dir("acme/foo")


def test_tenant_working_dir_rejects_invalid_tenant() -> None:
    with pytest.raises(InvalidTenantIdError):
        tenant_working_dir("acme/foo")


def test_uuid_object_accepted() -> None:
    doc_id = UUID("12345678-1234-5678-1234-567812345678")
    p = document_upload_path("acme", doc_id, "pdf")
    assert p.name == "12345678-1234-5678-1234-567812345678.pdf"
