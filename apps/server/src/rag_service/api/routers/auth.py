"""``/v1/auth`` — signup / login / me.

Three endpoints that bootstrap the user-identity surface for the alpha
product. They are deliberately self-contained — refresh / logout /
select-tenant land in Task 2.5 and replacement of the legacy
``current_tenant`` dep lands in Task 2.6.

Token strategy:

* ``POST /v1/auth/signup`` — create a user, auto-provision a personal
  tenant (``u-<userid12>``) with the new user as ``owner``, return both
  access + refresh tokens.
* ``POST /v1/auth/login`` — verify password, stamp ``last_login_at``,
  return tokens scoped to the user's first tenant (if any).
* ``GET /v1/auth/me`` — JWT-protected; returns the user row plus its
  tenant memberships.

Every endpoint that requires authentication uses :func:`_current_user`
defined inline below; once Task 2.6 lands this will move to a shared dep
module. The dep validates the bearer token, checks the access blacklist,
asserts the ``type`` claim, and resolves the user row.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rag_service.api.deps import get_db_session, get_redis
from rag_service.api.schemas import (
    AuthTokens,
    LoginRequest,
    MeResponse,
    SignupRequest,
    TenantBrief,
    UserInfo,
)
from rag_service.auth.jwt import (
    create_access_token,
    create_refresh_token,
    decode_token,
    is_access_blacklisted,
)
from rag_service.auth.password import hash_password, verify_password
from rag_service.db.models import Membership, Tenant, User
from rag_service.observability.metrics import rag_auth_login_total

router = APIRouter(prefix="/v1/auth", tags=["auth"])


async def _current_user(
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
    # ``sub`` is always a string in the JWT; the column is a UUID. Convert
    # explicitly so the dialect-specific bind processor (PG UUID or sqlite
    # CHAR(36)) sees the right type.
    try:
        user_id = uuid.UUID(claims["sub"])
    except (ValueError, TypeError, KeyError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")
    user = (
        await db.execute(select(User).where(User.user_id == user_id))
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user disabled")
    return user


async def _user_tenants(db: AsyncSession, user_id) -> list[TenantBrief]:
    """Fetch the user's tenant memberships joined with the tenants table.

    Returned in whatever order the DB chooses; callers that need a stable
    ordering should sort downstream.
    """
    rows = (
        await db.execute(
            select(Tenant, Membership.role)
            .join(Membership, Membership.tenant_id == Tenant.tenant_id)
            .where(Membership.user_id == user_id)
        )
    ).all()
    return [
        TenantBrief(tenant_id=t.tenant_id, display_name=t.display_name, role=role)
        for t, role in rows
    ]


@router.post("/signup", response_model=AuthTokens, status_code=201)
async def signup(
    req: SignupRequest,
    db: AsyncSession = Depends(get_db_session),
    redis=Depends(get_redis),  # noqa: ARG001 — reserved for future rate-limiting
) -> AuthTokens:
    """Create a user + personal tenant, return access + refresh tokens.

    Validation is deliberately minimal at this layer: a syntactic email
    sanity check (``@`` present) and a length floor on the password
    (``≥ 8``). Strong-password and full email-RFC validation are policy
    concerns for a later phase.
    """
    if not req.email or "@" not in req.email:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid email")
    if not req.password or len(req.password) < 8:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "password must be ≥ 8 chars")

    existing = (
        await db.execute(select(User).where(User.email == req.email))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "email already registered")

    user = User(
        email=req.email,
        password_hash=hash_password(req.password),
        display_name=req.display_name,
    )
    db.add(user)
    await db.flush()

    # Auto-provision the user's first tenant + owner membership. The id
    # ``u-<12hex>`` is short, opaque, and obviously user-scoped to anyone
    # eyeballing logs.
    tenant_id = f"u-{user.user_id.hex[:12]}"
    tenant = Tenant(
        tenant_id=tenant_id,
        display_name=req.display_name or req.email.split("@")[0],
    )
    db.add(tenant)
    await db.flush()

    db.add(Membership(user_id=user.user_id, tenant_id=tenant_id, role="owner"))
    await db.commit()
    await db.refresh(user)

    access = create_access_token(user.user_id, tenant_id)
    refresh, _ = create_refresh_token(user.user_id)

    return AuthTokens(
        access_token=access,
        refresh_token=refresh,
        user=UserInfo.model_validate(user),
        tenants=[
            TenantBrief(
                tenant_id=tenant_id,
                display_name=tenant.display_name,
                role="owner",
            )
        ],
    )


@router.post("/login", response_model=AuthTokens)
async def login(
    req: LoginRequest,
    db: AsyncSession = Depends(get_db_session),
    redis=Depends(get_redis),  # noqa: ARG001 — reserved for future rate-limiting
) -> AuthTokens:
    """Verify credentials and mint a fresh access + refresh token pair.

    The 401 path collapses ``unknown email``, ``wrong password`` and
    ``deactivated account`` to the same generic message ("invalid
    credentials") so an attacker can't probe which emails are registered.
    The login counter is incremented on both the success and failure
    paths so the metric exposes the fail rate.
    """
    user = (
        await db.execute(select(User).where(User.email == req.email))
    ).scalar_one_or_none()
    if (
        user is None
        or not verify_password(req.password, user.password_hash)
        or not user.is_active
    ):
        rag_auth_login_total.labels(result="fail").inc()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")

    user.last_login_at = datetime.now(timezone.utc)
    await db.commit()

    tenants = await _user_tenants(db, user.user_id)
    first_tenant = tenants[0].tenant_id if tenants else None

    access = create_access_token(user.user_id, first_tenant)
    refresh, _ = create_refresh_token(user.user_id)
    rag_auth_login_total.labels(result="ok").inc()

    return AuthTokens(
        access_token=access,
        refresh_token=refresh,
        user=UserInfo.model_validate(user),
        tenants=tenants,
    )


@router.get("/me", response_model=MeResponse)
async def me(
    user: User = Depends(_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> MeResponse:
    """Return the authenticated user's profile + tenant memberships."""
    tenants = await _user_tenants(db, user.user_id)
    return MeResponse(user=UserInfo.model_validate(user), tenants=tenants)
