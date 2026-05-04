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
COPY README.md ./
COPY raganything ./raganything
# LightRAG is a regular pip dep (lightrag-hku from PyPI); not a path source, so no copy needed.
COPY apps/server/pyproject.toml apps/server/pyproject.toml
COPY apps/server/README.md apps/server/README.md

# uv lockfile, if present (best-effort; the build still works without it via --frozen fallback).
COPY apps/server/uv.loc[k] apps/server/uv.lock

# Copy app source BEFORE uv sync. The hatchling build backend (declared in
# apps/server/pyproject.toml) needs src/rag_service/ to exist when uv resolves
# and installs the rag-service package itself, otherwise only deps are
# installed and the rag_service module is missing at runtime.
COPY apps/server/src apps/server/src
COPY apps/server/alembic apps/server/alembic
COPY apps/server/alembic.ini apps/server/alembic.ini

WORKDIR /app/apps/server
RUN uv sync --no-dev --frozen 2>/dev/null || uv sync --no-dev


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
