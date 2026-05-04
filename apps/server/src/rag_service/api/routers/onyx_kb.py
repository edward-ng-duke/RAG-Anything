"""/v1/onyx/kb — KB lifecycle for ONYX integration.

Service-to-service endpoints invoked by the ONYX backend (never directly
by a browser). Each request carries:
  Authorization: Bearer <INTERNAL_TOKEN>
  X-Onyx-User-Id:   <onyx user uuid>     (audit; not enforced as ACL)
  X-Onyx-KB-Id:     <kb_id>              (for GET-by-id / DELETE only)
  X-Request-Id:     <uuid>               (echoed back in response)

ACL is fully owned by ONYX. RAG enforces only KB existence + source=onyx.
"""

from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from rag_service.api.auth_onyx import (
    OnyxCallContext,
    onyx_service_auth,
    onyx_service_auth_no_kb,
)
from rag_service.api.deps import get_db_session
from rag_service.api.onyx_schemas import (
    CreateKBRequest,
    KBBrief,
    KBDetail,
    KBListResponse,
)
from rag_service.config import settings
from rag_service.db.repositories.tenants import (
    create_onyx_kb,
    delete_kb_cascade,
    get_onyx_kb,
    list_onyx_kbs,
)

router = APIRouter(prefix="/v1/onyx/kb", tags=["onyx-kb"])


def _to_brief(d: dict) -> KBBrief:
    return KBBrief(
        kb_id=d["tenant_id"],
        display_name=d["display_name"],
        storage_quota_mb=d.get("storage_quota_mb"),
        storage_used_mb=d.get("storage_used_mb", 0.0),
        document_count=d.get("document_count", 0),
        created_at=d["created_at"],
    )


def _to_detail(d: dict) -> KBDetail:
    return KBDetail(
        kb_id=d["tenant_id"],
        display_name=d["display_name"],
        storage_quota_mb=d.get("storage_quota_mb"),
        storage_used_mb=d.get("storage_used_mb", 0.0),
        document_count=d.get("document_count", 0),
        created_at=d["created_at"],
        onyx_workspace_id=d.get("onyx_workspace_id"),
        onyx_owner_user_id=d.get("onyx_owner_user_id"),
    )


@router.post("", response_model=KBDetail, status_code=status.HTTP_201_CREATED)
async def create_kb(
    body: CreateKBRequest,
    ctx: OnyxCallContext = Depends(onyx_service_auth_no_kb),
    db: AsyncSession = Depends(get_db_session),
) -> KBDetail:
    tenant = await create_onyx_kb(
        db,
        display_name=body.display_name,
        onyx_workspace_id=body.onyx_workspace_id,
        onyx_owner_user_id=body.onyx_owner_user_id,
        storage_quota_mb=body.storage_quota_mb,
    )
    await db.commit()
    # immediately fetch the enriched dict so storage_used_mb / document_count
    # are populated from the same source of truth used by /kb/{kb_id}
    enriched = await get_onyx_kb(db, tenant.tenant_id)
    return _to_detail(enriched)


@router.get("", response_model=KBListResponse)
async def list_kbs(
    cursor: str | None = None,
    limit: int = 50,
    onyx_workspace_id: str | None = None,
    onyx_owner_user_id: str | None = None,
    ctx: OnyxCallContext = Depends(onyx_service_auth_no_kb),
    db: AsyncSession = Depends(get_db_session),
) -> KBListResponse:
    if limit < 1:
        raise HTTPException(400, "limit must be >= 1")
    rows, next_cursor = await list_onyx_kbs(
        db,
        cursor=cursor,
        limit=limit,
        onyx_workspace_id=onyx_workspace_id,
        onyx_owner_user_id=onyx_owner_user_id,
    )
    # Each row from list_onyx_kbs is a Tenant ORM; we need the enriched dict
    # for storage_used_mb. Cheap: get_onyx_kb for each.
    items = []
    for r in rows:
        d = await get_onyx_kb(db, r.tenant_id)
        if d is not None:
            items.append(_to_brief(d))
    return KBListResponse(items=items, next_cursor=next_cursor)


@router.get("/{kb_id}", response_model=KBDetail)
async def get_kb(
    kb_id: str,
    ctx: OnyxCallContext = Depends(onyx_service_auth),
    db: AsyncSession = Depends(get_db_session),
) -> KBDetail:
    # onyx_service_auth already checked X-Onyx-KB-Id matches an existing
    # source=onyx tenant. We additionally require path kb_id == ctx.kb_id
    # so a caller can't pass header X-Onyx-KB-Id=A while hitting /kb/B.
    if kb_id != ctx.kb_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "path kb_id does not match X-Onyx-KB-Id header"
        )
    enriched = await get_onyx_kb(db, kb_id)
    if enriched is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "kb not found")
    return _to_detail(enriched)


@router.delete("/{kb_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_kb(
    kb_id: str,
    ctx: OnyxCallContext = Depends(onyx_service_auth),
    db: AsyncSession = Depends(get_db_session),
) -> None:
    if kb_id != ctx.kb_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "path kb_id does not match X-Onyx-KB-Id header"
        )
    deleted = await delete_kb_cascade(db, kb_id, data_dir=settings.data_dir)
    await db.commit()
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "kb not found")
    return None
