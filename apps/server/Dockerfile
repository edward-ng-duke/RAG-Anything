# syntax=docker/dockerfile:1.7

FROM python:3.11-slim AS builder
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 UV_LINK_MODE=copy

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libpq-dev curl && \
    rm -rf /var/lib/apt/lists/*

# Install uv (Astral)
RUN pip install --no-cache-dir uv

WORKDIR /app

# Copy what uv needs to resolve the dependency tree.
# raganything is declared as a path-editable source from apps/server/pyproject.toml
# (path = "../.."), so it must live at /app/raganything when WORKDIR=/app/apps/server.
COPY pyproject.toml ./
COPY raganything ./raganything
# LightRAG is a regular pip dep (lightrag-hku from PyPI); not a path source, so no copy needed.
COPY apps/server/pyproject.toml apps/server/pyproject.toml

# uv lockfile, if present (best-effort; the build still works without it via --frozen fallback).
COPY apps/server/uv.loc[k] apps/server/uv.lock

WORKDIR /app/apps/server
RUN uv sync --no-dev --frozen 2>/dev/null || uv sync --no-dev

# Copy app source last so code-only edits don't bust the dependency cache.
COPY apps/server/src ./src
COPY apps/server/alembic ./alembic
COPY apps/server/alembic.ini ./alembic.ini


FROM python:3.11-slim AS runner
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/apps/server/.venv/bin:$PATH"

RUN apt-get update && apt-get install -y --no-install-recommends libpq5 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app/apps/server

# Copy the entire prepared tree (venv + raganything path-source + app source).
COPY --from=builder /app /app

EXPOSE 8000

ENTRYPOINT []
CMD ["rag-api"]
