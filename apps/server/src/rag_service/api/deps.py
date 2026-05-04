from rag_service.api.auth import current_tenant  # noqa: F401
from rag_service.db.session import get_db_session  # noqa: F401

# Redis dep
import redis.asyncio as aioredis
from rag_service.config import settings
from functools import lru_cache


@lru_cache(maxsize=1)
def _redis_pool() -> aioredis.Redis:
    return aioredis.from_url(settings.redis_url, decode_responses=False)


async def get_redis() -> aioredis.Redis:
    return _redis_pool()


# RAG cache dep
from rag_service.core.rag_factory import get_cache as _get_cache


async def get_rag_cache():
    return _get_cache()
