"""Async LRU cache + lifecycle management for ``RAGAnything`` instances.

A single :class:`RAGAnythingCache` lives per process. It memoises one
``RAGAnything`` instance per ``tenant_id`` (LightRAG ``workspace``), backed
by Postgres storages (PG/pgvector/PGGraph/PGDocStatus). The cache is bounded
(``capacity`` slots, default :attr:`Settings.lru_instance_cap`) and least
recently used instances are finalised before being evicted.

Concurrency model
-----------------
* ``self._lock`` guards mutations of the underlying ``OrderedDict`` and the
  per-tenant build-lock map.
* ``self._build_locks[tid]`` serialises construction of a single tenant's
  instance — without it, two concurrent :py:meth:`get` calls for the same
  ``tid`` would both miss the cache and both build a (heavy) RAG instance,
  one of which would be discarded ("thundering herd").
* :py:meth:`evict` schedules ``finalize_storages`` as an :class:`asyncio.Task`
  so callers don't block on async cleanup.
* :py:meth:`aclose` awaits all in-flight finalisations on shutdown.

Postgres wiring
---------------
LightRAG's :mod:`lightrag.kg.postgres_impl` reads connection parameters from
discrete ``POSTGRES_*`` environment variables (host, port, user, password,
database). It does **not** consume a single DSN. We therefore parse
``settings.database_url`` once and ``os.environ.setdefault(...)`` each
component before constructing the first ``RAGAnything``.

Per-instance schema isolation is configured via ``POSTGRES_SERVER_SETTINGS``,
which LightRAG splits on ``&`` into asyncpg ``server_settings`` (applied at
connection time). We pass ``search_path=lightrag,public`` so all tables
created by LightRAG live in the dedicated ``lightrag`` schema rather than
``public`` (which we reserve for our own business tables).
"""

from __future__ import annotations

import asyncio
import collections
import os
from functools import lru_cache
from typing import Any
from urllib.parse import urlparse

from raganything import RAGAnything, RAGAnythingConfig

from rag_service.config import settings
from rag_service.core.llm_provider import (
    make_embedding_func,
    make_llm_func,
    make_vlm_func,
)
from rag_service.core.paths import tenant_working_dir, validate_tenant_id


def _populate_postgres_env_from_dsn(database_url: str) -> None:
    """Parse ``database_url`` and ``setdefault`` ``POSTGRES_*`` env vars.

    We use ``setdefault`` so an operator who has already exported one of
    these vars (e.g. for SSL or non-standard tuning) wins over the
    URL-derived value.

    Also sets ``POSTGRES_SERVER_SETTINGS`` to ``search_path=lightrag,public``
    when unset — this is what makes LightRAG's CREATE-TABLE statements land
    in our dedicated schema. (See module docstring for the rationale.)
    """
    parsed = urlparse(database_url)
    if parsed.hostname:
        os.environ.setdefault("POSTGRES_HOST", parsed.hostname)
    if parsed.port:
        os.environ.setdefault("POSTGRES_PORT", str(parsed.port))
    if parsed.username:
        os.environ.setdefault("POSTGRES_USER", parsed.username)
    if parsed.password:
        os.environ.setdefault("POSTGRES_PASSWORD", parsed.password)
    if parsed.path and parsed.path != "/":
        # urlparse leaves the leading "/" on the path
        os.environ.setdefault("POSTGRES_DATABASE", parsed.path.lstrip("/"))
    # search_path goes via server_settings; LightRAG parses the
    # "k=v&k=v" form into asyncpg's ``server_settings`` mapping.
    os.environ.setdefault(
        "POSTGRES_SERVER_SETTINGS", "search_path=lightrag,public"
    )


def _build_lightrag_kwargs(tenant_id: str) -> dict[str, Any]:
    """Construct the ``lightrag_kwargs`` dict for a tenant's RAGAnything.

    All LightRAG storages are pinned to their Postgres implementations.
    ``vector_db_storage_cls_kwargs`` includes the cosine threshold; LightRAG
    refuses to start without it.
    """
    working_dir = tenant_working_dir(tenant_id)
    working_dir.mkdir(parents=True, exist_ok=True)
    return {
        "working_dir": str(working_dir),
        "workspace": tenant_id,
        "kv_storage": "PGKVStorage",
        "vector_storage": "PGVectorStorage",
        "graph_storage": "PGGraphStorage",
        "doc_status_storage": "PGDocStatusStorage",
        "vector_db_storage_cls_kwargs": {
            "cosine_better_than_threshold": 0.5,
        },
    }


class RAGAnythingCache:
    """Bounded async LRU keyed by ``tenant_id``.

    A single instance lives at the module level (see :func:`get_cache`).
    The class is testable in isolation by passing a ``capacity`` and a
    ``builder`` (which defaults to :py:meth:`_build`).
    """

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError(f"capacity must be >= 1, got {capacity}")
        self._capacity = capacity
        self._cache: "collections.OrderedDict[str, RAGAnything]" = (
            collections.OrderedDict()
        )
        self._lock = asyncio.Lock()
        self._build_locks: dict[str, asyncio.Lock] = {}
        # Track outstanding finalisation tasks so aclose() can drain them.
        self._pending_finalisations: set[asyncio.Task[None]] = set()
        self._postgres_env_initialised = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get(self, tenant_id: str) -> RAGAnything:
        """Return the cached ``RAGAnything`` for ``tenant_id``, building if absent.

        Concurrent calls for the same ``tenant_id`` only build once, courtesy
        of a per-tenant build lock acquired *outside* ``self._lock`` (so we
        don't block other tenants' lookups during a slow build).
        """
        validate_tenant_id(tenant_id)

        # Fast path: cache hit.
        async with self._lock:
            if tenant_id in self._cache:
                self._cache.move_to_end(tenant_id)
                return self._cache[tenant_id]
            build_lock = self._build_locks.setdefault(tenant_id, asyncio.Lock())

        # Slow path: build under the per-tenant lock. Re-check after acquire
        # in case another waiter built it while we queued.
        async with build_lock:
            async with self._lock:
                if tenant_id in self._cache:
                    self._cache.move_to_end(tenant_id)
                    return self._cache[tenant_id]

            instance = await self._build(tenant_id)

            async with self._lock:
                self._cache[tenant_id] = instance
                self._cache.move_to_end(tenant_id)
                # Evict LRU if we just exceeded capacity.
                evicted: list[tuple[str, RAGAnything]] = []
                while len(self._cache) > self._capacity:
                    victim_tid, victim_inst = self._cache.popitem(last=False)
                    evicted.append((victim_tid, victim_inst))

            for victim_tid, victim_inst in evicted:
                self._schedule_finalize(victim_tid, victim_inst)
            return instance

    async def evict(self, tenant_id: str) -> None:
        """Remove ``tenant_id`` from the cache and schedule finalisation.

        No-op if ``tenant_id`` is absent. Finalisation runs as a background
        task — the caller doesn't wait for storage flush.
        """
        validate_tenant_id(tenant_id)
        async with self._lock:
            instance = self._cache.pop(tenant_id, None)
        if instance is not None:
            self._schedule_finalize(tenant_id, instance)

    async def aclose(self) -> None:
        """Finalise every cached instance and drop them.

        Awaits all in-flight finalisations scheduled by prior ``evict`` /
        capacity-trigger calls so we don't return while storage flushes
        are still pending.
        """
        async with self._lock:
            cached = list(self._cache.items())
            self._cache.clear()
        # Finalise sequentially; we don't expect dozens of instances at
        # shutdown and serial cleanup gives clearer error attribution.
        for tid, instance in cached:
            try:
                await instance.finalize_storages()
            except Exception:
                # Continue closing the rest; surface no exception to caller
                # so a single broken instance doesn't strand the others.
                pass
        # Drain any tasks scheduled by evict() / capacity eviction.
        if self._pending_finalisations:
            await asyncio.gather(
                *self._pending_finalisations, return_exceptions=True
            )
            self._pending_finalisations.clear()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _schedule_finalize(
        self, tenant_id: str, instance: RAGAnything
    ) -> None:
        """Spawn a background task to finalise ``instance``'s storages."""

        async def _runner() -> None:
            try:
                await instance.finalize_storages()
            except Exception:
                # The cache is no longer authoritative for this tenant;
                # swallow to avoid unhandled-task warnings on shutdown.
                pass

        task = asyncio.create_task(
            _runner(), name=f"finalize-rag-{tenant_id}"
        )
        self._pending_finalisations.add(task)
        task.add_done_callback(self._pending_finalisations.discard)

    async def _build(self, tenant_id: str) -> RAGAnything:
        """Construct + initialise a fresh ``RAGAnything`` for ``tenant_id``.

        First call also seeds the ``POSTGRES_*`` environment variables that
        LightRAG's storage classes read at construction time.
        """
        if not self._postgres_env_initialised:
            _populate_postgres_env_from_dsn(settings.database_url)
            self._postgres_env_initialised = True

        llm_func = make_llm_func(
            settings.llm_base_url, settings.llm_api_key, settings.llm_model
        )
        embedding_func = make_embedding_func(
            settings.embedding_base_url,
            settings.embedding_api_key,
            settings.embedding_model,
        )
        vlm_func = make_vlm_func(
            settings.llm_base_url, settings.llm_api_key, settings.vlm_model
        )

        # Parser selection: use mineru.net cloud parser when configured,
        # otherwise fall through to RAGAnything's default (local mineru).
        parser = None
        if (
            settings.parser_mode == "mineru_cloud"
            and settings.mineru_cloud_api_key
        ):
            # Lazy import: keeps the rag_factory module importable in tests
            # that don't exercise the cloud parser path.
            from rag_service.parsers.mineru_cloud import get_default_parser

            parser = get_default_parser()

        rag_config = RAGAnythingConfig(
            working_dir=str(tenant_working_dir(tenant_id)),
        )

        kwargs: dict[str, Any] = {
            "config": rag_config,
            "llm_model_func": llm_func,
            "embedding_func": embedding_func,
            "lightrag_kwargs": _build_lightrag_kwargs(tenant_id),
        }
        if vlm_func is not None:
            kwargs["vision_model_func"] = vlm_func
        if parser is not None:
            kwargs["parser"] = parser

        instance = RAGAnything(**kwargs)
        # If the RAGAnything implementation exposes an explicit async
        # initialise hook we honour it; the upstream class instead lazy-
        # initialises on first use, so we tolerate either shape.
        init = getattr(instance, "initialize", None)
        if init is not None and asyncio.iscoroutinefunction(init):
            await init()
        return instance


@lru_cache(maxsize=1)
def get_cache() -> RAGAnythingCache:
    """Return the process-wide :class:`RAGAnythingCache` singleton.

    Capacity is taken from :attr:`Settings.lru_instance_cap`. Cached via
    :func:`functools.lru_cache` so repeated callers don't incur lock or
    settings overhead — the underlying object's own locks are what govern
    concurrency.
    """
    return RAGAnythingCache(capacity=settings.lru_instance_cap)
