from contextlib import asynccontextmanager
from uuid import uuid4
import asyncio
import ipaddress
import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from rag_service.api.rate_limit import RateLimitMiddleware
from rag_service.api.routers.conversations import router as conversations_router
from rag_service.api.routers.health import router as health_router
from rag_service.api.routers.ingest import router as ingest_router
from rag_service.api.routers.jobs import router as jobs_router
from rag_service.api.routers.documents import router as documents_router
from rag_service.api.routers.kg import router as kg_router
from rag_service.api.routers.query import router as query_router
from rag_service.api.routers.tenants import router as tenants_router
from rag_service.api.routers.onyx_kb import router as onyx_kb_router
from rag_service.api.routers.onyx_documents import router as onyx_documents_router
from rag_service.api.routers.onyx_jobs import router as onyx_jobs_router
from rag_service.api.routers.onyx_query import router as onyx_query_router
from rag_service.api.routers.onyx_kg import router as onyx_kg_router
from rag_service.observability.metrics import metrics_router
from rag_service.observability.logging import (
    configure_logging,
    onyx_user_id_var,
    request_id_var,
    tenant_id_var,
)
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


class OnyxIpAllowlistMiddleware(BaseHTTPMiddleware):
    """Global short-circuit 403 for ``/v1/onyx/*`` callers outside the CIDR allowlist.

    The per-route ``onyx_service_auth*`` dep already enforces the same
    rule, but a global middleware lets us reject blocked IPs *before*
    the rate-limit middleware increments any counters — otherwise a
    DDoS originating outside the allowlist could still saturate the
    rate-limit Redis with INCRs. When ``settings.onyx_backend_allowed_cidrs``
    is empty the middleware is a no-op (matches the per-dep behavior).

    Caller-IP resolution mirrors :func:`auth_onyx._resolve_caller_ip`:
    XFF first hop wins, fallback to ``request.client.host``. Any failure
    to resolve / parse the IP collapses to the same 403 detail string
    so the error never leaks whether the IP was missing vs. outside the
    allowlist.
    """

    async def dispatch(self, request: Request, call_next):
        cidrs = settings.onyx_backend_allowed_cidrs
        if not cidrs or not request.url.path.startswith("/v1/onyx/"):
            return await call_next(request)

        # Resolve caller IP — XFF first hop, then request.client.host.
        ip_str: str | None = None
        xff = request.headers.get("x-forwarded-for")
        if xff:
            first = xff.split(",", 1)[0].strip()
            if first:
                ip_str = first
        if ip_str is None and request.client is not None and request.client.host:
            ip_str = request.client.host
        if ip_str is None:
            return JSONResponse(
                {"detail": "caller ip not allowed"}, status_code=403
            )

        try:
            addr = ipaddress.ip_address(ip_str)
        except ValueError:
            return JSONResponse(
                {"detail": "caller ip not allowed"}, status_code=403
            )

        for cidr in cidrs:
            try:
                net = ipaddress.ip_network(cidr, strict=False)
            except ValueError:
                # Settings validator should have caught this at startup;
                # treat malformed entries as no-match rather than 500.
                continue
            if addr.version != net.version:
                continue
            if addr in net:
                return await call_next(request)

        return JSONResponse(
            {"detail": "caller ip not allowed"}, status_code=403
        )


def create_app() -> FastAPI:
    app = FastAPI(
        title="rag-service",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Middleware registration order note (Starlette/FastAPI): with
    # ``add_middleware``, the LAST registered middleware is the
    # OUTERMOST — i.e. it runs FIRST on inbound requests. We want the
    # inbound chain to be:
    #     OnyxIpAllowlist → RateLimit → CORS → request_id_mw → onyx_context_mw → app
    # so blocked IPs short-circuit before rate limiting touches Redis.
    # CORS is added first (innermost of the three), then RateLimit,
    # then OnyxIpAllowlist (outermost / runs first inbound).

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

    # IP allowlist for /v1/onyx/* — added LAST so it runs FIRST inbound,
    # short-circuiting 403 before the rate limiter counts the request.
    app.add_middleware(OnyxIpAllowlistMiddleware)

    # Request-ID middleware. Also binds tenant_id_var (alpha) and
    # onyx_user_id_var (onyx) so structlog records emitted during the
    # request carry the right tags.
    @app.middleware("http")
    async def request_id_mw(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid4().hex
        token_rid = request_id_var.set(rid)
        token_tid = None
        token_oxu = None
        tid = request.headers.get("x-tenant-id")
        if tid:
            token_tid = tenant_id_var.set(tid)
        # Bind X-Onyx-User-Id for /v1/onyx/* requests so log records emitted
        # while serving the request carry the caller id (Task 3.3).
        if request.url.path.startswith("/v1/onyx/"):
            oxuid = request.headers.get("x-onyx-user-id")
            if oxuid:
                token_oxu = onyx_user_id_var.set(oxuid)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token_rid)
            if token_tid is not None:
                tenant_id_var.reset(token_tid)
            if token_oxu is not None:
                onyx_user_id_var.reset(token_oxu)
        response.headers["x-request-id"] = rid
        return response

    # Routers — alpha API surface
    app.include_router(health_router)
    app.include_router(ingest_router)
    app.include_router(jobs_router)
    app.include_router(documents_router)
    app.include_router(query_router)
    app.include_router(tenants_router)
    app.include_router(kg_router)
    app.include_router(conversations_router)
    app.include_router(metrics_router)

    # Routers — /v1/onyx/* surface (service-to-service, INTERNAL_TOKEN auth)
    app.include_router(onyx_kb_router)
    app.include_router(onyx_documents_router)
    app.include_router(onyx_jobs_router)
    app.include_router(onyx_query_router)
    app.include_router(onyx_kg_router)

    return app
