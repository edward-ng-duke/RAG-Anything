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
USER root

# pgvector tarball is pre-downloaded on the host (codeload.github.com) and
# COPY'd in, because some build networks can't reach github.com:443 directly.
COPY pgvector.tar.gz /tmp/pgvector.tar.gz

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        postgresql-server-dev-16; \
    mkdir -p /tmp/pgvector; \
    tar -xzf /tmp/pgvector.tar.gz --strip-components=1 -C /tmp/pgvector; \
    cd /tmp/pgvector; \
    make OPTFLAGS=""; \
    make install; \
    cd /; \
    rm -rf /tmp/pgvector /tmp/pgvector.tar.gz; \
    apt-get purge -y --auto-remove build-essential postgresql-server-dev-16; \
    rm -rf /var/lib/apt/lists/*

# Bootstrap extensions + lightrag schema on first PG init.
COPY init-poc.sql /docker-entrypoint-initdb.d/01-init-poc.sql
