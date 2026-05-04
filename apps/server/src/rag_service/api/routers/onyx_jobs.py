"""/v1/onyx/jobs — KB-scoped job inspection for ONYX integration.

Single endpoint mirroring α's /v1/jobs/{job_id} but tenant_id sourced
from X-Onyx-KB-Id (via onyx_service_auth) instead of JWT.
"""

from __future__ import annotations
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rag_service.api.auth_onyx import OnyxCallContext, onyx_service_auth
from rag_service.api.deps import get_db_session
from rag_service.api.onyx_schemas import OnyxJobResponse
from rag_service.db.models import Job

router = APIRouter(prefix="/v1/onyx/jobs", tags=["onyx-jobs"])


@router.get("/{job_id}", response_model=OnyxJobResponse)
async def get_job(
    job_id: UUID,
    ctx: OnyxCallContext = Depends(onyx_service_auth),
    db: AsyncSession = Depends(get_db_session),
) -> OnyxJobResponse:
    row = (
        await db.execute(
            select(Job).where(Job.job_id == job_id, Job.tenant_id == ctx.kb_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    return OnyxJobResponse(
        job_id=str(row.job_id),
        document_id=str(row.document_id) if row.document_id else None,
        job_type=row.job_type,
        status=row.status,
        progress=row.progress,
        error_message=row.error_message,
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        retries=getattr(row, "retries", 0) or 0,
    )
