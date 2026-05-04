# Followups

Issues logged during alpha-product plan execution. See PLAN.md for tasks.

## Critical
(none yet)

## Medium
(none yet)

## Low
(none yet)

## Manual steps deferred
(none yet)

## Assumptions made
- Git strategy: feature branch on main checkout (not worktree). See main turn for rationale.
- Plan tasks lack explicit `git add` + commit-message lines in their "Verification" sections; subagents will commit using the paths from each task's "Files:" section and a message of form `feat(taskN.M): <task-title>`.

## Task 0.3
**Low — manual: docker build for PG image not run.** Verified compose syntax + Dockerfile shape only. Actual `docker compose -f docker-compose.poc.yml build` may surface AGE base-image apt-source issues, pgvector source-build failures (PG16 dev headers), or tag-resolution failures. Run before Task 0.4 (PoC) is attempted live.

## Task 0.4
**Medium — search_path config gap.** The α plan §"LightRAG 表（lightrag schema）" specifies LightRAG tables should live in schema `lightrag`, configured via `search_path = lightrag, public` in the connection. The PoC script and Task 1.8 (rag_factory) need to set search_path explicitly when initializing LightRAG's PG backend, otherwise tables end up in `public` and our SQL queries (e.g., Task 3.x) would need to be unqualified or use a different schema. The PoC script was hardened to try both qualified and unqualified — but production rag_factory MUST set search_path correctly. **Action:** Task 1.8 implementer must wire `options=-c search_path=lightrag,public` into the PG DSN passed to LightRAG, OR set per-session via `ALTER ROLE rag SET search_path` in init-poc.sql. Confirm before Phase 1.8 dispatch.

**Low — asyncpg optional gate in PoC.** PoC silently skips SQL entity count if asyncpg missing. Acceptable for PoC; production tasks should hard-require asyncpg.

**Low — multimodal off by default in PoC.** PoC ingestion skips images/tables to be fast; toggle `enable_image_processing=True` for full validation.

## Task 1.4
**Low — `.gitignore` strips `*.ini`.** `apps/server/alembic.ini` had to be `git add -f`. Future edits won't appear in `git status`. Add a per-dir `apps/server/.gitignore` with `!alembic.ini` exception, OR amend root `.gitignore` to scope `*.ini` more tightly. Track as a one-line cleanup.

## Task 1.21/1.22
**Low — prometheus-client missing from pyproject.toml deps.** Task 1.21 added observability/metrics.py importing `prometheus_client` but didn't add the dep. Task 1.22 implementer hit this when verifying create_app(). Locally installed via `uv pip install` to unblock. Add `prometheus-client` to runtime deps in apps/server/pyproject.toml at next opportunity.

## Task 3.3
**Medium — `type` kwarg routed to repo that doesn't accept it.** kg router passes `type=` to `repository.list_entities` / `list_relations`, but Task 3.1 dropped the `type` filter on entities (no `entity_type` column in LIGHTRAG_VDB_ENTITY) and didn't add it to relations either. Tests pass because they mock the repo. Real call would TypeError. Either remove the `type` query param from the router or add a graceful filter in the repo. To be addressed when Task 3.4 (AGE-based) adds type support natively.

## Task 2.1 (onyx KB router)

**Important — N+1 in `list_kbs`** (`apps/server/src/rag_service/api/routers/onyx_kb.py:79-83`): per-row `await get_onyx_kb(db, r.tenant_id)` fires N round-trips per page (default limit 50, no upper bound). Fix: extend `list_onyx_kbs` to return enriched dicts directly (single aggregated query), OR batch-fetch by id list. Affects ONYX list-KB latency once N grows.

**Important — `limit` query param missing upper bound** (same file, around line 73): the GET /v1/onyx/kb endpoint accepts `limit` int with hand-rolled `>= 1` validation only — no `le=200` cap. Caller could pass `limit=10000`, would be honored. Fix: change to `Query(50, ge=1, le=200)` for FastAPI-native 422 + DoS shape control. One-line fix; deferred only because it'd alter the test suite contract for Task 2.1.

**Note — `get_kb` post-auth 404 path is TOCTOU-only** (`onyx_kb.py` get_kb handler): `onyx_service_auth` already validated `X-Onyx-KB-Id` is a real source=onyx tenant; only a race between auth check and route handler causes the in-handler 404. Worth a comment, not a logic bug.

## Task 2.3 (onyx jobs router)

**Note** — `OnyxJobResponse.progress: dict | None` should be tightened to `dict[str, Any] | None` for stronger Pydantic v2 contract. (`onyx_schemas.py:~113`)

**Note** — `getattr(row, "retries", 0) or 0` in `onyx_jobs.py:45` is defensive against a column that exists; `row.retries or 0` is sufficient.

**Note** — `OnyxJobResponse.created_at` lacks `= None` default; minor inconsistency with the other timestamp fields. (`onyx_schemas.py:~105`)

## Task 3.1 (rate limit onyx)

**Note** — per-user INCR is consumed before per-token bucket check (`rate_limit.py:198-217`). Under sustained shared-token load that trips the token cap, every user's per-user quota is prematurely exhausted. Fix: check token bucket first, OR use a Lua script for atomicity, OR decrement on token-429.

**Note** — non-atomic INCR/EXPIRE pairs (one per bucket); a crash between INCR and EXPIRE leaves a TTL-less key. Pre-existing pattern in α; combining into Lua/pipeline would unify.

**Note** — `test_redis_failure_fails_open` only covers the per-user INCR failure path; the per-token INCR failure branch is not exercised. Minor coverage gap.

## Task 3.2 (observability)

**Note** — `test_all_8_onyx_metrics_registered` mis-named (lists 7 metrics; KG endpoint metric is reused, not new). Rename to `test_all_7_onyx_metrics_registered` for clarity.

**Note** — `_capture()` helper in `test_logging_onyx.py` replaces root handlers without teardown; could leak handler state across the suite under different ordering.

**Note** — `rag_onyx_documents_total{kb_id}` Gauge has unbounded label cardinality (one series per KB); for tenants in the thousands, consider periodic `clear()` on full reconcile or aggregate at scrape-time.

## Task 5.1 (smoke ping)

**Note** — `GET /v1/onyx/kg/entities` and `POST /v1/onyx/query/sync` returned 500 against the live uvicorn smoke. Cause: `TypeError: RAGAnything.__init__() got an unexpected keyword argument 'parser'`, originating from the pre-existing dirty edit on `raganything/parser.py` (untracked before this branch). Auth/KB/DB layers all passed; failure is inside the RAG factory glue, not the `/v1/onyx/*` surface. Will self-resolve once `raganything` package version is aligned with the consumer (rebuild or revert the local parser.py edit). All other endpoints p95 < 22ms.

## Phase 5 frontend
**Manual: `npm install` not run** for any 5.x task. TypeScript diagnostics on apps/web/ files are expected until operator runs `cd apps/web && npm install`. Build verification (`npm run build`) deferred to that step. After install, ESLint + tsc --noEmit should both pass cleanly across the frontend.

## PoC live run 2026-05-04 (verdict: GO)

**Low — `psycopg[binary]` + `pgvector` + `asyncpg` missing from `apps/server/pyproject.toml` runtime deps.** alembic env.py converts asyncpg DSN → sync psycopg DSN, but `psycopg` not installed; LightRAG PG impl imports `pgvector.asyncpg.register_vector` at module-load time; both were installed ad-hoc via `uv pip install` on the host venv to unblock the PoC. Action: add to `apps/server/pyproject.toml` runtime deps (not just dev) so docker images and CI runs pick them up automatically.

**Low — `apache/age:PG16_latest` tag does not exist.** The Dockerfile comment mentioned `release_PG16_1.5.0` as a fallback; `PG16_latest` was the preferred tag but DockerHub returns 404 for it. Switched `apps/server/docker/poc-pg.Dockerfile` to `release_PG16_1.5.0`. Document this in the README/deploy docs so future operators don't chase the missing tag.

**Low — pgvector source pre-downloaded on host.** Docker daemon's container network couldn't reach `github.com:443` reliably (timeouts at 136s). Worked around by pre-downloading `pgvector-v0.7.4.tar.gz` from `codeload.github.com` on the host and `COPY`ing it into the build image. The tarball lives at `apps/server/docker/pgvector.tar.gz` (force-added to git via `git add -f`). Re-pin pgvector version when bumping; consider switching to PGDG apt source if outbound network becomes reliable.

**Low — `len(sources) == 0` in PoC script.** LightRAG `aquery` returns answer + context; PoC reads `result.sources` which is the wrong field. Chunks ARE used (log shows `Final context: ... 4 chunks`). Fix the extraction in `scripts/poc/poc_ingest_query.py` if reused; production API layer (Phase 4 chat router) constructs its own response envelope from the proper context structure, so this does not block.

**Low — host port collisions.** Default ports 8000 (whisperx-private-gpu) and 3000 (open-webui) on user's host were already bound. Changed `docker-compose.dev.yml` mappings to 8800:8000 (api) and 3300:3000 (web), updated `NEXT_PUBLIC_API_BASE_URL` in `.env`. If publishing the dev compose to other devs, document these are dev-only ports and should be re-mapped if conflicts arise.
