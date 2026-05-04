# PoC PostgreSQL image: Apache AGE (PG16) + pgvector + lightrag bootstrap.
#
# Base: apache/age PG16 image. Tag preference is PG16_latest; if unavailable
# at build time, swap to apache/age:release_PG16_1.5.0 (last known-good tag).
FROM apache/age:release_PG16_1.5.0

# pgvector install strategy: build from source.
# Rationale: the apache/age image is Debian-based but its apt repos do not
# reliably ship `postgresql-16-pgvector` (the PGDG repo isn't enabled by
# default in this image, and adding it just to grab one package is heavier
# than a quick source build). Source build is small, deterministic, and
# pinned to a known-good pgvector tag.
ARG PGVECTOR_REF=v0.7.4
USER root

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        git \
        postgresql-server-dev-16; \
    git clone --depth 1 --branch "${PGVECTOR_REF}" https://github.com/pgvector/pgvector.git /tmp/pgvector; \
    cd /tmp/pgvector; \
    make OPTFLAGS=""; \
    make install; \
    cd /; \
    rm -rf /tmp/pgvector; \
    apt-get purge -y --auto-remove build-essential git postgresql-server-dev-16; \
    rm -rf /var/lib/apt/lists/*

# Bootstrap extensions + lightrag schema on first PG init.
COPY init-poc.sql /docker-entrypoint-initdb.d/01-init-poc.sql
