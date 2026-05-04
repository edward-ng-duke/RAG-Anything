#!/usr/bin/env bash
# restore.sh — restore a Postgres dump (and optionally a data-volume snapshot)
# produced by ./scripts/ops/backup.sh.
#
# This is destructive: pg_restore --clean drops every object that exists in
# the dump before re-creating it. Pass --yes to skip the confirm prompt.
#
# Usage:
#   ./scripts/ops/restore.sh --from BACKUP_DIR/pg-{ts}.dump \
#                            [--data BACKUP_DIR/data-{ts}] [--yes]
#
# Environment (mirrors backup.sh):
#   COMPOSE_FILE, PG_SERVICE, API_SERVICE, POSTGRES_USER, POSTGRES_DB

set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
PG_SERVICE="${PG_SERVICE:-pg}"
API_SERVICE="${API_SERVICE:-api}"
POSTGRES_USER="${POSTGRES_USER:-rag}"
POSTGRES_DB="${POSTGRES_DB:-rag}"

dump=""
data_src=""
auto_yes="0"

usage() {
    cat <<EOF
Usage: $0 --from <dump> [--data <data-dir>] [--yes]

  --from <path>   Path to a pg_dump custom-format file (e.g. backups/pg-*.dump)
  --data <path>   Optional data-volume snapshot directory to mirror back into
                  the rag-data volume.
  --yes           Skip the destructive-restore confirmation prompt.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --from)
            dump="${2:-}"
            shift 2
            ;;
        --data)
            data_src="${2:-}"
            shift 2
            ;;
        --yes|-y)
            auto_yes="1"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "unknown arg: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [ -z "${dump}" ]; then
    echo "error: --from is required" >&2
    usage >&2
    exit 2
fi
if [ ! -f "${dump}" ]; then
    echo "error: dump file not found: ${dump}" >&2
    exit 2
fi
if [ -n "${data_src}" ] && [ ! -d "${data_src}" ]; then
    echo "error: --data dir not found: ${data_src}" >&2
    exit 2
fi

if [ "${auto_yes}" != "1" ]; then
    echo "About to restore Postgres from: ${dump}"
    if [ -n "${data_src}" ]; then
        echo "And mirror data dir from:       ${data_src}/"
    fi
    echo "This will DROP and RECREATE existing objects. Continue? [y/N]"
    read -r answer
    case "${answer}" in
        y|Y|yes|YES) ;;
        *) echo "aborted."; exit 1 ;;
    esac
fi

compose=(docker compose -f "${COMPOSE_FILE}")

echo "[restore] piping dump into pg_restore..."
"${compose[@]}" exec -T "${PG_SERVICE}" \
    pg_restore --clean --if-exists --no-owner --no-acl \
        -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" \
    < "${dump}"

if [ -n "${data_src}" ]; then
    echo "[restore] streaming data dir into the rag-data volume..."
    tar -C "${data_src}" -cf - . \
        | "${compose[@]}" exec -T "${API_SERVICE}" tar -C /data -xf -
fi

echo "[restore] done."
