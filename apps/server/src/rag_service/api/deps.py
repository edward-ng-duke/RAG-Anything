"""Aggregated FastAPI dependency module.

Re-exports the project's per-request deps from their canonical homes so
routers can pull everything from one place. The auth deps live in
``rag_service.api.auth`` (so that module is self-contained and can be
imported without touching this aggregator); ``get_db_session`` lives next
to the engine in ``rag_service.db.session``; ``get_redis`` is colocated
with the auth deps because the JWT layer needs it.
"""

from rag_service.api.auth import current_tenant, current_user, get_redis  # noqa: F401
from rag_service.db.session import get_db_session  # noqa: F401


# RAG cache dep — kept here because no other module needs it.
from rag_service.core.rag_factory import get_cache as _get_cache


async def get_rag_cache():
    return _get_cache()
