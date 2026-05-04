# rag-service (apps/server)

FastAPI + arq backend for the RAG-Anything alpha-product. Wraps `raganything` /
LightRAG into a multi-tenant HTTP API: auth, document upload + parsing,
chunk/entity storage in PostgreSQL (pgvector + Apache AGE), conversational
chat with SSE streaming, and a knowledge-graph view.

## Layout

```
src/rag_service/
  api/            FastAPI routers (auth, documents, chat, kg, health, admin)
  auth/           Password hashing, JWT issuance, dependency-injection guards
  conversations/  Chat sessions + message history
  core/
    paths.py        Per-tenant working-directory resolution (DATA_DIR/{tenant})
    rag_factory.py  LRU-cached LightRAG instances keyed by (tenant, kb)
    llm_provider.py OpenAI-compatible LLM/embedding client wiring
    reload_listener.py  Cache invalidation via Redis pub/sub
  db/             SQLAlchemy async models + session factory
  kg/             Knowledge-graph read API (entities, relationships, neighborhood)
  observability/  Structured logging + Prometheus metrics
  parsers/        MinerU local + cloud parser adapters
  worker/         arq task definitions (document parsing + indexing)
  cli.py          Entrypoints: rag-api, rag-worker
  config.py       pydantic-settings; reads env vars
```

Migrations live in `alembic/versions/`.

## Dev quick-start

```bash
cd apps/server

# Install deps (uv: https://github.com/astral-sh/uv)
uv sync

# Bring up Postgres (with AGE+pgvector) and Redis from the repo root
cd ../..
docker compose -f docker-compose.dev.yml up -d pg redis

# Apply migrations
cd apps/server
uv run alembic upgrade head

# Run the API in one terminal
uv run rag-api

# Run the worker in another
uv run rag-worker
```

API listens on `:8000`. Health: `GET /healthz`. OpenAPI: `GET /docs`.

## Tests

```bash
uv run pytest                      # full suite (uses testcontainers for PG/Redis)
uv run pytest tests/unit -q        # fast unit-only
uv run pytest -k chat              # filter by name
```

## Key env vars

| Var                             | Notes                                                       |
|---------------------------------|-------------------------------------------------------------|
| `DATABASE_URL`                  | `postgresql+asyncpg://...`; PG must have AGE + pgvector     |
| `REDIS_URL`                     | `redis://...`; used for arq queue and pub/sub invalidation  |
| `JWT_SECRET_KEY`                | >=64 chars; used to sign user JWTs                          |
| `INTERNAL_TOKEN`                | Shared secret between api <-> worker for trusted endpoints  |
| `LLM_*`, `EMBEDDING_*`          | OpenAI-compatible base URL / api key / model                |
| `VLM_MODEL`                     | Optional; enables image-aware ingestion                     |
| `PARSER_MODE`                   | `mineru_cloud` (default) or `mineru_local`                  |
| `MINERU_CLOUD_API_KEY`          | Required when `PARSER_MODE=mineru_cloud`                    |
| `DATA_DIR`                      | Per-tenant working dirs land under here                     |
| `MAX_UPLOAD_MB`                 | Per-file upload cap                                         |
| `LRU_INSTANCE_CAP`              | How many LightRAG instances stay hot in memory              |

See `src/rag_service/config.py` for the full schema.

## Where to look

- Tenant working-dir resolution: `core/paths.py`
- LightRAG instance lifecycle / cache: `core/rag_factory.py`
- Chat SSE streaming + persistence: `api/chat.py`, `conversations/`
- Worker tasks (parse + index): `worker/tasks.py`
- DB models: `db/models.py`
