"""``GET /v1/jobs/{job_id}`` — fetch a single job's status.

Read-only inspection endpoint scoped to the caller's tenant. The query
filters on both ``job_id`` *and* ``tenant_id`` so a client can never read
another tenant's job — a missing row and a cross-tenant row are
indistinguishable from the outside (both return ``404``), which is the
behaviour we want for tenant isolation.

The response shape lives in :class:`rag_service.api.schemas.JobResponse`,
which maps directly onto the ``jobs`` ORM model via ``from_attributes``.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rag_service.api.deps import current_tenant, get_db_session
from rag_service.api.schemas import JobResponse
from rag_service.db.models import Job

router = APIRouter(prefix="/v1/jobs", tags=["jobs"])


@router.get("/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: UUID,
    tenant_id: str = Depends(current_tenant),
    db: AsyncSession = Depends(get_db_session),
) -> Job:
    """Return the job row owned by ``tenant_id`` with id ``job_id``.

    Raises ``404`` if the row does not exist *or* belongs to another
    tenant — we never leak the existence of another tenant's job.
    """
    result = await db.execute(
        select(Job).where(Job.job_id == job_id, Job.tenant_id == tenant_id)
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    return job
