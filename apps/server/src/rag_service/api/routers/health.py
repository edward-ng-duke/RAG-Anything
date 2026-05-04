"""Liveness and readiness endpoints.

``/healthz`` always returns 200 (process is up).
``/readyz`` checks Postgres, Redis, and that the configured ``data_dir`` is
writable; returns 503 with per-check status if any dependency is unhealthy.

The ``/metrics`` endpoint is wired by the observability module (Task 1.21);
this file only owns ``/healthz`` and ``/readyz``.
"""

from __future__ import annotations

from pathlib import Path

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from rag_service.api.deps import get_db_session, get_redis
from rag_service.config import settings

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe — always 200 if the process is running."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(
    db: AsyncSession = Depends(get_db_session),
    redis: aioredis.Redis = Depends(get_redis),
) -> dict[str, object]:
    """Readiness probe — verifies PG, Redis, and data_dir are usable."""
    checks: dict[str, str] = {}

    # Postgres: cheap round-trip.
    try:
        await db.execute(text("SELECT 1"))
        checks["pg"] = "ok"
    except Exception as e:  # noqa: BLE001 — surface failure mode in payload
        checks["pg"] = f"fail: {type(e).__name__}"

    # Redis: PING returns truthy on success.
    try:
        pong = await redis.ping()
        checks["redis"] = "ok" if pong else "fail"
    except Exception as e:  # noqa: BLE001
        checks["redis"] = f"fail: {type(e).__name__}"

    # data_dir: must exist and be writable for uploads/working dirs.
    try:
        data_dir = Path(settings.data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / ".rw-probe"
        probe.write_text("x")
        probe.unlink()
        checks["data_dir"] = "ok"
    except Exception as e:  # noqa: BLE001
        checks["data_dir"] = f"fail: {type(e).__name__}"

    if not all(v == "ok" for v in checks.values()):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "not_ready", "checks": checks},
        )
    return {"status": "ready", "checks": checks}
