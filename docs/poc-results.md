# PoC: LightRAG-PG + AGE + MinerU Cloud + Multi-Tenant Workspace

## Status

> **Verdict: PENDING** — script readiness verified (see Task 0.4 commit), live execution awaits operator with credentials.

The PoC harness (`scripts/poc/poc_ingest_query.py`, docker-compose stack, `.env.poc.example`) has been authored and statically reviewed. No live ingest/query has been executed yet because that requires:

- Real LLM API credentials (provider + key + model)
- Real embedding API credentials (provider + key + model)
- A MinerU Cloud token
- A locally running Postgres-with-AGE-and-pgvector container (per `docker-compose.poc.yml`)

Until an operator runs the steps in [How to Run](#how-to-run) and fills in [Results](#results--to-fill-in-after-live-run), this document remains in the PENDING state.

## What This PoC Validates

- **LightRAG PG backends**: KV, vector, doc-status, and graph storages all initialize against a single Postgres instance using `PGKVStorage`, `PGVectorStorage`, `PGDocStatusStorage`, `PGGraphStorage`.
- **AGE present**: Apache AGE extension loads in the Postgres container and `PGGraphStorage` issues Cypher successfully (graph nodes/edges round-trip).
- **MinerU Cloud parsing**: `process_document_complete` is driven through `raganything.parser` with `parser="mineru"` + `parse_method="cloud"` (or equivalent registry entry) and produces non-empty content_list without falling back to local MinerU.
- **Multi-tenant workspace isolation**: Two distinct `workspace=` values produce disjoint KV/vector/graph rows; a query in workspace A cannot retrieve content ingested into workspace B.
- **`finalize_storages` clean**: Calling `await rag.finalize_storages()` (and the LightRAG equivalent) closes all PG/AGE connection pools without warnings, exceptions, or leaked tasks.

## How to Run

```bash
cd /home/edward/research/RAG-Anything
docker compose -f docker-compose.poc.yml up -d
sleep 10  # wait for PG ready
cp scripts/poc/.env.poc.example scripts/poc/.env.poc
# edit scripts/poc/.env.poc to fill in real credentials
uv run --with-editable apps/server python scripts/poc/poc_ingest_query.py
```

Teardown:

```bash
cd /home/edward/research/RAG-Anything
docker compose -f docker-compose.poc.yml down -v
rm -f scripts/poc/.env.poc
```

(`-v` removes the Postgres data volume so the next run starts from a clean schema; omit `-v` if you want to inspect rows after the run.)

## Results — to fill in after live run

| Field | Value |
| --- | --- |
| Timestamp (UTC) | `<TBD>` |
| PDF filename | `<TBD>` |
| PDF size (MB) | `<TBD>` |
| PDF page count | `<TBD>` |
| MinerU tier | `<TBD>` |
| LLM model | `<TBD>` |
| Embedding model | `<TBD>` |
| Ingestion duration (s) | `<TBD>` |
| Query latency (ms) | `<TBD>` |
| Entity count returned | `<TBD>` |
| Sources count | `<TBD>` |
| Warnings | `<TBD>` |
| Errors | `<TBD>` |
| `finalize_storages` clean (Y/N) | `<TBD>` |

## Open Questions Surfaced By Dry-Run

- **search_path config gap** (see `followups.md` Task 0.4): does running the PoC fail because LightRAG creates its tables in the `public` schema instead of a dedicated `lightrag` schema? If so, we need to either set `search_path=lightrag,public` on the role/database or accept `public` as the storage schema and document it.
- **AGE + pgvector image compatibility** (Task 0.3 followup): does the `apache/age:PG16_latest` base image co-operate with our pgvector source-build layer in `docker-compose.poc.yml`? Watch the container logs for extension load errors during `docker compose up`.
- **MinerU Cloud parser routing**: does the MinerU Cloud parser registration in `raganything.parser` registry actually drive `process_document_complete`, or does the call silently fall back to the default local MinerU code path? Verify by checking that no local `mineru` binary is invoked and that outbound traffic hits the MinerU Cloud endpoint.

## Verdict

**Verdict: PENDING — re-run after operator executes the PoC.**

When PoC succeeds, update the [Status](#status) section and this Verdict to `GO`. If PoC fails, update Verdict to `NO-GO` and document the blocker before any Phase 1 task is dispatched.
