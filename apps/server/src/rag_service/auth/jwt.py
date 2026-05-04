"""JWT signing, validation, and Redis-backed revocation.

Tokens are HS256-signed with ``settings.jwt_secret_key`` (validated to be
≥64 chars). Two token types are issued:

- ``access``  short-lived (default 15 min); revoked via blacklist on logout.
- ``refresh`` long-lived (default 7 days); each carries a ``jti`` so a single
  refresh token can be revoked (logout / refresh-rotation) without rotating
  the signing key.

Revocation state lives in Redis under two namespaces:

- ``refresh_revoked:<jti>``      → set on logout / rotation
- ``access_blacklisted:<key>``   → set on logout

Both keys carry a TTL equal to the token's remaining lifetime so Redis
self-cleans expired entries.
"""

from __future__ import annotations

import hashlib
import time
import uuid

import jwt as pyjwt

from rag_service.config import settings

ALGO = "HS256"


def _now() -> int:
    """Current unix timestamp as int — patched in tests to simulate skew/expiry."""
    return int(time.time())


def _access_key(claims: dict, token: str) -> str:
    """Identifier used for blacklisting an access token in Redis.

    Prefer the JWT's ``jti`` claim if present; fall back to a 16-char SHA-256
    prefix of the raw token. The hash is collision-resistant enough for an
    ephemeral blacklist while keeping the Redis key short.
    """
    return claims.get("jti") or hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def create_access_token(user_id: str, tenant_id: str | None = None) -> str:
    """Mint a short-lived access token for ``user_id``.

    ``tenant_id`` is optional — included as a ``tenant`` claim only when set
    so single-tenant deployments don't carry an empty field.
    """
    payload: dict = {
        "sub": str(user_id),
        "iat": _now(),
        "exp": _now() + settings.access_token_ttl_min * 60,
        "type": "access",
    }
    if tenant_id is not None:
        payload["tenant"] = tenant_id
    return pyjwt.encode(payload, settings.jwt_secret_key, algorithm=ALGO)


def create_refresh_token(user_id: str) -> tuple[str, str]:
    """Mint a refresh token; return ``(token, jti)``.

    Callers should persist the ``jti`` (e.g. in Redis with TTL =
    ``refresh_token_ttl_days * 86400``) so the token can be revoked
    individually on logout or refresh-rotation.
    """
    jti = uuid.uuid4().hex
    payload = {
        "sub": str(user_id),
        "iat": _now(),
        "exp": _now() + settings.refresh_token_ttl_days * 86400,
        "jti": jti,
        "type": "refresh",
    }
    return pyjwt.encode(payload, settings.jwt_secret_key, algorithm=ALGO), jti


def decode_token(token: str) -> dict:
    """Verify signature + expiry and return the claims dict.

    Raises ``pyjwt`` errors (``ExpiredSignatureError``,
    ``InvalidSignatureError``, ``DecodeError``, …) on failure.
    """
    return pyjwt.decode(token, settings.jwt_secret_key, algorithms=[ALGO])


# ---- Refresh-token revocation (per-jti) ----


async def revoke_refresh(redis, jti: str) -> None:
    """Mark a refresh token as revoked. Call on logout / refresh-rotation."""
    await redis.set(
        f"refresh_revoked:{jti}",
        "1",
        ex=settings.refresh_token_ttl_days * 86400,
    )


async def is_refresh_revoked(redis, jti: str) -> bool:
    """True if the given refresh ``jti`` has been revoked."""
    return bool(await redis.exists(f"refresh_revoked:{jti}"))


# ---- Access-token blacklist (logout) ----


async def blacklist_access(redis, token: str) -> None:
    """Blacklist an access token until its ``exp``.

    Malformed tokens are silently ignored — there is nothing meaningful to
    blacklist, and the matching ``is_access_blacklisted`` already treats
    undecodable input as untrusted.
    """
    try:
        claims = pyjwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[ALGO],
            options={"verify_exp": False},
        )
    except pyjwt.PyJWTError:
        return
    exp = claims.get("exp", _now())
    ttl = max(1, exp - _now())
    key = _access_key(claims, token)
    await redis.set(f"access_blacklisted:{key}", "1", ex=ttl)


async def is_access_blacklisted(redis, token: str) -> bool:
    """True if the access token is on the blacklist (or undecodable).

    Undecodable input is treated as blacklisted: callers should refuse it
    rather than passing it through unchecked.
    """
    try:
        claims = pyjwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[ALGO],
            options={"verify_exp": False},
        )
    except pyjwt.PyJWTError:
        return True
    key = _access_key(claims, token)
    return bool(await redis.exists(f"access_blacklisted:{key}"))
