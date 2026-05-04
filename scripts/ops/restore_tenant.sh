#!/usr/bin/env bash
# restore_tenant.sh — restore rows belonging to a single tenant from a full
# pg_dump produced by ./scripts/ops/backup.sh.
#
# Approach:
#   1. pg_restore the dump into a throwaway temporary database inside the
#      pg container.
#   2. INSERT ... SELECT only the rows whose tenant_id (or workspace, for
#      LightRAG-managed tables) matches the requested tenant, with ON
#      CONFLICT DO NOTHING so the script can be re-run.
#   3. Drop the temporary DB.
#   4. Optionally mirror the tenant's upload subtree from a data-dir
#      snapshot back into the live rag-data volume.
#
# Tables restored from public schema:
#   tenants, documents, jobs, query_log, conversations, messages,
#   memberships
#
# LightRAG tables (under the lightrag schema) filtered by workspace=$TENANT.
# The exact LightRAG table set depends on the active storage backend; we
# restore every table in the lightrag schema and rely on the workspace
# column being present (PG-backed LightRAG storages all carry it). If a
# given table doesn't have a workspace column, the filter is skipped and
# *all* rows are restored — this is approximate, see GAP below.
#
# GAP: this is approximate. Rows in tables without a tenant/workspace
# column (e.g. shared lookup tables, if any) are restored wholesale.
# Rows that reference deleted tenants via FK may also be re-introduced.
# Manual review of the temp DB before merge is the safe path for an
# audit-grade restore.
#
# Usage:
#   ./scripts/ops/restore_tenant.sh --tenant <tenant_id> \
#                                   --from BACKUP_DIR/pg-{ts}.dump \
#                                   [--data BACKUP_DIR/data-{ts}] [--yes]

set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
PG_SERVICE="${PG_SERVICE:-pg}"
API_SERVICE="${API_SERVICE:-api}"
POSTGRES_USER="${POSTGRES_USER:-rag}"
POSTGRES_DB="${POSTGRES_DB:-rag}"

tenant=""
dump=""
data_src=""
auto_yes="0"

usage() {
    cat <<EOF
Usage: $0 --tenant <tenant_id> --from <dump> [--data <data-dir>] [--yes]

  --tenant <id>   tenant_id whose rows should be restored
  --from <path>   pg_dump custom-format file produced by backup.sh
  --data <path>   optional data-dir snapshot; uploads/<tenant>/ is mirrored
  --yes           skip confirmation prompt
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --tenant)  tenant="${2:-}";   shift 2 ;;
        --from)    dump="${2:-}";     shift 2 ;;
        --data)    data_src="${2:-}"; shift 2 ;;
        --yes|-y)  auto_yes="1";      shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown arg: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [ -z "${tenant}" ] || [ -z "${dump}" ]; then
    echo "error: --tenant and --from are required" >&2
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
# Cheap whitelist: avoid SQL injection on the tenant arg by restricting it
# to identifier-friendly characters. Matches the API's validate_tenant_id.
case "${tenant}" in
    *[!a-zA-Z0-9_-]*)
        echo "error: tenant id contains forbidden chars" >&2
        exit 2
        ;;
esac

if [ "${auto_yes}" != "1" ]; then
    echo "Will restore rows for tenant '${tenant}' from: ${dump}"
    if [ -n "${data_src}" ]; then
        echo "Will mirror uploads/${tenant}/ from:        ${data_src}/uploads/${tenant}/"
    fi
    echo "Continue? [y/N]"
    read -r answer
    case "${answer}" in
        y|Y|yes|YES) ;;
        *) echo "aborted."; exit 1 ;;
    esac
fi

compose=(docker compose -f "${COMPOSE_FILE}")
tmp_db="rag_restore_$(date -u +%Y%m%d%H%M%S)_$$"

# Public-schema tables we know how to filter on tenant_id. Keep this list
# in sync with rag_service.db.models. Order matters for FK satisfaction.
public_tables_tenant=(tenants memberships documents jobs query_log conversations)
# `messages` is filtered transitively via conversation_id.
# (memberships ON tenant_id = $TENANT brings users in via FK, but users
# themselves are NOT filtered — a user may belong to multiple tenants.)

cleanup() {
    "${compose[@]}" exec -T "${PG_SERVICE}" \
        psql -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" \
            -c "DROP DATABASE IF EXISTS \"${tmp_db}\";" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "[restore-tenant] creating temp database ${tmp_db}..."
"${compose[@]}" exec -T "${PG_SERVICE}" \
    psql -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" \
        -c "CREATE DATABASE \"${tmp_db}\";"

echo "[restore-tenant] loading dump into ${tmp_db}..."
"${compose[@]}" exec -T "${PG_SERVICE}" \
    pg_restore --no-owner --no-acl \
        -U "${POSTGRES_USER}" -d "${tmp_db}" \
    < "${dump}"

echo "[restore-tenant] copying tenant rows from ${tmp_db} -> ${POSTGRES_DB}..."

# Build one SQL script and pipe it in to avoid per-table round-trips.
sql=""
for tbl in "${public_tables_tenant[@]}"; do
    sql="${sql}
INSERT INTO public.${tbl}
SELECT * FROM dblink('dbname=${tmp_db}',
    'SELECT * FROM public.${tbl} WHERE tenant_id = ''${tenant}''')
AS t(LIKE public.${tbl})
ON CONFLICT DO NOTHING;
"
done

# messages: filtered via conversation_id ∈ tenant's conversations.
sql="${sql}
INSERT INTO public.messages
SELECT m.* FROM dblink('dbname=${tmp_db}',
    'SELECT m.* FROM public.messages m
     JOIN public.conversations c USING (conversation_id)
     WHERE c.tenant_id = ''${tenant}''')
AS m(LIKE public.messages)
ON CONFLICT DO NOTHING;
"

# LightRAG tables: every table in the lightrag schema that has a workspace
# column. We dynamically generate the inserts via information_schema in
# the live DB (the schema must already exist on the live side).
sql="${sql}
DO \$do\$
DECLARE
    r record;
    cmd text;
BEGIN
    FOR r IN
        SELECT table_name
        FROM information_schema.columns
        WHERE table_schema = 'lightrag' AND column_name = 'workspace'
    LOOP
        cmd := format(
            'INSERT INTO lightrag.%I SELECT * FROM dblink(%L, %L) AS t(LIKE lightrag.%I) ON CONFLICT DO NOTHING',
            r.table_name,
            'dbname=${tmp_db}',
            format('SELECT * FROM lightrag.%I WHERE workspace = %L', r.table_name, '${tenant}'),
            r.table_name
        );
        EXECUTE cmd;
    END LOOP;
END
\$do\$;
"

echo "${sql}" | "${compose[@]}" exec -T "${PG_SERVICE}" \
    psql -v ON_ERROR_STOP=1 \
        -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" \
        -c "CREATE EXTENSION IF NOT EXISTS dblink;" \
        -f -

if [ -n "${data_src}" ]; then
    src_uploads="${data_src}/uploads/${tenant}"
    if [ -d "${src_uploads}" ]; then
        echo "[restore-tenant] mirroring uploads/${tenant}/ into rag-data volume..."
        tar -C "${src_uploads}" -cf - . \
            | "${compose[@]}" exec -T "${API_SERVICE}" \
                sh -c "mkdir -p /data/uploads/${tenant} && tar -C /data/uploads/${tenant} -xf -"
    else
        echo "[restore-tenant] warning: ${src_uploads} not found in snapshot; skipping data mirror"
    fi
fi

echo "[restore-tenant] done."
