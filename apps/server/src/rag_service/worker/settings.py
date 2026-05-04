"""arq worker entry config — registers ingest_document + rebuild_index."""
from __future__ import annotations

from arq.connections import RedisSettings

from rag_service.config import settings
from rag_service.worker.tasks import ingest_document, rebuild_index


def _redis_settings_from_url(url: str) -> RedisSettings:
    """Parse a Redis DSN into arq's :class:`RedisSettings`."""
    return RedisSettings.from_dsn(url)


async def _on_startup(ctx):
    """Init shared resources: redis client, RAG cache."""
    import redis.asyncio as aioredis

    from rag_service.core.rag_factory import get_cache

    ctx["redis"] = aioredis.from_url(settings.redis_url)
    ctx["rag_cache"] = get_cache()


async def _on_shutdown(ctx):
    """Finalise resources opened in :func:`_on_startup`."""
    cache = ctx.get("rag_cache")
    if cache is not None:
        await cache.aclose()
    redis = ctx.get("redis")
    if redis is not None:
        await redis.aclose()


class WorkerSettings:
    """arq ``WorkerSettings`` consumed by the ``arq`` CLI.

    arq introspects this class for ``redis_settings``, ``functions``,
    ``on_startup``, ``on_shutdown``, etc. Because arq reads ``redis_settings``
    as a class attribute (not a property/descriptor it understands), we
    compute it eagerly at module import. If the configuration is incomplete
    we fall back to ``None`` so the worker fails fast with a clear error
    when arq actually tries to connect.
    """

    functions = [ingest_document, rebuild_index]
    on_startup = staticmethod(_on_startup)
    on_shutdown = staticmethod(_on_shutdown)
    max_jobs = 8
    job_timeout = 3600
    keep_result = 86400


try:
    WorkerSettings.redis_settings = _redis_settings_from_url(settings.redis_url)
except Exception:  # pragma: no cover - misconfiguration surfaced at startup
    WorkerSettings.redis_settings = None  # type: ignore[assignment]
