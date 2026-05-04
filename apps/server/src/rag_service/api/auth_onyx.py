"""Internal-token + IP-allowlist + KB-existence dependencies for ``/v1/onyx/*``.

The ``/v1/onyx/*`` surface is consumed only by the ONYX backend (a
trusted internal service), never directly by browsers. Auth is therefore
modeled as service-to-service rather than user-session: a long-lived
shared secret in ``Authorization: Bearer <INTERNAL_TOKEN>``, optionally
paired with a CIDR allowlist for defense in depth.

Two dependencies are exported. ``onyx_service_auth_no_kb`` is for
endpoints that don't operate on a particular KB (create / list); it
checks token + IP and forces ``ctx.kb_id = None`` regardless of what the
``X-Onyx-KB-Id`` header says. ``onyx_service_auth`` is for everything
else: it does the same token + IP checks and additionally validates
that the ``X-Onyx-KB-Id`` header maps to a tenants row whose
``config_json.source == "onyx"``. Both unknown KBs and tenants the ONYX
backend isn't supposed to know about (those owned by regular users)
collapse to the same 404 — we don't want this surface to be a tenant
oracle.

All comparisons against the live + legacy tokens go through
``hmac.compare_digest`` so a malicious caller can't time-side-channel
their way to a valid prefix. The legacy-token loop deliberately compares
against every entry rather than short-circuiting on the first match;
short-circuiting would leak which slot the caller's token landed in.
"""

from __future__ import annotations

import hmac
import ipaddress
import uuid
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rag_service.config import settings
from rag_service.core.paths import InvalidTenantIdError, validate_tenant_id
from rag_service.db.models import Tenant
from rag_service.db.session import get_db_session


# ``X-Onyx-User-Id`` is opaque to us — we only relay it for audit
# logging. Cap it at 128 chars so a malicious / buggy caller can't blow
# up the audit pipeline with megabyte-sized headers. KB ids share the
# same 64-char ceiling enforced by :func:`validate_tenant_id` (kept here
# as a named constant so callers can reference it without re-deriving).
ONYX_USER_ID_MAX_LEN = 128
ONYX_KB_ID_MAX_LEN = 64


@dataclass(frozen=True)
class OnyxCallContext:
    """Per-request bundle threaded into routers via ``Depends(...)``.

    ``kb_id`` is ``None`` for create / list endpoints (those routes use
    ``onyx_service_auth_no_kb``) and is always set for KB-scoped routes
    (``onyx_service_auth``). ``onyx_user_id`` is optional at the
    dependency layer so the same context shape can serve every route;
    individual routers MUST raise themselves if they need it set.
    ``request_id`` is always populated — either from the caller's
    ``X-Request-Id`` header or freshly generated as a uuid4 hex.
    """

    kb_id: str | None
    onyx_user_id: str | None
    request_id: str
    caller_ip: str


# ---------------------------------------------------------------------------
# Token comparison
# ---------------------------------------------------------------------------


def _validate_internal_token(token: str) -> bool:
    """Return ``True`` iff ``token`` matches the live OR any legacy token.

    Comparisons go through :func:`hmac.compare_digest` to avoid timing
    side-channels. The legacy loop intentionally evaluates every entry —
    a short-circuit ``return True`` on first match would tell an attacker
    *which* slot accepted their guess by measuring response time.

    Catches all exceptions and returns ``False`` because any failure
    here (e.g. a non-string slipping through) MUST collapse to ``invalid
    token`` rather than a 500.
    """
    try:
        if not isinstance(token, str):
            return False
        matched = False
        if hmac.compare_digest(token, settings.internal_token):
            matched = True
        for legacy in settings.internal_tokens_legacy:
            if hmac.compare_digest(token, legacy):
                matched = True
        return matched
    except Exception:  # noqa: BLE001 — any failure → reject
        return False


# ---------------------------------------------------------------------------
# IP allowlist
# ---------------------------------------------------------------------------


def _resolve_caller_ip(request: Request) -> str | None:
    """Best-effort caller-IP extraction.

    Prefers the first hop of ``X-Forwarded-For`` (the convention behind
    every reverse proxy that fronts this service); falls back to the
    direct ``request.client.host``. Returns ``None`` when neither source
    is available — this is the "unknown caller" case that the allowlist
    rejects when enabled.
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",", 1)[0].strip()
        if first:
            return first
    if request.client is not None and request.client.host:
        return request.client.host
    return None


def _check_caller_ip_allowed(request: Request) -> str:
    """Enforce the CIDR allowlist (if configured) and return the caller IP.

    Returns the resolved caller IP string on success — routers stash this
    on :class:`OnyxCallContext` for audit logs. When the allowlist is
    empty we still return whatever IP we managed to extract (or
    ``"unknown"`` as a last resort) so the audit channel never has a
    ``None`` to deal with.

    Raises ``HTTPException(403)`` when the allowlist is non-empty AND
    either (a) we can't determine a caller IP or (b) the IP we found
    sits outside every configured network. Both cases collapse to the
    same generic ``caller ip not allowed`` detail to avoid leaking
    whether the IP was missing vs. outside the allowlist.
    """
    caller_ip = _resolve_caller_ip(request)
    cidrs = settings.onyx_backend_allowed_cidrs

    if not cidrs:
        return caller_ip or "unknown"

    if caller_ip is None:
        # Allowlist on but nothing identifies the caller — fail closed.
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail="caller ip not allowed"
        )

    try:
        addr = ipaddress.ip_address(caller_ip)
    except ValueError:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail="caller ip not allowed"
        )

    for cidr in cidrs:
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            # Settings validator should have caught this at startup;
            # treat any malformed entry here as a no-match rather than
            # raising 500.
            continue
        if addr.version != net.version:
            continue
        if addr in net:
            return caller_ip

    raise HTTPException(
        status.HTTP_403_FORBIDDEN, detail="caller ip not allowed"
    )


# ---------------------------------------------------------------------------
# Header parsing — shared between both deps
# ---------------------------------------------------------------------------


def _enforce_bearer_and_extract_token(request: Request) -> str:
    """Return the bearer token string or raise the matching 401.

    Splitting this out keeps the two deps in lock-step on the exact 401
    detail strings — a divergence there would leak information about the
    code path.
    """
    auth = request.headers.get("authorization")
    if not auth or not auth.startswith("Bearer "):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, detail="missing internal token"
        )
    token = auth[len("Bearer "):]
    if not _validate_internal_token(token):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, detail="invalid internal token"
        )
    return token


def _read_user_id_header(request: Request) -> str | None:
    """Return ``X-Onyx-User-Id`` (or ``None``); 400 when too long."""
    raw = request.headers.get("x-onyx-user-id")
    if raw is None:
        return None
    if len(raw) > ONYX_USER_ID_MAX_LEN:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="X-Onyx-User-Id too long"
        )
    return raw


def _read_request_id_header(request: Request) -> str:
    """Return the caller's ``X-Request-Id`` or freshly mint a uuid4 hex."""
    rid = request.headers.get("x-request-id")
    if rid:
        return rid
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# DB lookup
# ---------------------------------------------------------------------------


async def verify_onyx_kb(kb_id: str, db: AsyncSession) -> Tenant | None:
    """Return the ``Tenant`` row IFF it exists AND is owned by ONYX.

    "Owned by ONYX" means ``config_json.source == "onyx"``. Returns
    ``None`` for: missing rows, rows whose ``config_json`` is null /
    empty, and rows whose ``source`` is anything else. Callers MUST NOT
    distinguish those cases in error responses — the whole point of this
    helper is to cloak non-ONYX tenants behind the same 404 as missing
    ones, so the ONYX backend can't enumerate user-tenant ids.
    """
    row = (
        await db.execute(select(Tenant).where(Tenant.tenant_id == kb_id))
    ).scalar_one_or_none()
    if row is None:
        return None
    cfg = row.config_json
    if not isinstance(cfg, dict):
        return None
    if cfg.get("source") != "onyx":
        return None
    return row


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


async def onyx_service_auth_no_kb(request: Request) -> OnyxCallContext:
    """Auth for create / list endpoints — no KB scoping.

    Validates the bearer token and IP allowlist, then returns a context
    with ``kb_id=None``. The ``X-Onyx-KB-Id`` header is *deliberately
    ignored* on this path even if the caller sends one — these
    endpoints don't operate on a specific KB, and accepting the header
    silently could mask a routing bug on the ONYX side.
    """
    _enforce_bearer_and_extract_token(request)
    caller_ip = _check_caller_ip_allowed(request)
    user_id = _read_user_id_header(request)
    request_id = _read_request_id_header(request)
    return OnyxCallContext(
        kb_id=None,
        onyx_user_id=user_id,
        request_id=request_id,
        caller_ip=caller_ip,
    )


async def onyx_service_auth(
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> OnyxCallContext:
    """Auth for KB-scoped endpoints.

    Same token + IP checks as :func:`onyx_service_auth_no_kb`, plus the
    KB-existence check. ``X-Onyx-KB-Id`` is required; missing → 400.
    Pattern-invalid ids and rows that aren't ONYX-owned both cloak as
    404 ``kb not found`` (see :func:`verify_onyx_kb`).
    """
    _enforce_bearer_and_extract_token(request)
    caller_ip = _check_caller_ip_allowed(request)
    user_id = _read_user_id_header(request)
    request_id = _read_request_id_header(request)

    raw_kb = request.headers.get("x-onyx-kb-id")
    if not raw_kb:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="X-Onyx-KB-Id header required"
        )
    try:
        kb_id = validate_tenant_id(raw_kb)
    except InvalidTenantIdError:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail="kb not found"
        )

    tenant = await verify_onyx_kb(kb_id, db)
    if tenant is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail="kb not found"
        )

    return OnyxCallContext(
        kb_id=kb_id,
        onyx_user_id=user_id,
        request_id=request_id,
        caller_ip=caller_ip,
    )
