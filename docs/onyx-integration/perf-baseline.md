# /v1/onyx/* perf baseline

Run: 2026-05-04T12:33:13.081578+00:00

Stack: local uvicorn (http://127.0.0.1:8801) + dev pg (5532) + dev redis (6479)

Calls per endpoint: 5

| Endpoint | Statuses | p50 (ms) | p95 (ms) |
|---|---|---|---|
| `GET /healthz` | [200, 200, 200, 200, 200] | 0.8 | 2.0 |
| `POST /v1/onyx/kb` | [201, 201, 201, 201, 201] | 5.9 | 12.2 |
| `GET /v1/onyx/kb` | [200, 200, 200, 200, 200] | 4.9 | 10.4 |
| `GET /v1/onyx/kb/{kb_id}` | [200, 200, 200, 200, 200] | 1.8 | 2.1 |
| `GET /v1/onyx/jobs/{uuid}` | [404, 404, 404, 404, 404] | 1.4 | 3.3 |
| `GET /v1/onyx/documents` | [200, 200, 200, 200, 200] | 1.4 | 3.1 |
| `GET /v1/onyx/kg/stats` | [200, 200, 200, 200, 200] | 2.0 | 5.7 |
| `GET /v1/onyx/kg/entities` | [500, 500, 500, 500, 500] | 3.0 | 4.5 |
| `POST /v1/onyx/query/sync` | [500, 500, 500, 500, 500] | 3.5 | 4.7 |
| `DELETE /v1/onyx/kb/{kb_id}` | [204, 404, 404, 404, 404] | 1.5 | 21.3 |

Notes:

- `POST /v1/onyx/query/sync` returns 502 if the LLM endpoint is
  unreachable from this host; that's expected and not a regression.
- In this run both `GET /v1/onyx/kg/entities` and `POST /v1/onyx/query/sync`
  surfaced as 500 because `RAGAnything.__init__()` rejected an
  `parser` kwarg from a local edit on the working tree
  (`raganything/parser.py`). The auth + KB-existence + DB layers all
  passed; the failure is in the RAGAnything factory and not in the
  `/v1/onyx/*` surface itself. This will go away once the container is
  rebuilt against the matching `raganything` package version.
- The five DELETEs reuse a single setup-kb id, so calls 2..5 return 404
  after the first 204 — still useful for measuring auth + lookup cost.
- All non-RAG-factory endpoints (KB CRUD, list, jobs lookup, documents
  list, kg/stats, healthz) respond < 22ms p95 against the dev stack.
