"""Shared fixtures for ``tests/api/``.

Provides :func:`make_jwt`, a tiny helper that mints a real JWT for a
given ``(user_id, tenant_id)`` so tests that exercise the JWT-aware auth
deps can produce valid bearer tokens without re-implementing the signing
logic.
"""

from __future__ import annotations

import pytest

from rag_service.auth.jwt import create_access_token


@pytest.fixture
def make_jwt():
    """Return a callable that signs an access token for ``(user_id, tenant_id)``.

    The two-arg signature matches :func:`rag_service.auth.jwt.create_access_token`
    so tests can swap the helper for the underlying function with no
    code changes once they need refresh-token plumbing too.
    """

    def _make(user_id, tenant_id=None) -> str:
        return create_access_token(user_id, tenant_id)

    return _make
