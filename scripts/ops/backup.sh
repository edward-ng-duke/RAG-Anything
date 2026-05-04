#!/usr/bin/env bash
# backup.sh — snapshot the rag-service Postgres DB and on-disk uploads volume.
#
# Produces three artifacts under ${BACKUP_DIR:-./backups}/:
#   - pg-{ts}.dump          : full DB (custom format) covering public + lightrag
#                             schemas, suitable for `pg_restore --clean`.
#   - pg-public-{ts}.dump   : just the public schema; lets ops fast-restore
#                             business state without dragging the LightRAG
#                             tables along.
#   - data-{ts}/            : rsync mirror of the rag-data docker volume.
#
# Environment:
#   BACKUP_DIR           where to write artifacts (default: ./backups)
#   COMPOSE_FILE         compose file to talk to (default: docker-compose.prod.yml)
#   COMPOSE_PROJECT_NAME compose project name (default: docker compose's auto)
#   PG_SERVICE           pg service name (default: pg)
#   API_SERVICE          api service name (default: api), used to locate the
#                        rag-data volume mount point inside the running container.
#   POSTGRES_USER        postgres user (default: rag)
#   POSTGRES_DB          postgres db (default: rag)
#
# Usage:
#   ./scripts/ops/backup.sh

set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-./backups}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
PG_SERVICE="${PG_SERVICE:-pg}"
API_SERVICE="${API_SERVICE:-api}"
POSTGRES_USER="${POSTGRES_USER:-rag}"
POSTGRES_DB="${POSTGRES_DB:-rag}"

ts="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "${BACKUP_DIR}"

pg_full="${BACKUP_DIR}/pg-${ts}.dump"
pg_public="${BACKUP_DIR}/pg-public-${ts}.dump"
data_dir="${BACKUP_DIR}/data-${ts}"

compose=(docker compose -f "${COMPOSE_FILE}")

echo "[backup] writing full DB dump -> ${pg_full}"
"${compose[@]}" exec -T "${PG_SERVICE}" \
    pg_dump --format=custom --no-owner --no-acl \
        -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" \
    > "${pg_full}"

echo "[backup] writing public-schema dump -> ${pg_public}"
"${compose[@]}" exec -T "${PG_SERVICE}" \
    pg_dump --format=custom --no-owner --no-acl --schema=public \
        -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" \
    > "${pg_public}"

# Rsync the rag-data volume contents. We piggy-back on the API container —
# it already mounts the volume at /data — and stream a tar through stdout
# into a local extract. This is rsync-equivalent for our purposes (we
# rebuild the tree from scratch each run) without requiring rsync inside
# the container.
echo "[backup] copying rag-data volume -> ${data_dir}/"
mkdir -p "${data_dir}"
"${compose[@]}" exec -T "${API_SERVICE}" \
    tar -C /data -cf - . \
    | tar -C "${data_dir}" -xf -

echo "[backup] done."
echo "  full DB     : ${pg_full}"
echo "  public-only : ${pg_public}"
echo "  data dir    : ${data_dir}/"
