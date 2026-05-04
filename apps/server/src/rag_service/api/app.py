from contextlib import asynccontextmanager
from uuid import uuid4
import asyncio
import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from rag_service.api.rate_limit import RateLimitMiddleware
from rag_service.api.routers.conversations import router as conversations_router
from rag_service.api.routers.health import router as health_router
from rag_service.api.routers.ingest import router as ingest_router
from rag_service.api.routers.jobs import router as jobs_router
from rag_service.api.routers.documents import router as documents_router
from rag_service.api.routers.kg import router as kg_router
from rag_service.api.routers.query import router as query_router
from rag_service.api.routers.tenants import router as tenants_router
from rag_service.observability.metrics import metrics_router
from rag_service.observability.logging import configure_logging, request_id_var, tenant_id_var
from rag_service.config import settings

import redis.asyncio as aioredis_mw


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Configure logging
    configure_logging(json=True)
    log = logging.getLogger("rag_service")
    log.info("startup")

    # Start reload listener
    listener_task = None
    try:
        import redis.asyncio as aioredis
        from rag_service.core.rag_factory import get_cache
        from rag_service.core.reload_listener import start_in_background
        redis = aioredis.from_url(settings.redis_url)
        cache = get_cache()
        listener_task = start_in_background(redis, cache)
        app.state.redis = redis
        app.state.rag_cache = cache
        app.state.listener_task = listener_task
    except Exception as e:
        log.warning("reload listener init failed: %s", e)
        listener_task = None

    try:
        yield
    finally:
        log.info("shutdown")
        if listener_task is not None:
            listener_task.cancel()
            try:
                await listener_task
            except (asyncio.CancelledError, Exception):
                pass
        if hasattr(app.state, "rag_cache"):
            await app.state.rag_cache.aclose()
        if hasattr(app.state, "redis"):
            await app.state.redis.aclose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="rag-service",
        version="0.1.0",
        lifespan=lifespan,
    )

    # CORS — default open for v1; tighten in Phase 10
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Rate limiting (sliding window, Redis-backed). ``aioredis.from_url``
    # returns an unconnected pool — no I/O happens until the first request
    # hits the middleware, so installing it during create_app() can't fail
    # even when Redis is briefly unavailable. The middleware itself fails
    # open if Redis errors at request time.
    app.add_middleware(
        RateLimitMiddleware,
        redis=aioredis_mw.from_url(settings.redis_url, decode_responses=False),
    )

    # Request-ID middleware
    @app.middleware("http")
    async def request_id_mw(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid4().hex
        token_rid = request_id_var.set(rid)
        token_tid = None
        tid = request.headers.get("x-tenant-id")
        if tid:
            token_tid = tenant_id_var.set(tid)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token_rid)
            if token_tid is not None:
                tenant_id_var.reset(token_tid)
        response.headers["x-request-id"] = rid
        return response

    # Routers
    app.include_router(health_router)
    app.include_router(ingest_router)
    app.include_router(jobs_router)
    app.include_router(documents_router)
    app.include_router(query_router)
    app.include_router(tenants_router)
    app.include_router(kg_router)
    app.include_router(conversations_router)
    app.include_router(metrics_router)

    return app
