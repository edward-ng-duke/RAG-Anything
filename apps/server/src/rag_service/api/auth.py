"""JWT-based ``current_user`` / ``current_tenant`` dependencies.

Replaces the header-bearer placeholder used during early scaffolding. Both
deps validate the bearer token, check the access blacklist, decode the
JWT, assert ``type=access``, and load the user row. ``current_tenant``
additionally enforces that the JWT's ``tenant`` claim matches an existing
membership row — switching tenant goes through ``/v1/auth/select_tenant``,
which mints a fresh access token.
"""

from __future__ import annotations

import uuid

from functools import lru_cache

import redis.asyncio as aioredis
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rag_service.auth.jwt import decode_token, is_access_blacklisted
from rag_service.config import settings
from rag_service.core.paths import InvalidTenantIdError, validate_tenant_id
from rag_service.db.models import Membership, User
from rag_service.db.session import get_db_session


# ``get_redis`` is defined here rather than imported from
# ``rag_service.api.deps`` because deps.py re-exports the auth deps —
# pulling redis out of deps would create a circular import. Defining the
# pool helper here keeps ``auth.py`` self-contained; deps.py re-exports
# ``get_redis`` for callers that prefer the one-stop dependency module.


@lru_cache(maxsize=1)
def _redis_pool() -> aioredis.Redis:
    return aioredis.from_url(settings.redis_url, decode_responses=False)


async def get_redis() -> aioredis.Redis:
    return _redis_pool()


async def current_user(
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db_session),
    redis=Depends(get_redis),
) -> User:
    """Resolve the JWT-authenticated user or raise 401.

    Validates four things in order: bearer-prefixed header, blacklist
    membership, signature/exp via ``decode_token``, and ``type=access``
    claim. Finally loads the user row and rejects deactivated accounts.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    token = authorization[len("Bearer "):]
    if await is_access_blacklisted(redis, token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token revoked")
    try:
        claims = decode_token(token)
    except Exception:  # noqa: BLE001 — pyjwt error tree, treat all as 401
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")
    if claims.get("type") != "access":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong token type")
    try:
        user_id = uuid.UUID(claims["sub"])
    except (KeyError, ValueError, TypeError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "malformed token sub")
    user = (
        await db.execute(select(User).where(User.user_id == user_id))
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user disabled")
    return user


async def current_tenant(
    user: User = Depends(current_user),
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db_session),
) -> str:
    """Return the active tenant_id from the JWT claim, validated against memberships.

    The header is decoded a second time here rather than threaded through
    ``current_user``'s return value because keeping the dep contracts narrow
    (user vs. tenant) makes the routers self-documenting. Decoding twice on
    every request is cheap (HS256 + a dict lookup); the database round-trip
    that ``current_user`` already issued dwarfs it.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    try:
        claims = decode_token(authorization[len("Bearer "):])
    except Exception:  # noqa: BLE001 — pyjwt error tree, treat all as 401
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")
    tenant_id = claims.get("tenant")
    if not tenant_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "no active tenant; call select_tenant"
        )
    try:
        tenant_id = validate_tenant_id(tenant_id)
    except InvalidTenantIdError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid tenant_id in token")

    membership = (
        await db.execute(
            select(Membership).where(
                Membership.user_id == user.user_id,
                Membership.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if membership is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "not a member of that tenant"
        )
    return tenant_id
