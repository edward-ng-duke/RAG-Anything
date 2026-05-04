"""``/v1/tenants`` — tenant identity + live storage accounting.

A read-only window onto the caller's own tenant row. The single endpoint
``GET /v1/tenants/me`` joins the static ``tenants`` row (identity, quota
config) with two aggregate queries over ``documents`` (storage_used_mb,
document_count) so clients can render a quota bar without doing the
conversion themselves.

PATCH /v1/tenants/me (display_name / config_json updates) is Phase 9 and
intentionally not implemented here.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from rag_service.api.deps import current_tenant, get_db_session
from rag_service.api.schemas import TenantInfoResponse
from rag_service.db.models import Document, Tenant

router = APIRouter(prefix="/v1/tenants", tags=["tenants"])


@router.get("/me", response_model=TenantInfoResponse)
async def me(
    tenant_id: str = Depends(current_tenant),
    db: AsyncSession = Depends(get_db_session),
) -> TenantInfoResponse:
    """Return identity + live storage accounting for the caller's tenant.

    A token whose ``X-Tenant-Id`` claim doesn't match any ``tenants`` row
    yields a 404 — the API never auto-provisions tenants on first use;
    creation is an out-of-band ops step.

    The two aggregate queries (``SUM(file_size)`` and ``COUNT(*)``) both
    exclude rows in the ``"deleted"`` status so the displayed numbers
    line up with what ``GET /v1/documents`` returns by default.
    """
    t = (
        await db.execute(select(Tenant).where(Tenant.tenant_id == tenant_id))
    ).scalar_one_or_none()
    if t is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "tenant not found")

    sum_size = (
        await db.execute(
            select(func.coalesce(func.sum(Document.file_size), 0)).where(
                Document.tenant_id == tenant_id,
                Document.status != "deleted",
            )
        )
    ).scalar_one()

    count = (
        await db.execute(
            select(func.count(Document.document_id)).where(
                Document.tenant_id == tenant_id,
                Document.status != "deleted",
            )
        )
    ).scalar_one()

    return TenantInfoResponse(
        tenant_id=t.tenant_id,
        display_name=t.display_name,
        storage_quota_mb=t.storage_quota_mb,
        storage_used_mb=round(sum_size / (1024 * 1024), 2),
        document_count=count,
    )
