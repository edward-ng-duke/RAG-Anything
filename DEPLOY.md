# Deploy — RAG-Anything alpha-product

Runbook for the alpha-product backend (`apps/server`) + web frontend
(`apps/web`). Two compose files live at the repo root:

- `docker-compose.dev.yml` — local development (ports exposed, hot rebuilds OK)
- `docker-compose.prod.yml` — production-ish (env-driven, healthchecks, restart
  policies, resource limits, no required ports beyond api/web)

## 1. Prereqs

- **Docker Engine 24+** with the `docker compose` v2 plugin
- ~6 GB RAM free for the api/worker/pg containers (defaults cap at 4 GB each)
- A Postgres image with **Apache AGE** and **pgvector**. `apps/server/docker/poc-pg.Dockerfile` builds one for you (used by both compose files); managed Postgres providers that don't ship AGE are not supported in the alpha
- An OpenAI-compatible LLM + embedding endpoint (or the `MINERU_CLOUD_API_KEY` for cloud parsing)

## 2. Local dev

```bash
docker compose -f docker-compose.dev.yml up --build
# api    -> http://localhost:8800   (docs at /docs, health at /healthz)
# web    -> http://localhost:3300
# pg     -> localhost:5532          (shifted from 5432 to coexist with onyx)
# redis  -> localhost:6479          (shifted from 6379 to coexist with onyx)
```

A thin top-level `Makefile` wraps the same compose: `make dev`, `make dev-bg`,
`make dev-down`, `make dev-logs`, `make dev-migrate`, `make dev-rebuild`,
`make dev-nuke`. Pick whichever style you prefer.

Migrations run automatically when `rag-api` starts. To run them by hand:

```bash
docker compose -f docker-compose.dev.yml exec api uv run alembic upgrade head
```

## 3. Production deploy

### 3.1 Required env vars

Create `.env` next to `docker-compose.prod.yml` (or export inline). The
following are **required** and the compose file will refuse to start without
them:

```dotenv
POSTGRES_PASSWORD=<strong-random>
INTERNAL_TOKEN=<random-32-bytes-hex>
JWT_SECRET_KEY=<random-64-plus-chars>
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=sk-...
LLM_MODEL=gpt-4o-mini
EMBEDDING_BASE_URL=https://api.openai.com/v1
EMBEDDING_API_KEY=sk-...
EMBEDDING_MODEL=text-embedding-3-small
```

Optional but commonly set:

```dotenv
PARSER_MODE=mineru_cloud
MINERU_CLOUD_API_KEY=...
VLM_MODEL=gpt-4o-mini
NEXT_PUBLIC_API_BASE_URL=https://rag.example.com   # browser-reachable origin
PG_IMAGE=ghcr.io/your-org/rag-pg-age:1.0.0          # use a registry image
API_IMAGE=ghcr.io/your-org/rag-service:1.0.0
WEB_IMAGE=ghcr.io/your-org/rag-web:1.0.0
```

### 3.2 Build images

If you're not pulling from a registry:

```bash
# api + worker (build context = repo root)
docker build -f apps/server/Dockerfile -t rag-service:latest .

# web (build context = apps/web; do NOT use repo root)
docker build -f apps/web/Dockerfile -t rag-web:latest apps/web

# pg with AGE + pgvector
docker build -f apps/server/docker/poc-pg.Dockerfile -t rag-pg-age:latest apps/server/docker
```

### 3.3 Start

```bash
docker compose -f docker-compose.prod.yml --env-file .env up -d
docker compose -f docker-compose.prod.yml ps
```

Check health: `curl -fsS http://localhost:${API_PORT:-8000}/healthz`.

## 4. Backups

Two things to back up:

1. **Postgres** (users, documents metadata, conversations, KG vectors + AGE graph)
2. **`rag-data` volume** (per-tenant LightRAG working dirs and uploaded source files)

Quick recipe (until Task 10.2 lands proper scripts under `scripts/ops/`):

```bash
# Postgres dump
docker compose -f docker-compose.prod.yml exec -T pg \
    pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB" > backup-$(date +%F).dump

# rag-data tarball (read-only mount into a throwaway container)
docker run --rm -v rag-anything_rag-data:/data -v "$PWD":/out alpine \
    tar czf /out/rag-data-$(date +%F).tgz -C / data
```

Restore by `pg_restore`-ing into a fresh DB and untarring `rag-data` into the
volume before starting `api` / `worker`.

## 5. Upgrades

- **LightRAG schema migrations.** LightRAG's KV/vector storage occasionally
  changes shape between versions. Always test an upgrade against a copy of the
  `rag-data` volume first; see [HKUDS/LightRAG#2255](https://github.com/HKUDS/LightRAG/issues/2255) for the canonical migration thread
- **Postgres major-version upgrades.** AGE links against a specific major
  version. `pg_upgrade` won't carry an AGE database forward; the safe path is
  `pg_dump` from the old version, build a new pg image targeting the new major,
  `pg_restore` into it, then `CREATE EXTENSION age` + `LOAD 'age'` again
- **Alembic.** `docker compose ... exec api uv run alembic upgrade head` after
  each deploy that bumps revisions

## 6. Troubleshooting

| Symptom                                       | Likely cause / fix                                                                                  |
|-----------------------------------------------|-----------------------------------------------------------------------------------------------------|
| `ERROR: extension "age" is not available`     | You're using a stock Postgres image. Use `apps/server/docker/poc-pg.Dockerfile` or the apache/age image |
| `ValueError: JWT_SECRET_KEY too short`        | Must be >=64 chars. Regenerate with `openssl rand -hex 48`                                          |
| Chat SSE hangs / never streams                | A reverse proxy is buffering. Disable buffering for `/api/chat/*`; pass through `Content-Type: text/event-stream` and add `X-Accel-Buffering: no` for nginx; check CORS allows the web origin |
| `connection refused` to pg from api           | `pg` container not healthy yet. `docker compose ... logs pg`. The api waits on the healthcheck, so this means PG is failing to start (volume permissions / corrupted data dir) |
| Upload fails at ~1 GB                         | `MAX_UPLOAD_MB` (default 1000). Raise it and any reverse-proxy `client_max_body_size`               |
| Worker exits with `MINERU_CLOUD_API_KEY`      | `PARSER_MODE=mineru_cloud` requires the key. Set it or switch to `mineru_local` (heavier image)     |

`docker compose -f docker-compose.prod.yml logs -f api worker` is your friend.

## 7. Manual / followup checklist

Open items deferred from earlier tasks (rate limiting, quota enforcement,
backup scripts, etc.) live in [`followups.md`](followups.md). Review before
each release cut.
