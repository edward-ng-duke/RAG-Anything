"""Path-construction helpers with tenant_id validation and traversal safety.

These helpers are pure: they perform no disk I/O. They only build :class:`Path`
objects and validate inputs (tenant identifier shape, file extension shape,
document_id type). All resolved paths are checked to live under the configured
``data_dir`` to defeat traversal attempts.
"""

from __future__ import annotations

import re
from pathlib import Path
from uuid import UUID

from rag_service.config import settings

TENANT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_EXT_PATTERN = re.compile(r"^[a-z0-9]{1,8}$")


class InvalidTenantIdError(ValueError):
    """Raised when a tenant_id fails shape validation."""


class PathTraversalError(ValueError):
    """Raised when a constructed path escapes the configured data root."""


def validate_tenant_id(tid: str) -> str:
    """Return ``tid`` unchanged if valid, else raise :class:`InvalidTenantIdError`.

    A valid tenant_id is a non-empty string of 1..64 chars drawn from
    ``[a-zA-Z0-9_-]``.
    """
    if not isinstance(tid, str) or not TENANT_ID_PATTERN.match(tid):
        raise InvalidTenantIdError(f"invalid tenant_id: {tid!r}")
    return tid


def _data_root() -> Path:
    return Path(settings.data_dir).resolve()


def tenant_upload_dir(tid: str) -> Path:
    """Return the per-tenant upload directory under ``data_dir/uploads``."""
    validate_tenant_id(tid)
    p = (_data_root() / "uploads" / tid).resolve()
    if not p.is_relative_to(_data_root()):
        raise PathTraversalError(p)
    return p


def tenant_working_dir(tid: str) -> Path:
    """Return the per-tenant LightRAG working directory under ``data_dir/working_dirs``."""
    validate_tenant_id(tid)
    p = (_data_root() / "working_dirs" / tid).resolve()
    if not p.is_relative_to(_data_root()):
        raise PathTraversalError(p)
    return p


def document_upload_path(tid: str, document_id: UUID, ext: str) -> Path:
    """Return the on-disk upload path ``<uploads>/<tid>/<document_id>.<ext>``.

    ``ext`` may be supplied with or without a leading dot; it is normalized to
    lowercase and validated against ``[a-z0-9]{1,8}``.
    """
    validate_tenant_id(tid)
    if not isinstance(document_id, UUID):
        raise TypeError("document_id must be UUID")
    safe_ext = ext.lstrip(".").lower()
    if not _EXT_PATTERN.match(safe_ext):
        raise ValueError(f"invalid extension: {ext!r}")
    p = (tenant_upload_dir(tid) / f"{document_id}.{safe_ext}").resolve()
    if not p.is_relative_to(_data_root()):
        raise PathTraversalError(p)
    return p
