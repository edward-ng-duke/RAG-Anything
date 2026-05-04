# RAG-Anything 独立产品 — 实施任务列表

> **Source design:** [/home/edward/.claude/plans/onyx-integration-design-md-design-sleepy-alpaca.md](file:///home/edward/.claude/plans/onyx-integration-design-md-design-sleepy-alpaca.md) (approved α plan)
> **Status:** Planning complete — awaiting user approval before sub-agent dispatch.

**Goal:** Build RAG-Anything as a complete standalone multi-tenant product (auth + UI + KG browsing + multi-turn chat), backed by FastAPI + arq workers + PG (with pgvector + AGE) + Redis + Next.js.

**Architecture:** Mono-repo. RA library stays untouched in `raganything/`. New code lives in `apps/server/` (Python backend) and `apps/web/` (Next.js frontend). LightRAG storage uses PG backend in a dedicated `lightrag` schema; business data in `public`. MinerU defaults to Cloud API.

**Tech Stack:** Python 3.11 / FastAPI / arq / SQLAlchemy 2.0 async / asyncpg / alembic / Postgres 16 + pgvector + Apache AGE / Redis 7 / Next.js 15 / TypeScript / Tailwind / shadcn/ui / TanStack Query / sigma.js / docker-compose.

---

## Conventions

### Complexity tags
- **S** = ≤ ~50 LOC change, single concept, < 30 min
- **M** = ~50–250 LOC, multiple files OR new abstraction, 30–120 min
- (No **L** — must split before this plan goes to execution)

### Sub-agent dispatch protocol (orchestration rules)

Per user instructions:
1. Each task is dispatched via Task tool to a single sub-agent with **only** that task's spec, file allowlist, and verification steps.
2. Sub-agent prompt must include: "Complete ONLY this task. Do not refactor, rename, or edit anything outside the listed files. Return a short summary (files changed, verification result) — do not dump full file contents."
3. After return: main thread inspects diff, re-runs verification, confirms no out-of-scope changes. If failed → dispatch a focused fix task (don't fix inline).
4. Tasks must be done in declared dependency order.

### File path conventions
- Repo root: `/home/edward/research/RAG-Anything/`
- Backend code: `/home/edward/research/RAG-Anything/apps/server/`
- Frontend code: `/home/edward/research/RAG-Anything/apps/web/`
- Backend Python package: `apps/server/src/rag_service/`
- Backend tests: `apps/server/tests/`
- Frontend pages: `apps/web/app/` (Next.js App Router)
- Frontend components: `apps/web/components/`

### Universally out-of-scope (every task)
- `/home/edward/research/RAG-Anything/raganything/` (the RA library itself — never touch)
- `/home/edward/research/RAG-Anything/lightrag/` (LightRAG library — never touch)
- `/home/edward/research/RAG-Anything/.git/`
- `/home/edward/research/RAG-Anything/data/`, `samples/` (user's existing data, keep as-is)
- `/home/edward/research/onyx/` (separate project — fully separate per α decision)

---

## Phase 0: PoC (validate AGE + LightRAG PG + MinerU Cloud day 1)

**Goal:** Prove that LightRAG-PG-with-AGE backend + MinerU Cloud API + multi-tenant workspace isolation actually work end-to-end on a single PDF before building anything else. Stop and reconsider if anything blocks.

### Task 0.1: Create apps/ directory skeleton

**Complexity:** S
**Dependencies:** none

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/.gitkeep`
- Create: `/home/edward/research/RAG-Anything/apps/server/.gitkeep`
- Create: `/home/edward/research/RAG-Anything/apps/web/.gitkeep`
- Create: `/home/edward/research/RAG-Anything/apps/README.md` (one paragraph explaining the apps/ split)

**In-scope:** Just the directory placeholders + a top-level README.

**Out-of-scope:** No code, no config, no dependency manifest yet.

**Verification:**
```bash
ls -la /home/edward/research/RAG-Anything/apps/
```
Expected: `server/`, `web/`, `.gitkeep`, `README.md` present.

---

### Task 0.2: Write `apps/server/pyproject.toml` with PoC dependencies

**Complexity:** S
**Dependencies:** 0.1

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/server/pyproject.toml`

**In-scope:** Define a `rag-service` package using `pyproject.toml` (PEP 621). Dependencies for PoC stage: `raganything` (path-installed from `../../`, editable), `fastapi`, `uvicorn[standard]`, `arq`, `asyncpg`, `redis`, `sqlalchemy[asyncio]>=2.0`, `alembic`, `pydantic-settings`, `python-multipart`, `structlog`, `httpx`. Dev deps: `pytest`, `pytest-asyncio`, `testcontainers[postgres,redis]`, `ruff`.

**Out-of-scope:** Frontend deps. Lockfiles. Code.

**Verification:**
```bash
cd /home/edward/research/RAG-Anything/apps/server && python -c "import tomllib; tomllib.load(open('pyproject.toml','rb'))"
```
Expected: parses without error.

---

### Task 0.3: Write `docker-compose.poc.yml` with PG (custom AGE image) + Redis

**Complexity:** S
**Dependencies:** 0.1

**Files:**
- Create: `/home/edward/research/RAG-Anything/docker-compose.poc.yml`
- Create: `/home/edward/research/RAG-Anything/apps/server/docker/poc-pg.Dockerfile`
- Create: `/home/edward/research/RAG-Anything/apps/server/docker/init-poc.sql`

**In-scope:**
- `docker-compose.poc.yml` defines services: `pg` (uses custom Dockerfile), `redis` (redis:7-alpine).
- `poc-pg.Dockerfile` based on `apache/age:release_PG16_1.6.0` (or latest AGE image — sub-agent should check via `docker pull` for what's current). Adds `pgvector` extension installation.
- `init-poc.sql` runs at PG init: `CREATE EXTENSION IF NOT EXISTS vector;`, `CREATE EXTENSION IF NOT EXISTS age;`, `CREATE SCHEMA IF NOT EXISTS lightrag;`, `LOAD 'age';`, `SET search_path = ag_catalog, "$user", public;`.
- Volumes for PG data and Redis data persistence.
- Single network for service discovery.

**Out-of-scope:** Worker / API services (not yet built). Production hardening.

**Verification:**
```bash
cd /home/edward/research/RAG-Anything && docker compose -f docker-compose.poc.yml up -d pg redis
sleep 10
docker compose -f docker-compose.poc.yml exec -T pg psql -U rag -d rag -c "SELECT extname FROM pg_extension;"
```
Expected output contains: `vector` and `age` rows.

```bash
docker compose -f docker-compose.poc.yml exec -T pg psql -U rag -d rag -c "\dn"
```
Expected: `lightrag` schema exists.

After verification: `docker compose -f docker-compose.poc.yml down` (leave containers stopped).

---

### Task 0.4: Write `scripts/poc/poc_ingest_query.py` — single-PDF E2E PoC

**Complexity:** M
**Dependencies:** 0.2, 0.3

**Files:**
- Create: `/home/edward/research/RAG-Anything/scripts/poc/poc_ingest_query.py`
- Create: `/home/edward/research/RAG-Anything/scripts/poc/.env.poc.example`
- Read for context: `/home/edward/research/RAG-Anything/scripts/run_rag.py`, `/home/edward/research/RAG-Anything/scripts/mineru_cloud_parse.py`, `/home/edward/research/RAG-Anything/api_summary.md` §8

**In-scope:**
- Standalone Python script that:
  1. Reads env from `.env.poc` (LLM/embedding/MinerU credentials, PG conn string).
  2. Constructs `RAGAnything` with `lightrag_kwargs` pointing at the dockerized PG via `PGKVStorage` / `PGVectorStorage` / `PGGraphStorage` / `PGDocStatusStorage`.
  3. Sets `workspace="poc-tenant-1"`.
  4. Uses MinerU Cloud parser (mirroring `scripts/mineru_cloud_parse.py`'s call pattern).
  5. Calls `process_document_complete()` on a sample PDF from `/home/edward/research/RAG-Anything/data/` (sub-agent picks a small one and prints which it picked).
  6. Calls `aquery()` with a hardcoded test question, prints answer + sources.
  7. Then runs raw SQL via asyncpg: `SELECT count(*) FROM lightrag.LIGHTRAG_VDB_ENTITY WHERE workspace='poc-tenant-1';` and prints the count.
  8. Calls `await rag.finalize_storages()` cleanly before exit.

**Out-of-scope:** No FastAPI, no auth, no Redis, no worker — purely a sync script that exercises the storage layer.

**Verification:**
```bash
cd /home/edward/research/RAG-Anything
docker compose -f docker-compose.poc.yml up -d pg redis
cp scripts/poc/.env.poc.example scripts/poc/.env.poc
# user fills in MinerU + LLM credentials manually before running
uv run --with-editable apps/server python scripts/poc/poc_ingest_query.py
```
Expected:
- Prints "Picked: <pdf_path>"
- Prints "Ingestion complete in <Ns>"
- Prints "Query: ..." then "Answer: ..." (non-empty) and "Sources: [...]" (length ≥ 1)
- Prints "Entities in workspace 'poc-tenant-1': N" with N > 0
- Exits 0

**If this task fails**: stop the plan and re-evaluate. Do NOT proceed to Phase 1.

---

### Task 0.5: Write `docs/poc-results.md` — record PoC outcome

**Complexity:** S
**Dependencies:** 0.4

**Files:**
- Create: `/home/edward/research/RAG-Anything/docs/poc-results.md`

**In-scope:** Document the PoC run: timestamp, PDF used, Mineru tier used, LLM model used, ingestion time, query latency, entity count, any warnings/errors, screenshots of `\dt lightrag.*` output, and an explicit "GO/NO-GO" verdict for Phase 1.

**Out-of-scope:** Anything not directly observed during the PoC.

**Verification:** File exists, has all sections filled, ends with `**Verdict: GO**` or `**Verdict: NO-GO — see issues**`.

---

## Phase 1: Backend foundation (server skeleton + ingest/query/jobs/documents)

**Goal:** Get the FastAPI app + arq worker fully working, mirroring `RAG_SERVICE_DESIGN.md` API surface (with `X-Tenant-Id` header for now — Phase 2 will replace with JWT).

### Task 1.1: Backend package structure + config module

**Complexity:** S
**Dependencies:** 0.4

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/__init__.py`
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/config.py`
- Create: `/home/edward/research/RAG-Anything/apps/server/.env.example`

**In-scope:** `config.py` defines a `Settings` class via `pydantic-settings` reading env vars: `DATABASE_URL`, `REDIS_URL`, `INTERNAL_TOKEN`, `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, `EMBEDDING_BASE_URL`, `EMBEDDING_API_KEY`, `EMBEDDING_MODEL`, `VLM_MODEL`, `MINERU_CLOUD_API_KEY`, `PARSER_MODE` (default `mineru_cloud`), `DATA_DIR`, `MAX_UPLOAD_MB` (default 1000), `LRU_INSTANCE_CAP` (default 32). Singleton `settings = Settings()`. `.env.example` lists all keys with placeholder values.

**Out-of-scope:** JWT settings (Phase 2). LLM provider construction.

**Verification:**
```bash
cd /home/edward/research/RAG-Anything/apps/server
uv run python -c "from rag_service.config import settings; print(settings.model_dump_json(indent=2))"
```
Expected: prints JSON without errors.

---

### Task 1.2: Business-table SQLAlchemy models

**Complexity:** M
**Dependencies:** 1.1

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/db/__init__.py`
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/db/base.py`
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/db/models.py`

**In-scope:** SQLAlchemy 2.0 declarative models for `Tenant`, `Document`, `Job`, `QueryLog` matching the SQL spec in approved plan §"业务表（public schema）". `base.py` exports `Base = DeclarativeBase()` and `metadata`. Foreign keys with `ondelete="CASCADE"` where specified. UUID primary keys via `gen_random_uuid()` server default.

**Out-of-scope:** `users`, `tenants` (already here), `memberships`, `conversations`, `messages` — those are Phase 2 / Phase 4. LightRAG schema not modeled here (LightRAG manages it).

**Verification:**
```bash
cd /home/edward/research/RAG-Anything/apps/server
uv run python -c "from rag_service.db.models import Tenant, Document, Job, QueryLog, Base; print([t.name for t in Base.metadata.sorted_tables])"
```
Expected output (set, order may vary): `['tenants', 'documents', 'jobs', 'query_log']`.

---

### Task 1.3: Async session + engine

**Complexity:** S
**Dependencies:** 1.2

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/db/session.py`

**In-scope:** Async engine via `create_async_engine(settings.database_url, ...)`. `async_sessionmaker`. Async dep `get_db_session()` for FastAPI. `pool_size=20`, `max_overflow=10`, `pool_pre_ping=True`, `pool_recycle=1200`.

**Out-of-scope:** Migrations, repository pattern.

**Verification:**
```bash
cd /home/edward/research/RAG-Anything/apps/server
uv run python -c "import asyncio; from rag_service.db.session import async_session_maker; \
async def t():\n    async with async_session_maker() as s: print(await s.execute(__import__('sqlalchemy').text('SELECT 1')))\nasyncio.run(t())"
```
Expected: prints a Result object (not an exception). PG must be running.

---

### Task 1.4: Initial alembic setup + first migration

**Complexity:** M
**Dependencies:** 1.2

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/server/alembic.ini`
- Create: `/home/edward/research/RAG-Anything/apps/server/alembic/env.py`
- Create: `/home/edward/research/RAG-Anything/apps/server/alembic/script.py.mako`
- Create: `/home/edward/research/RAG-Anything/apps/server/alembic/versions/001_initial_business_tables.py`

**In-scope:** Wire alembic to read `Base.metadata` from `rag_service.db.base`. First migration creates `tenants`, `documents`, `jobs`, `query_log` tables + their indexes. `env.py` reads `DATABASE_URL` from env. Use `version_locations` pointing at `alembic/versions`.

**Out-of-scope:** Lightrag schema migration (LightRAG self-manages). Users/memberships table (Phase 2).

**Verification:**
```bash
cd /home/edward/research/RAG-Anything/apps/server
uv run alembic upgrade head
docker compose -f ../../docker-compose.poc.yml exec -T pg psql -U rag -d rag -c "\dt public.*"
```
Expected: 4 tables present. `alembic_version` table exists with one row.

---

### Task 1.5: `core/paths.py` — tenant_id validation + path safety

**Complexity:** S
**Dependencies:** 1.1

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/core/__init__.py`
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/core/paths.py`
- Create: `/home/edward/research/RAG-Anything/apps/server/tests/core/test_paths.py`

**In-scope:** Functions:
- `validate_tenant_id(tid: str) -> str` (regex `^[a-zA-Z0-9_-]{1,64}$`, raises `ValueError` on miss)
- `tenant_upload_dir(tid: str) -> Path`
- `tenant_working_dir(tid: str) -> Path`
- `document_upload_path(tid: str, document_id: UUID, ext: str) -> Path`
All paths must resolve under `settings.data_dir`. Use `path.resolve().is_relative_to(...)` to defeat traversal.

Tests cover: valid/invalid tenant_id (including `../`, empty, too long, special chars), traversal attempt via doc_id manipulation.

**Out-of-scope:** Disk I/O. File creation. Just path construction + validation.

**Verification:**
```bash
cd /home/edward/research/RAG-Anything/apps/server
uv run pytest tests/core/test_paths.py -v
```
Expected: all tests pass.

---

### Task 1.6: `core/llm_provider.py` — OpenAI-compatible LLM/embedding/VLM factories

**Complexity:** M
**Dependencies:** 1.1

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/core/llm_provider.py`
- Create: `/home/edward/research/RAG-Anything/apps/server/tests/core/test_llm_provider.py`

**In-scope:** Functions returning callables compatible with LightRAG/RAGAnything signatures:
- `make_llm_func(base_url, api_key, model) -> Callable`  → wraps OpenAI-compatible chat completions, returns `str`. Honors LightRAG's `system_prompt`, `history_messages` kwargs.
- `make_embedding_func(base_url, api_key, model) -> EmbeddingFunc`  → batched embedding, returns `EmbeddingFunc(embedding_dim=int, max_token_size=int, func=Callable)` (the LightRAG type).
- `make_vlm_func(base_url, api_key, model) -> Callable | None`

Tests with a mock httpx transport (no real API calls): verify request shape, kwargs propagation, error handling for 4xx/5xx.

**Out-of-scope:** Caching. Per-tenant overrides.

**Verification:**
```bash
cd /home/edward/research/RAG-Anything/apps/server
uv run pytest tests/core/test_llm_provider.py -v
```
Expected: all tests pass.

---

### Task 1.7: `parsers/mineru_cloud.py` — MinerU Cloud API wrapper

**Complexity:** M
**Dependencies:** 1.1
**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/parsers/__init__.py`
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/parsers/mineru_cloud.py`
- Create: `/home/edward/research/RAG-Anything/apps/server/tests/parsers/test_mineru_cloud.py`
- Read for context: `/home/edward/research/RAG-Anything/scripts/mineru_cloud_parse.py`

**In-scope:** Class `MineruCloudParser` exposing the same shape as RA's `Parser` ABC (likely `parse_document(file_path, output_dir, **kwargs) -> List[dict]`). Internally calls MinerU Cloud HTTP endpoints, polls until done, downloads results, returns content list. Sub-agent must read `scripts/mineru_cloud_parse.py` to discover exact API shape.

Tests with mocked HTTP: verify upload → poll → download flow; verify timeout handling; verify content_list shape.

**Out-of-scope:** Local GPU mode (env flag will gate parser selection elsewhere; this is just the cloud impl).

**Verification:**
```bash
cd /home/edward/research/RAG-Anything/apps/server
uv run pytest tests/parsers/test_mineru_cloud.py -v
```
Expected: tests pass.

---

### Task 1.8: `core/rag_factory.py` — RAGAnything LRU cache + lifecycle

**Complexity:** M
**Dependencies:** 1.6, 1.7

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/core/rag_factory.py`
- Create: `/home/edward/research/RAG-Anything/apps/server/tests/core/test_rag_factory.py`
- Read for context: `/home/edward/research/RAG-Anything/api_summary.md` §8 (lifecycle constraints), `/home/edward/research/RAG-Anything/raganything/raganything.py:50-450`

**In-scope:**
- Async LRU cache class `RAGAnythingCache` with capacity `settings.lru_instance_cap`.
- `get(tenant_id) -> RAGAnything` lazy-creates an instance configured with PG storage backends (workspace=tenant_id), MinerU Cloud parser, LLM/embedding/VLM funcs from `llm_provider`.
- On eviction: schedules `await rag.finalize_storages()` (must NOT block `get()`).
- `evict(tenant_id)` for explicit eviction (used by reload listener later).
- `aclose()` for graceful shutdown (finalize all instances).

Tests: cache hits/misses, eviction calls finalize, capacity enforcement.

**Out-of-scope:** Pubsub listener integration (Phase 1.13). Per-request LLM override.

**Verification:**
```bash
cd /home/edward/research/RAG-Anything/apps/server
uv run pytest tests/core/test_rag_factory.py -v
```
Expected: tests pass.

---

### Task 1.9: `worker/locks.py` — per-tenant Redis lock

**Complexity:** S
**Dependencies:** 1.1

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/worker/__init__.py`
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/worker/locks.py`
- Create: `/home/edward/research/RAG-Anything/apps/server/tests/worker/test_locks.py`

**In-scope:** Async context manager `tenant_ingest_lock(redis, tenant_id, ttl=600)` using `SET NX EX`. Releases via Lua script (compare token before del). Raises `LockBusy` if can't acquire within `acquire_timeout`.

Tests with real Redis (testcontainers): two concurrent acquires → second blocks/fails; release → second acquires.

**Out-of-scope:** Distributed locking edge cases (clock skew, partitions). `redlock` algorithm — single Redis is fine for v1.

**Verification:**
```bash
cd /home/edward/research/RAG-Anything/apps/server
uv run pytest tests/worker/test_locks.py -v
```
Expected: tests pass.

---

### Task 1.10: `worker/tasks.py` — `ingest_document` arq task

**Complexity:** M
**Dependencies:** 1.5, 1.8, 1.9

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/worker/tasks.py`
- Create: `/home/edward/research/RAG-Anything/apps/server/tests/worker/test_tasks_ingest.py`

**In-scope:** Async function `ingest_document(ctx, tenant_id: str, document_id: str)` (arq task signature):
1. Acquire per-tenant lock.
2. Update `jobs.status='running'`.
3. Look up `documents.storage_path`.
4. Get `RAGAnything` instance from cache.
5. Call `await rag.process_document_complete(file_path, output_dir=mineru_output_path)`.
6. Update `documents.status='indexed'`, `jobs.status='done'`.
7. Publish `tenant_reload:{tenant_id}` to Redis.
8. On exception: classify (parser error / LLM error / unknown); update `jobs.status='failed'` with `error_message`; for parser errors retry up to 2x.

Tests: happy path, parser error retried, LLM error not retried, lock contention raises.

**Out-of-scope:** `rebuild_index` task (next task).

**Verification:**
```bash
cd /home/edward/research/RAG-Anything/apps/server
uv run pytest tests/worker/test_tasks_ingest.py -v
```
Expected: tests pass.

---

### Task 1.11: `worker/tasks.py` — `rebuild_index` task

**Complexity:** M
**Dependencies:** 1.10

**Files:**
- Modify: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/worker/tasks.py` (append)
- Create: `/home/edward/research/RAG-Anything/apps/server/tests/worker/test_tasks_rebuild.py`

**In-scope:** Async function `rebuild_index(ctx, tenant_id: str)`:
1. Acquire lock with longer TTL (3600s).
2. Backup existing `working_dir` to `.bak` (only if `.bak` doesn't already exist — idempotency for resume).
3. List all `documents` for tenant where `status='indexed'`.
4. Re-call `process_document_complete()` per document (parser cache will short-circuit MinerU re-parse where possible).
5. Mark soft-deleted documents permanently removed.
6. On success: rm `.bak`, publish reload signal.
7. On failure: keep `.bak`, mark job failed, raise.

Tests: rebuild after deletion preserves remaining docs; existing `.bak` skips backup step; failure leaves `.bak`.

**Out-of-scope:** Optimistic deletion strategy ("filter at query time").

**Verification:**
```bash
cd /home/edward/research/RAG-Anything/apps/server
uv run pytest tests/worker/test_tasks_rebuild.py -v
```
Expected: tests pass.

---

### Task 1.12: `worker/settings.py` — arq WorkerSettings

**Complexity:** S
**Dependencies:** 1.10, 1.11

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/worker/settings.py`

**In-scope:** `WorkerSettings` class for arq: `redis_settings`, `functions=[ingest_document, rebuild_index]`, `on_startup` (init LRU cache, redis connection), `on_shutdown` (close cache, redis), `max_jobs=8`, `job_timeout=3600`.

**Out-of-scope:** Beat schedule (no periodic jobs in v1 backend).

**Verification:**
```bash
cd /home/edward/research/RAG-Anything/apps/server
uv run python -c "from rag_service.worker.settings import WorkerSettings; assert WorkerSettings.functions"
```
Expected: no error.

---

### Task 1.13: `core/reload_listener.py` — Redis pubsub eviction

**Complexity:** S
**Dependencies:** 1.8

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/core/reload_listener.py`
- Create: `/home/edward/research/RAG-Anything/apps/server/tests/core/test_reload_listener.py`

**In-scope:** Async background task: subscribe to pattern `tenant_reload:*`, on message extract tenant_id and call `cache.evict(tenant_id)`. Started by API on FastAPI lifespan.

Tests with real Redis: publish → eviction observed.

**Out-of-scope:** Hot-rebuild (just evict, lazy-rebuild on next query).

**Verification:**
```bash
cd /home/edward/research/RAG-Anything/apps/server
uv run pytest tests/core/test_reload_listener.py -v
```
Expected: tests pass.

---

### Task 1.14: API auth middleware (placeholder, tenant via header)

**Complexity:** S
**Dependencies:** 1.5

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/api/__init__.py`
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/api/auth.py`
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/api/deps.py`

**In-scope:** FastAPI dependency `current_tenant` that:
- Verifies `Authorization: Bearer <INTERNAL_TOKEN>` (constant-time compare).
- Reads `X-Tenant-Id` header, validates via `core/paths.validate_tenant_id`.
- Returns the validated tenant_id.

`deps.py` exports `get_db`, `get_redis`, `get_rag_cache` etc. for routers.

**Out-of-scope:** JWT (Phase 2 will fully replace this auth).

**Verification:** Implicitly tested via router tests (no dedicated test; sub-agent should add a small test that calls the dep with bad/missing token/tenant_id).

---

### Task 1.15: API router — `health.py`

**Complexity:** S
**Dependencies:** 1.3

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/api/routers/__init__.py`
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/api/routers/health.py`
- Create: `/home/edward/research/RAG-Anything/apps/server/tests/api/test_health.py`

**In-scope:** `/healthz` (always 200), `/readyz` (checks PG `SELECT 1`, Redis `PING`, `data_dir` writable; 200 if all pass else 503), `/metrics` (Prometheus exposition placeholder, full impl in 1.21).

**Out-of-scope:** Custom metrics.

**Verification:**
```bash
cd /home/edward/research/RAG-Anything/apps/server
uv run pytest tests/api/test_health.py -v
```
Expected: tests pass.

---

### Task 1.16: API router — `ingest.py`

**Complexity:** M
**Dependencies:** 1.5, 1.14

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/api/routers/ingest.py`
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/api/schemas.py`
- Create: `/home/edward/research/RAG-Anything/apps/server/tests/api/test_ingest.py`

**In-scope:**
- `POST /v1/ingest` accepts multipart `file`. Stream upload to disk via `aiofiles` (no full memory load). Compute SHA256 incrementally. Look up dedup by `(tenant_id, content_hash)`. If hit → return `deduplicated=true` without queueing. Else → insert `documents` row with `status='pending'`, insert `jobs` row, enqueue `ingest_document` arq task, return `{job_id, document_id, status: queued, deduplicated: false}`.
- MIME validation (magic bytes), size limit per `MAX_UPLOAD_MB`.
- `schemas.py` defines `IngestResponse`.

Tests: valid upload, oversized, dedup, bad MIME, missing auth.

**Out-of-scope:** Quota enforcement (Phase 10).

**Verification:**
```bash
cd /home/edward/research/RAG-Anything/apps/server
uv run pytest tests/api/test_ingest.py -v
```
Expected: tests pass.

---

### Task 1.17: API router — `jobs.py`

**Complexity:** S
**Dependencies:** 1.14

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/api/routers/jobs.py`
- Modify: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/api/schemas.py` (append `JobResponse`)
- Create: `/home/edward/research/RAG-Anything/apps/server/tests/api/test_jobs.py`

**In-scope:** `GET /v1/jobs/{job_id}` returning job status + progress. Tenant scope check (404 if not in current tenant).

Tests: 200 own job, 404 cross-tenant, 404 missing.

**Verification:**
```bash
cd /home/edward/research/RAG-Anything/apps/server
uv run pytest tests/api/test_jobs.py -v
```

---

### Task 1.18: API router — `documents.py`

**Complexity:** M
**Dependencies:** 1.14

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/api/routers/documents.py`
- Modify: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/api/schemas.py`
- Create: `/home/edward/research/RAG-Anything/apps/server/tests/api/test_documents.py`

**In-scope:**
- `GET /v1/documents` (cursor pagination, filter by status)
- `GET /v1/documents/{id}` (single doc)
- `DELETE /v1/documents/{id}` (soft delete + enqueue `rebuild_index`)
All scoped to current tenant.

Tests: list pagination, get own/cross-tenant, delete enqueues rebuild.

**Verification:**
```bash
cd /home/edward/research/RAG-Anything/apps/server
uv run pytest tests/api/test_documents.py -v
```

---

### Task 1.19: API router — `query.py`

**Complexity:** M
**Dependencies:** 1.8, 1.14

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/api/routers/query.py`
- Modify: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/api/schemas.py`
- Create: `/home/edward/research/RAG-Anything/apps/server/tests/api/test_query.py`

**In-scope:** `POST /v1/query` body `{question, mode, top_k, vlm_enhanced}`. Get RAG instance from cache, call `aquery()` (or `aquery_vlm_enhanced()` if flag), return answer + sources + latency_ms + token usage. Log to `query_log` (best-effort, don't fail request on log error).

Tests with mocked RAG instance: shapes, error mapping (LLM error → 502).

**Verification:**
```bash
cd /home/edward/research/RAG-Anything/apps/server
uv run pytest tests/api/test_query.py -v
```

---

### Task 1.20: API router — `tenants.py`

**Complexity:** S
**Dependencies:** 1.14

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/api/routers/tenants.py`
- Modify: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/api/schemas.py`
- Create: `/home/edward/research/RAG-Anything/apps/server/tests/api/test_tenants.py`

**In-scope:** `GET /v1/tenants/me` returns current tenant info (storage_quota_mb, current usage from `documents.file_size` sum, document count).

**Verification:**
```bash
cd /home/edward/research/RAG-Anything/apps/server
uv run pytest tests/api/test_tenants.py -v
```

---

### Task 1.21: `observability/` — structlog + Prometheus metrics

**Complexity:** S
**Dependencies:** 1.1

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/observability/__init__.py`
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/observability/logging.py`
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/observability/metrics.py`

**In-scope:**
- `logging.py`: structlog JSON renderer, contextvar-bound `tenant_id`/`request_id`/`job_id`.
- `metrics.py`: Prometheus collectors: `rag_query_latency_seconds` (Histogram), `rag_ingest_duration_seconds`, `rag_llm_tokens_total{type}`, `rag_active_rag_instances` (Gauge), `rag_queue_depth`, `rag_storage_used_mb{tenant_id}`. Expose `/metrics` content via `metrics_router`.

**Out-of-scope:** Auth, tracing.

**Verification:**
```bash
cd /home/edward/research/RAG-Anything/apps/server
uv run python -c "from rag_service.observability.metrics import REGISTRY; print(len(list(REGISTRY.collect())))"
```
Expected: prints a positive integer.

---

### Task 1.22: FastAPI app assembly

**Complexity:** S
**Dependencies:** 1.13, 1.15, 1.16, 1.17, 1.18, 1.19, 1.20, 1.21

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/api/app.py`
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/cli.py`

**In-scope:** `create_app()` factory: include all routers, register error handler, lifespan starts/stops reload listener + LRU cache, structlog setup, CORS middleware (default `*` for v1; will tighten in Phase 10), `request_id` middleware. `cli.py` defines entry points `rag-api` (uvicorn run) and `rag-worker` (arq run).

**Out-of-scope:** Rate limit middleware (Phase 10).

**Verification:**
```bash
cd /home/edward/research/RAG-Anything/apps/server
uv run uvicorn rag_service.api.app:create_app --factory --port 8001 &
sleep 3
curl -s http://localhost:8001/healthz
kill %1
```
Expected: `{"status":"ok"}` returned.

---

### Task 1.23: Dockerfile for server

**Complexity:** S
**Dependencies:** 1.22

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/server/Dockerfile`

**In-scope:** Multi-stage build: builder installs deps via uv; runner is python:3.11-slim with deps + source. ENTRYPOINT switchable via CMD between `rag-api` and `rag-worker`. Build context is repo root so `raganything/` is available for editable install.

**Verification:**
```bash
cd /home/edward/research/RAG-Anything
docker build -f apps/server/Dockerfile -t rag-service:test .
docker run --rm rag-service:test rag-api --version 2>&1 || true
```
Expected: builds successfully (the run is just to confirm entrypoint resolves).

---

### Task 1.24: docker-compose update — add api + worker services

**Complexity:** S
**Dependencies:** 1.23

**Files:**
- Create: `/home/edward/research/RAG-Anything/docker-compose.dev.yml`

**In-scope:** Full dev compose: `pg` (custom AGE image from 0.3), `redis`, `api` (port 8000), `worker`, named volumes. Same network. `.env` file referenced. Healthchecks.

**Out-of-scope:** Production deployment (Phase 10).

**Verification:**
```bash
cd /home/edward/research/RAG-Anything
docker compose -f docker-compose.dev.yml up -d --build
sleep 30
curl -s http://localhost:8000/readyz
docker compose -f docker-compose.dev.yml down
```
Expected: `{"status":"ready",...}` returned.

---

### Task 1.25: E2E test — ingest+query roundtrip

**Complexity:** M
**Dependencies:** 1.24

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/server/tests/e2e/test_ingest_query.py`
- Create: `/home/edward/research/RAG-Anything/apps/server/tests/e2e/conftest.py`

**In-scope:** pytest test using testcontainers to bring up real PG + Redis. Spawn API + worker subprocesses (or in-process). Curl-equivalent via httpx: ingest a tiny test PDF (use `data/` fixture or generate one in-test), poll job until `done`, query, assert sources non-empty.

**Out-of-scope:** Frontend.

**Verification:**
```bash
cd /home/edward/research/RAG-Anything/apps/server
uv run pytest tests/e2e/test_ingest_query.py -v --slow
```
Expected: passes within 5 minutes.

---

## Phase 2: Authentication (replace header-based with JWT)

### Task 2.1: User/membership models + alembic migration

**Complexity:** M
**Dependencies:** 1.4

**Files:**
- Modify: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/db/models.py` (append User, Membership)
- Create: `/home/edward/research/RAG-Anything/apps/server/alembic/versions/002_auth_tables.py`

**In-scope:** `User` (UUID pk, email unique, password_hash, display_name, timestamps, is_active), `Membership` (composite pk user_id+tenant_id, role).

**Out-of-scope:** OIDC fields (Phase 11+).

**Verification:**
```bash
cd /home/edward/research/RAG-Anything/apps/server
uv run alembic upgrade head
docker compose -f ../../docker-compose.dev.yml exec -T pg psql -U rag -d rag -c "\dt public.*"
```
Expected: `users`, `memberships` present.

---

### Task 2.2: `auth/password.py` — bcrypt utilities

**Complexity:** S
**Dependencies:** 1.1

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/auth/__init__.py`
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/auth/password.py`
- Create: `/home/edward/research/RAG-Anything/apps/server/tests/auth/test_password.py`

**In-scope:** `hash_password(plain) -> str` (bcrypt cost 12), `verify_password(plain, hash) -> bool`. Use `passlib[bcrypt]` or `bcrypt` directly.

**Verification:**
```bash
uv run pytest tests/auth/test_password.py -v
```

---

### Task 2.3: `auth/jwt.py` — JWT signing/validation

**Complexity:** M
**Dependencies:** 1.1

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/auth/jwt.py`
- Create: `/home/edward/research/RAG-Anything/apps/server/tests/auth/test_jwt.py`

**In-scope:** `create_access_token(user_id, tenant_id) -> str` (15min exp), `create_refresh_token(user_id) -> (token, jti)` (7d exp; jti stored in Redis), `decode_token(token) -> claims dict`, `revoke_refresh(jti)`. HS256 with `JWT_SECRET_KEY` from env (validate length ≥ 64 at app startup).

**Verification:**
```bash
uv run pytest tests/auth/test_jwt.py -v
```

---

### Task 2.4: `api/routers/auth.py` — signup/login/me

**Complexity:** M
**Dependencies:** 2.1, 2.2, 2.3

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/api/routers/auth.py`
- Modify: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/api/schemas.py`
- Create: `/home/edward/research/RAG-Anything/apps/server/tests/api/test_auth_basic.py`

**In-scope:**
- `POST /v1/auth/signup` `{email, password, display_name}` → creates user + auto-creates first tenant + owner membership.
- `POST /v1/auth/login` `{email, password}` → `{access_token, refresh_token, user, tenants}`.
- `GET /v1/auth/me` (auth required) → current user + tenants.

Tests: signup, login OK/wrong-pwd, me requires auth.

**Out-of-scope:** Refresh, logout, select_tenant (next task). Email verification.

**Verification:**
```bash
uv run pytest tests/api/test_auth_basic.py -v
```

---

### Task 2.5: `api/routers/auth.py` — refresh/logout/select_tenant

**Complexity:** M
**Dependencies:** 2.4

**Files:**
- Modify: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/api/routers/auth.py`
- Create: `/home/edward/research/RAG-Anything/apps/server/tests/api/test_auth_session.py`

**In-scope:**
- `POST /v1/auth/refresh` `{refresh_token}` → new access_token (validates jti not revoked).
- `POST /v1/auth/logout` → revokes refresh jti + adds access token to blacklist (Redis with TTL = remaining exp).
- `POST /v1/auth/select_tenant` `{tenant_id}` → validates membership, returns new access_token with new tenant claim.

**Verification:**
```bash
uv run pytest tests/api/test_auth_session.py -v
```

---

### Task 2.6: Replace `current_tenant` dep with JWT-based version

**Complexity:** M
**Dependencies:** 2.5

**Files:**
- Modify: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/api/auth.py`
- Modify: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/api/deps.py`
- Modify: All existing routers (`ingest`, `jobs`, `documents`, `query`, `tenants`) — replace bearer+header dep with new JWT dep
- Modify: All existing tests under `tests/api/` to use a `make_jwt(user_id, tenant_id)` helper

**In-scope:** Decode JWT, check blacklist, verify user active + membership of tenant in claim, return tenant_id. New helper `current_user` returns the User row.

**Out-of-scope:** Role-based authorization (Phase 11+).

**Verification:**
```bash
uv run pytest tests/api/ -v
```
Expected: all existing tests pass with JWT auth.

---

## Phase 3: Knowledge graph browsing endpoints

### Task 3.1: `kg/repository.py` — flat queries on LIGHTRAG_VDB_*

**Complexity:** M
**Dependencies:** 1.3

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/kg/__init__.py`
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/kg/repository.py`
- Create: `/home/edward/research/RAG-Anything/apps/server/tests/kg/test_repository.py`
- Read for context: `/home/edward/research/RAG-Anything/lightrag/kg/postgres_impl.py:6314-6462` (table layout)

**In-scope:** Async repo methods (parameterized SQL, no string concat):
- `list_entities(tenant_id, *, type=None, search=None, cursor=None, limit=50)`
- `get_entity(tenant_id, entity_id)`
- `list_relations(tenant_id, *, source=None, target=None, type=None, cursor=None, limit=50)`
- `get_chunk(tenant_id, chunk_id)`
- `stats(tenant_id)`

All filter by `workspace=tenant_id`.

**Out-of-scope:** Graph traversal (next task).

**Verification:**
```bash
uv run pytest tests/kg/test_repository.py -v
```

---

### Task 3.2: `kg/graph.py` — AGE cypher wrappers

**Complexity:** M
**Dependencies:** 3.1

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/kg/graph.py`
- Create: `/home/edward/research/RAG-Anything/apps/server/tests/kg/test_graph.py`
- Read for context: `/home/edward/research/RAG-Anything/lightrag/kg/postgres_impl.py:4605-4700` (cypher patterns + `_normalize_node_id`)

**In-scope:** Async functions:
- `neighbors(tenant_id, entity_id, depth=1) -> {nodes, edges}`
- `subgraph(tenant_id, entity_ids: list, depth=2) -> {nodes, edges}`

Internally constructs cypher queries via AGE's `cypher()` function. **Must reuse LightRAG's `_normalize_node_id` (or replicate it) to defeat cypher injection.** Query template uses parameterized values where AGE supports it; for entity IDs that go into cypher string, use the normalize helper exclusively.

Tests with seeded mini-graph in test PG: depth-1 / depth-2 / multi-source subgraph.

**Out-of-scope:** Graph algorithms (PageRank etc.).

**Verification:**
```bash
uv run pytest tests/kg/test_graph.py -v
```

---

### Task 3.3: `api/routers/kg.py` — 3 endpoint families (entities, relations, chunks)

**Complexity:** M
**Dependencies:** 3.1, 2.6

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/api/routers/kg.py`
- Modify: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/api/schemas.py`
- Create: `/home/edward/research/RAG-Anything/apps/server/tests/api/test_kg_flat.py`

**In-scope:**
- `GET /v1/kg/entities` (filter, cursor pagination)
- `GET /v1/kg/entities/{id}`
- `GET /v1/kg/relations`
- `GET /v1/kg/chunks/{id}`
- `GET /v1/kg/stats`

All use repo from 3.1, JWT-protected.

**Out-of-scope:** Graph traversal endpoints (next task).

**Verification:**
```bash
uv run pytest tests/api/test_kg_flat.py -v
```

---

### Task 3.4: `api/routers/kg.py` — graph endpoints (neighbors, subgraph)

**Complexity:** M
**Dependencies:** 3.2, 3.3

**Files:**
- Modify: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/api/routers/kg.py`
- Create: `/home/edward/research/RAG-Anything/apps/server/tests/api/test_kg_graph.py`

**In-scope:**
- `GET /v1/kg/entities/{id}/neighbors?depth=1`
- `GET /v1/kg/subgraph?entities=a,b,c&depth=2`

Validate `depth` ∈ {1, 2, 3}; refuse higher (perf bound).

**Verification:**
```bash
uv run pytest tests/api/test_kg_graph.py -v
```

---

## Phase 4: Multi-turn conversations + SSE streaming

### Task 4.1: Conversation/message models + migration

**Complexity:** S
**Dependencies:** 2.1

**Files:**
- Modify: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/db/models.py`
- Create: `/home/edward/research/RAG-Anything/apps/server/alembic/versions/003_conversations.py`

**In-scope:** `Conversation`, `Message` models per plan §"业务表".

**Verification:**
```bash
uv run alembic upgrade head
```

---

### Task 4.2: `conversations/repository.py`

**Complexity:** S
**Dependencies:** 4.1

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/conversations/__init__.py`
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/conversations/repository.py`
- Create: `/home/edward/research/RAG-Anything/apps/server/tests/conversations/test_repository.py`

**In-scope:** CRUD methods scoped to (tenant_id, user_id): list, create, get_with_messages, delete, append_message, recent_history(conv_id, n=10).

**Verification:**
```bash
uv run pytest tests/conversations/test_repository.py -v
```

---

### Task 4.3: `conversations/orchestrator.py` — history-aware aquery

**Complexity:** M
**Dependencies:** 4.2, 1.8

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/conversations/orchestrator.py`
- Create: `/home/edward/research/RAG-Anything/apps/server/tests/conversations/test_orchestrator.py`
- Read for context: `/home/edward/research/RAG-Anything/raganything/query.py:102-200` (verify if `aquery` accepts conversation_history kwarg)

**In-scope:** `async def stream_assistant_response(tenant_id, conversation_id, user_message_content, mode, top_k, vlm_enhanced) -> AsyncIterator[Event]`:
1. Persist user message.
2. Pull last N (default 10) messages, format as LightRAG-compatible history.
3. Call RA's `aquery(...)` with history kwarg if supported, else inject into system prompt manually.
4. Stream tokens (yield `{"event":"delta","content":"..."}`).
5. After completion, persist assistant message with sources.
6. Yield `{"event":"done","sources":[...]}`.

**Out-of-scope:** Tool calls / multi-step agents.

**Verification:**
```bash
uv run pytest tests/conversations/test_orchestrator.py -v
```

---

### Task 4.4: `api/routers/conversations.py`

**Complexity:** M
**Dependencies:** 4.2, 4.3, 2.6

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/api/routers/conversations.py`
- Modify: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/api/schemas.py`
- Create: `/home/edward/research/RAG-Anything/apps/server/tests/api/test_conversations.py`

**In-scope:**
- `GET /v1/conversations`
- `POST /v1/conversations`
- `GET /v1/conversations/{id}`
- `DELETE /v1/conversations/{id}`
- `POST /v1/conversations/{id}/messages` → SSE response (`text/event-stream`) wrapping orchestrator's iterator

**Verification:**
```bash
uv run pytest tests/api/test_conversations.py -v
```

---

## Phase 5: Frontend foundation (Next.js + auth)

### Task 5.1: Initialize Next.js 15 app

**Complexity:** S
**Dependencies:** 0.1

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/web/package.json`
- Create: `/home/edward/research/RAG-Anything/apps/web/tsconfig.json`
- Create: `/home/edward/research/RAG-Anything/apps/web/next.config.mjs`
- Create: `/home/edward/research/RAG-Anything/apps/web/tailwind.config.ts`
- Create: `/home/edward/research/RAG-Anything/apps/web/postcss.config.js`
- Create: `/home/edward/research/RAG-Anything/apps/web/app/layout.tsx`
- Create: `/home/edward/research/RAG-Anything/apps/web/app/page.tsx` (redirect to /documents when auth, else /login)
- Create: `/home/edward/research/RAG-Anything/apps/web/app/globals.css`
- Create: `/home/edward/research/RAG-Anything/apps/web/.eslintrc.json`
- Create: `/home/edward/research/RAG-Anything/apps/web/.gitignore`

**In-scope:** Boilerplate from `npx create-next-app@latest --typescript --tailwind --app --src-dir=false`. Adjust tsconfig paths (`@/*`).

**Verification:**
```bash
cd /home/edward/research/RAG-Anything/apps/web
npm install
npm run build
```
Expected: build succeeds.

---

### Task 5.2: Install + configure shadcn/ui core components

**Complexity:** S
**Dependencies:** 5.1

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/web/components.json`
- Create: `/home/edward/research/RAG-Anything/apps/web/lib/utils.ts`
- Create: `/home/edward/research/RAG-Anything/apps/web/components/ui/button.tsx`
- Create: `/home/edward/research/RAG-Anything/apps/web/components/ui/input.tsx`
- Create: `/home/edward/research/RAG-Anything/apps/web/components/ui/label.tsx`
- Create: `/home/edward/research/RAG-Anything/apps/web/components/ui/card.tsx`
- Create: `/home/edward/research/RAG-Anything/apps/web/components/ui/dialog.tsx`
- Create: `/home/edward/research/RAG-Anything/apps/web/components/ui/toast.tsx`
- Create: `/home/edward/research/RAG-Anything/apps/web/components/ui/sonner.tsx`

**In-scope:** Initial shadcn components needed for auth pages.

**Verification:**
```bash
cd /home/edward/research/RAG-Anything/apps/web
npm run build
```

---

### Task 5.3: API client + TanStack Query setup

**Complexity:** S
**Dependencies:** 5.1

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/web/lib/api/client.ts`
- Create: `/home/edward/research/RAG-Anything/apps/web/lib/api/types.ts`
- Create: `/home/edward/research/RAG-Anything/apps/web/lib/providers.tsx`
- Modify: `/home/edward/research/RAG-Anything/apps/web/app/layout.tsx` (wrap with providers)

**In-scope:** axios instance with auth interceptor reading JWT from `localStorage`/`sessionStorage`, refresh on 401, redirect to /login on refresh failure. TanStack Query client provider. Types module with shared response types.

**Out-of-scope:** Per-resource query hooks (added per page).

**Verification:**
```bash
cd /home/edward/research/RAG-Anything/apps/web && npm run build
```

---

### Task 5.4: Zustand stores (user/tenant)

**Complexity:** S
**Dependencies:** 5.3

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/web/lib/stores/auth.ts`

**In-scope:** Zustand store with: `accessToken`, `refreshToken`, `user`, `tenants`, `currentTenantId`. Persist via `persist` middleware → localStorage. Actions: `setSession`, `clear`, `selectTenant`.

**Verification:** TypeScript builds cleanly.

---

### Task 5.5: `/login` and `/signup` pages

**Complexity:** M
**Dependencies:** 5.2, 5.3, 5.4

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/web/app/(auth)/layout.tsx`
- Create: `/home/edward/research/RAG-Anything/apps/web/app/(auth)/login/page.tsx`
- Create: `/home/edward/research/RAG-Anything/apps/web/app/(auth)/signup/page.tsx`
- Create: `/home/edward/research/RAG-Anything/apps/web/lib/api/auth.ts`

**In-scope:** Forms with react-hook-form + zod. On submit: call `POST /v1/auth/login` or `signup`, set session, redirect to `/`. Sonner toast on errors.

**Verification:** Manual: start backend + frontend, register a user, login, see redirect.

---

### Task 5.6: Authenticated layout + tenant switcher

**Complexity:** M
**Dependencies:** 5.5

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/web/app/(app)/layout.tsx`
- Create: `/home/edward/research/RAG-Anything/apps/web/components/app-shell.tsx`
- Create: `/home/edward/research/RAG-Anything/apps/web/components/tenant-switcher.tsx`
- Create: `/home/edward/research/RAG-Anything/apps/web/components/auth-guard.tsx`

**In-scope:** Sidebar (Documents / Chat / Knowledge Graph / Settings) + top bar with tenant switcher dropdown + user menu. Auth guard redirects to `/login` when no token.

**Verification:** Manual: login → see app shell → switch tenant → JWT refreshes.

---

## Phase 6: Documents UI

### Task 6.1: `/documents` list page (read-only)

**Complexity:** M
**Dependencies:** 5.6

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/web/app/(app)/documents/page.tsx`
- Create: `/home/edward/research/RAG-Anything/apps/web/lib/api/documents.ts`
- Create: `/home/edward/research/RAG-Anything/apps/web/components/documents/document-table.tsx`
- Create: `/home/edward/research/RAG-Anything/apps/web/components/ui/table.tsx` (shadcn)
- Create: `/home/edward/research/RAG-Anything/apps/web/components/ui/badge.tsx` (shadcn)

**In-scope:** Table view (filename, size, status, uploaded_at, actions). TanStack Query hook `useDocuments()` paginated. Status badges color-coded.

**Verification:** Manual.

---

### Task 6.2: Upload component + status polling

**Complexity:** M
**Dependencies:** 6.1

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/web/components/documents/upload-dropzone.tsx`
- Create: `/home/edward/research/RAG-Anything/apps/web/lib/api/jobs.ts`
- Create: `/home/edward/research/RAG-Anything/apps/web/lib/hooks/use-job-polling.ts`

**In-scope:** Drag-and-drop multi-file with `react-dropzone` (or native HTML5). Progress bar per file (XHR `upload.onprogress`). After response → register `useJobPolling(job_id)` (refetch every 3s while not terminal). Updates documents table on completion.

**Verification:** Manual: upload a small PDF → observe progress → terminal status.

---

### Task 6.3: `/documents/{id}` detail page

**Complexity:** M
**Dependencies:** 6.1

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/web/app/(app)/documents/[id]/page.tsx`
- Create: `/home/edward/research/RAG-Anything/apps/web/components/documents/document-detail.tsx`

**In-scope:** Show metadata + chunks list (call kg/chunks via document_id link if available; else show parsed multimodal items). Delete button → confirmation dialog.

**Verification:** Manual.

---

## Phase 7: Chat UI

### Task 7.1: `/chat` conversation list + create

**Complexity:** M
**Dependencies:** 5.6

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/web/app/(app)/chat/page.tsx`
- Create: `/home/edward/research/RAG-Anything/apps/web/lib/api/conversations.ts`
- Create: `/home/edward/research/RAG-Anything/apps/web/components/chat/conversation-list.tsx`

**In-scope:** Sidebar list of conversations + "New" button. Empty state.

---

### Task 7.2: `/chat/[id]` page + message list rendering

**Complexity:** M
**Dependencies:** 7.1

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/web/app/(app)/chat/[id]/page.tsx`
- Create: `/home/edward/research/RAG-Anything/apps/web/components/chat/message-list.tsx`
- Create: `/home/edward/research/RAG-Anything/apps/web/components/chat/message-bubble.tsx`
- Create: `/home/edward/research/RAG-Anything/apps/web/components/chat/citation-card.tsx`

**In-scope:** Display existing messages with markdown rendering (`react-markdown` + `remark-gfm`). Citation cards under assistant messages, click → navigate to `/documents/{id}`.

---

### Task 7.3: SSE streaming send

**Complexity:** M
**Dependencies:** 7.2

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/web/components/chat/composer.tsx`
- Create: `/home/edward/research/RAG-Anything/apps/web/lib/api/sse.ts`
- Create: `/home/edward/research/RAG-Anything/apps/web/components/chat/options-popover.tsx`

**In-scope:** Composer with textarea + send button + popover for `mode`/`top_k`/`vlm_enhanced`. On send: POST messages endpoint, parse SSE stream, append delta tokens to assistant bubble in-place. On `done` event, attach sources.

**Verification:** Manual: send a question → see streaming response → citations appear.

---

## Phase 8: Knowledge graph UI

### Task 8.1: `/kg` overview + stats cards

**Complexity:** S
**Dependencies:** 5.6

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/web/app/(app)/kg/page.tsx`
- Create: `/home/edward/research/RAG-Anything/apps/web/lib/api/kg.ts`

**In-scope:** Stats cards (entities count, relations count, chunks count) + entry buttons to entities/relations/explore.

---

### Task 8.2: `/kg/entities` list page

**Complexity:** M
**Dependencies:** 8.1

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/web/app/(app)/kg/entities/page.tsx`
- Create: `/home/edward/research/RAG-Anything/apps/web/components/kg/entity-table.tsx`
- Create: `/home/edward/research/RAG-Anything/apps/web/components/ui/select.tsx` (shadcn)

**In-scope:** Table with type filter dropdown, search input (debounced), cursor pagination.

---

### Task 8.3: Sigma.js subgraph viewer component

**Complexity:** M
**Dependencies:** 8.1

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/web/components/kg/subgraph-viewer.tsx`
- Modify: `/home/edward/research/RAG-Anything/apps/web/package.json` (add sigma + graphology)

**In-scope:** React component receiving `{nodes, edges}` props, renders sigma graph in canvas. Zoom/pan, hover tooltip, click handler. Layout: forceAtlas2 stop after stabilization.

**Verification:** Manual: render with mock 50-node fixture, verify smooth interaction.

---

### Task 8.4: `/kg/entities/[id]` detail + neighbors

**Complexity:** M
**Dependencies:** 8.3

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/web/app/(app)/kg/entities/[id]/page.tsx`
- Create: `/home/edward/research/RAG-Anything/apps/web/components/kg/entity-detail.tsx`

**In-scope:** Top: name/type/desc. Middle: subgraph viewer with depth 1 (depth selector → re-fetch). Bottom: associated chunks list.

---

### Task 8.5: `/kg/relations` list + `/kg/explore`

**Complexity:** M
**Dependencies:** 8.3

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/web/app/(app)/kg/relations/page.tsx`
- Create: `/home/edward/research/RAG-Anything/apps/web/app/(app)/kg/explore/page.tsx`
- Create: `/home/edward/research/RAG-Anything/apps/web/components/kg/explore-canvas.tsx`

**In-scope:** Relations: filterable table. Explore: entity-search starting point, click node → expand neighbors and merge into canvas, right-side inspector.

---

## Phase 9: Settings UI

### Task 9.1: `/settings` shell + profile

**Complexity:** M
**Dependencies:** 5.6

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/web/app/(app)/settings/layout.tsx`
- Create: `/home/edward/research/RAG-Anything/apps/web/app/(app)/settings/profile/page.tsx`

**In-scope:** Settings nav (Profile / Tenant / LLM / Members / API Keys). Profile page edits display_name + change password.

---

### Task 9.2: Tenant + LLM config page

**Complexity:** M
**Dependencies:** 9.1

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/web/app/(app)/settings/tenant/page.tsx`
- Create: `/home/edward/research/RAG-Anything/apps/web/app/(app)/settings/llm/page.tsx`
- Modify backend: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/api/routers/tenants.py` (add PATCH for config)

**In-scope:** Tenant info + storage usage. LLM config: model/base_url/api_key (encrypted at rest via pgcrypto in backend), test button.

---

### Task 9.3: Members page

**Complexity:** M
**Dependencies:** 9.1

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/web/app/(app)/settings/members/page.tsx`
- Modify backend: add `/v1/tenants/{id}/members` endpoints in `tenants.py`

**In-scope:** List members, invite by email (sends a one-time link), remove member, change role.

---

### Task 9.4: API keys page

**Complexity:** M
**Dependencies:** 9.1

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/web/app/(app)/settings/api-keys/page.tsx`
- Modify backend: add `api_keys` table + endpoints `/v1/api-keys`

**In-scope:** Generate API key (one-time view), list (last4 only), revoke. Backend stores hashed key.

---

## Phase 10: Production hardening

### Task 10.1: `docker-compose.prod.yml`

**Complexity:** M
**Dependencies:** 1.24, 5.1

**Files:**
- Create: `/home/edward/research/RAG-Anything/docker-compose.prod.yml`
- Create: `/home/edward/research/RAG-Anything/apps/web/Dockerfile`

**In-scope:** Prod compose with: prebuilt images (no build context), restart policies, resource limits, healthchecks, logging driver, named volumes for backups. Web Dockerfile (Next.js standalone build).

---

### Task 10.2: Backup/restore scripts

**Complexity:** M
**Dependencies:** 10.1

**Files:**
- Create: `/home/edward/research/RAG-Anything/scripts/ops/backup.sh`
- Create: `/home/edward/research/RAG-Anything/scripts/ops/restore.sh`
- Create: `/home/edward/research/RAG-Anything/scripts/ops/restore_tenant.sh`

**In-scope:** `backup.sh` runs `pg_dump` (full) + `pg_dump --schema=public --schema=lightrag` (separable) + rsync data/ to `BACKUP_DIR`. `restore.sh` reverses. `restore_tenant.sh tenant_id` extracts a single tenant's data via filtered SQL + per-tenant working_dir.

---

### Task 10.3: Rate limiting middleware

**Complexity:** M
**Dependencies:** 2.6

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/api/rate_limit.py`
- Modify: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/api/app.py`

**In-scope:** Redis sliding-window per (user_id, route_class) and (tenant_id, route_class). 5/min on auth/signup/login. 60/min on default API. 429 with `Retry-After`.

---

### Task 10.4: Storage quota enforcement

**Complexity:** S
**Dependencies:** 1.16

**Files:**
- Modify: `/home/edward/research/RAG-Anything/apps/server/src/rag_service/api/routers/ingest.py`

**In-scope:** Pre-upload check sum of `documents.file_size` for tenant against `tenants.storage_quota_mb`; reject with 413 if would exceed.

---

### Task 10.5: README + deployment doc

**Complexity:** M
**Dependencies:** 10.1, 10.2, 10.3

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/server/README.md`
- Create: `/home/edward/research/RAG-Anything/apps/web/README.md`
- Create: `/home/edward/research/RAG-Anything/DEPLOY.md`

**In-scope:** Local dev setup, env reference, prod deployment walkthrough, backup runbook, troubleshooting (LightRAG migration windows, AGE upgrade, PG upgrade caveats).

---

### Task 10.6: Final E2E browser test (Playwright)

**Complexity:** M
**Dependencies:** all

**Files:**
- Create: `/home/edward/research/RAG-Anything/apps/web/tests/e2e/full-flow.spec.ts`
- Create: `/home/edward/research/RAG-Anything/apps/web/playwright.config.ts`

**In-scope:** Single Playwright spec: signup → login → upload tiny PDF → wait for indexed → chat 1 question → see citation → click citation → see document → open KG → see entity → click into subgraph.

**Verification:**
```bash
cd /home/edward/research/RAG-Anything/apps/web
npx playwright test
```

---

## Self-Review

**Spec coverage check:**
- Auth (D4): tasks 2.1–2.6 ✅
- Multi-tenancy (D2): enforced via `current_tenant` dep + tenant_id columns; verified in 1.x tests ✅
- LightRAG PG backend (D1): used in 0.4 PoC, 1.8 factory ✅
- MinerU Cloud default (D3): 1.7 + 1.8 ✅
- Mono-repo (D6): apps/server + apps/web layout, 0.1, 5.1 ✅
- Frontend (D5): Next.js + shadcn 5.x ✅
- KG browse: 3.x + 8.x ✅
- Multi-turn chat: 4.x + 7.x ✅
- Settings: 9.x ✅
- Production: 10.x ✅
- PoC milestone: 0.4 + 0.5, with explicit GO/NO-GO ✅

**Gaps:**
- "MinerU local GPU mode" mentioned in plan as fallback but no explicit task — sub-agent during 1.8 should at minimum leave a clear extension hook (a `Parser` interface + `mineru_cloud` impl); local GPU added in a future task as needed.
- `/forgot-password` was marked v2 in spec; no task — correct.
- Onboarding email verification (D4 said "v1 simple bcrypt+JWT") — no task, correct.
- LLM cache cleanup cron (risk #4 in spec): no dedicated task; document as a known operational task in 10.5 README runbook section.

**Type consistency:** All references to LightRAG storage classes use names from `lightrag/kg/__init__.py:1-46` (PGKVStorage etc.). Tenant-id type is `str` throughout. Workspace = tenant_id mapping is consistent.

**Total tasks:** 70 (all S/M, no L).

**Estimated wall-clock:** 16–17 weeks single-engineer (matches approved plan). Dispatch order should follow declared dependencies; many tasks within a phase parallelize across multiple engineers if available.

---

## Execution checkpoints

After each phase completes, the orchestrator (main thread) should:
1. Verify all tasks in the phase show ✅ in TodoWrite.
2. Run the phase's E2E verification (e.g., 1.25 for Phase 1).
3. Summarize progress to the user before starting the next phase.

If main context fills up, the orchestrator dumps state to `/home/edward/research/RAG-Anything/PLAN_PROGRESS.md` and continues from there.
