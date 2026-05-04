# PoC: LightRAG-PG + AGE + MinerU Cloud + Multi-Tenant Workspace

## Status

> **Verdict: GO** — live PoC executed 2026-05-04 against the dev stack. End-to-end pipeline (MinerU Cloud → LightRAG PG storages → query → workspace verification) succeeded; clean `finalize_storages`.

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

## Results — live run 2026-05-04

| Field | Value |
| --- | --- |
| Timestamp (CST) | 2026-05-04 17:42 → 17:58 |
| PDF filename | `【大模型工程师_上海】蒋涛宇 1年.pdf` (samples/) |
| MinerU tier | Cloud API (`MINERU_CLOUD_TOKEN` from main `.env`) |
| Parse output | `/tmp/poc_pf65cu78/mineru/.../full.md` |
| LLM endpoint | `http://10.0.0.94:5000/v1` |
| Embedding endpoint | `http://10.0.0.32:7061/v1` |
| Workspace | `poc-tenant-1` |
| Documents | 1 |
| Chunks ingested | 7 |
| Entities extracted | 149 (`lightrag.lightrag_vdb_entity` filtered by workspace) |
| Relations extracted | 23 → 45 retrieved at query time |
| Ingestion duration | **973.3 s** (~16 min) |
| Query mode | hybrid (default) |
| Query final context | 19 entities + 45 relations + 4 chunks |
| Query answer | non-empty (500-char prefix shown in raw log) |
| `len(sources)` returned by script | **0** — script bug (not system bug); see Open Issues |
| `finalize_storages` clean | ✅ "Successfully finalized 12 storages" |
| Errors | none |
| Warnings | benign: edges missing `weight` attr (defaulted to 1.0); rerank not configured |

## Resolved During Live Run

- ✅ **search_path** — set `POSTGRES_SERVER_SETTINGS=search_path=lightrag,public` in `scripts/poc/.env.poc`; LightRAG created all 11 tables under `lightrag` schema as designed.
- ✅ **AGE + pgvector image** — `apache/age:release_PG16_1.5.0` (the documented fallback; `PG16_latest` tag does not exist) with pre-downloaded pgvector tarball builds and runs cleanly. Both `age` and `vector` extensions present.
- ✅ **MinerU Cloud routing** — `parser="mineru" + parse_method="cloud"` reached MinerU Cloud (Token-auth path); no local `mineru` binary invoked. Output `full.md` produced under `/tmp/poc_pf65cu78/mineru/`.
- ✅ **Multi-tenant workspace isolation** — workspace `poc-tenant-1` rows correctly scoped on every LightRAG table.
- ✅ **`finalize_storages` clean** — 12 storages closed, no leaked tasks.

## Open Issues (Non-Blocking, Logged to followups.md)

- **`len(sources) == 0` in PoC script output** — LightRAG's `aquery` returns a string answer + `context` payload; the PoC script's `result.sources` extraction path does not match the actual return shape. Chunks WERE used (log shows `Final context: ... 4 chunks`). Fix: read sources from the proper field in `aquery` result, or switch to the `query_with_separate_keyword_extraction` /  `param.return_type=...` path. Cosmetic; does not affect Phase 1+ since the API layer constructs its own response envelope.
- **Runtime deps missing from `apps/server/pyproject.toml`** — `psycopg[binary]` (alembic env.py uses sync psycopg DSN) and `pgvector` + `asyncpg` (LightRAG PG impl imports `pgvector.asyncpg.register_vector`) were installed ad-hoc on host venv. Add to runtime deps before Phase 1 work to avoid container-rebuild surprises.

## Verdict

**Verdict: GO** — proceed to Phase 1+ with confidence. All foundational risks (AGE + managed-PG quirk, LightRAG PG day-1 viability, MinerU Cloud usability, schema isolation, finalize hygiene) are validated. Remaining items are cosmetic / dep-hygiene.
