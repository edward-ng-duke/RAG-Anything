import secrets
from fastapi import Header, HTTPException, status
from rag_service.config import settings
from rag_service.core.paths import validate_tenant_id, InvalidTenantIdError


def _const_eq(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode(), b.encode())


async def current_tenant(
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    token = authorization[len("Bearer "):]
    if not _const_eq(token, settings.internal_token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")
    if not x_tenant_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "missing X-Tenant-Id")
    try:
        return validate_tenant_id(x_tenant_id)
    except InvalidTenantIdError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid X-Tenant-Id")
