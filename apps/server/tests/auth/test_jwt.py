"""Tests for ``rag_service.auth.jwt``: signing, decode, and Redis revocation.

Bootstraps required env (including a 64-char ``JWT_SECRET_KEY``) before any
``rag_service`` import so the lazy ``settings`` singleton can be constructed
without a real ``.env`` file.
"""

from __future__ import annotations

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
# 64+ char secret so the field_validator on Settings.jwt_secret_key passes.
_TEST_SECRET = "a" * 64
os.environ.setdefault("JWT_SECRET_KEY", _TEST_SECRET)

import fakeredis.aioredis  # noqa: E402
import jwt as pyjwt  # noqa: E402
import pytest  # noqa: E402

from rag_service.auth import jwt as auth_jwt  # noqa: E402


# ---- Token round-trips ----------------------------------------------------


def test_access_token_roundtrip():
    """create_access_token + decode_token returns sub + tenant claims."""
    tok = auth_jwt.create_access_token("user-123", tenant_id="tenant-A")
    claims = auth_jwt.decode_token(tok)
    assert claims["sub"] == "user-123"
    assert claims["tenant"] == "tenant-A"
    assert claims["type"] == "access"
    assert "iat" in claims
    assert "exp" in claims


def test_access_token_without_tenant_omits_claim():
    """When tenant_id is None, the 'tenant' claim must not be present."""
    tok = auth_jwt.create_access_token("user-123")
    claims = auth_jwt.decode_token(tok)
    assert "tenant" not in claims


def test_refresh_token_roundtrip():
    """create_refresh_token + decode_token returns sub, jti, type=refresh."""
    tok, jti = auth_jwt.create_refresh_token("user-456")
    claims = auth_jwt.decode_token(tok)
    assert claims["sub"] == "user-456"
    assert claims["jti"] == jti
    assert claims["type"] == "refresh"
    # jti is a uuid4 hex (32 chars).
    assert len(jti) == 32


# ---- Expiry & signature failures -----------------------------------------


def test_expired_access_token_raises(monkeypatch):
    """Token minted in the past must raise ExpiredSignatureError on decode."""
    # Mint a token whose exp is already in the past by stubbing _now backward.
    real_now = auth_jwt._now()
    monkeypatch.setattr(auth_jwt, "_now", lambda: real_now - 3600)
    tok = auth_jwt.create_access_token("user-1")
    monkeypatch.undo()  # restore real _now for the decode call
    with pytest.raises(pyjwt.ExpiredSignatureError):
        auth_jwt.decode_token(tok)


def test_invalid_signature_raises():
    """A token signed with a different key must be rejected."""
    other_key = "b" * 64
    bad_tok = pyjwt.encode({"sub": "u", "exp": auth_jwt._now() + 60}, other_key, algorithm="HS256")
    with pytest.raises(pyjwt.InvalidSignatureError):
        auth_jwt.decode_token(bad_tok)


# ---- Refresh revocation via Redis ----------------------------------------


@pytest.mark.asyncio
async def test_revoke_refresh_via_redis():
    """revoke_refresh marks one jti revoked; another jti remains unrevoked."""
    redis = fakeredis.aioredis.FakeRedis()
    _tok, jti = auth_jwt.create_refresh_token("user-1")

    assert await auth_jwt.is_refresh_revoked(redis, jti) is False
    await auth_jwt.revoke_refresh(redis, jti)
    assert await auth_jwt.is_refresh_revoked(redis, jti) is True

    # An unrelated jti is still valid.
    _tok2, other_jti = auth_jwt.create_refresh_token("user-2")
    assert await auth_jwt.is_refresh_revoked(redis, other_jti) is False


# ---- Access blacklist (logout) -------------------------------------------


@pytest.mark.asyncio
async def test_blacklist_access_marks_blacklisted():
    """blacklist_access flips is_access_blacklisted for that token only."""
    redis = fakeredis.aioredis.FakeRedis()
    tok = auth_jwt.create_access_token("user-1")
    other = auth_jwt.create_access_token("user-2")

    assert await auth_jwt.is_access_blacklisted(redis, tok) is False
    await auth_jwt.blacklist_access(redis, tok)
    assert await auth_jwt.is_access_blacklisted(redis, tok) is True
    # Different token (different exp/iat → different signature/hash) unaffected.
    assert await auth_jwt.is_access_blacklisted(redis, other) is False


@pytest.mark.asyncio
async def test_blacklist_malformed_token_safe():
    """Garbage input must not crash; is_blacklisted treats it as untrusted."""
    redis = fakeredis.aioredis.FakeRedis()
    # Should not raise.
    await auth_jwt.blacklist_access(redis, "not-a-jwt-at-all")
    # Undecodable → treat as blacklisted (untrusted).
    assert await auth_jwt.is_access_blacklisted(redis, "not-a-jwt-at-all") is True


# ---- Settings validation -------------------------------------------------


def test_jwt_secret_too_short_raises(monkeypatch):
    """A <64-char JWT_SECRET_KEY must fail Settings validation."""
    from pydantic import ValidationError

    from rag_service.config import Settings

    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 30)
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]
