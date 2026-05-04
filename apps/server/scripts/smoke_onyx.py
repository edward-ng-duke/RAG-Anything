"""Smoke ping the /v1/onyx/* surface against a running local uvicorn.

Usage:
    cd /home/edward/research/RAG-Anything/apps/server
    set -a; . ../../.env; set +a
    uv run uvicorn rag_service.api.app:create_app --factory \\
        --host 127.0.0.1 --port 8801 &
    sleep 3
    uv run python scripts/smoke_onyx.py --base-url http://127.0.0.1:8801

Hits each onyx endpoint 5 times to compute p50/p95 latency, then writes
the results to ``docs/onyx-integration/perf-baseline.md`` (path
configurable via ``--out``). Designed for re-use against the dev stack;
expects PG + Redis to be reachable from the uvicorn process.

Endpoints exercised:

  - ``GET  /healthz``                          (no auth, sanity baseline)
  - ``POST /v1/onyx/kb``                       (create KB)
  - ``GET  /v1/onyx/kb``                       (list KBs)
  - ``GET  /v1/onyx/kb/{kb_id}``               (get KB)
  - ``GET  /v1/onyx/jobs/{some-uuid}``         (404 expected, just timing)
  - ``GET  /v1/onyx/documents``                (empty list)
  - ``GET  /v1/onyx/kg/stats``                 (empty stats)
  - ``GET  /v1/onyx/kg/entities``              (empty entities)
  - ``POST /v1/onyx/query/sync``               (502 expected if LLM is unreachable)
  - ``DELETE /v1/onyx/kb/{kb_id}``             (cleanup)
"""

from __future__ import annotations

import argparse
import os
import statistics
import time
import uuid
from datetime import datetime, timezone

import httpx


def time_call(fn, n: int = 5) -> tuple[list[int], list[float]]:
    """Call ``fn`` ``n`` times. Return ``(statuses, latencies_ms)``.

    Per-call exceptions become ``status=-1`` so the table can still
    render — we don't want one upstream blip to abort the whole sweep.
    """
    statuses: list[int] = []
    latencies: list[float] = []
    for _ in range(n):
        t0 = time.perf_counter()
        try:
            s = fn()
        except Exception:  # noqa: BLE001 — record + continue
            s = -1
        latencies.append((time.perf_counter() - t0) * 1000)
        statuses.append(s)
    return statuses, latencies


def p95(samples: list[float]) -> float:
    """Approximate 95th percentile via 20-quantile cuts (matches sketch)."""
    if not samples:
        return float("nan")
    if len(samples) < 2:
        return samples[0]
    return statistics.quantiles(samples, n=20)[-1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="e.g. http://127.0.0.1:8801")
    parser.add_argument("--token", default=os.environ.get("INTERNAL_TOKEN", ""))
    parser.add_argument(
        "--out",
        default="docs/onyx-integration/perf-baseline.md",
        help="Path to write the markdown report (relative to repo root)",
    )
    parser.add_argument("--n", type=int, default=5, help="calls per endpoint")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")

    # Headers used by every /v1/onyx/* call. Routes that need a KB header
    # patch in their own X-Onyx-KB-Id below.
    headers_no_kb = {
        "Authorization": f"Bearer {args.token}",
        "X-Onyx-User-Id": "u_smoke",
    }

    client = httpx.Client(base_url=base, timeout=30.0)
    results: list[tuple[str, list[int], list[float]]] = []

    # 1. /healthz (no auth, sanity baseline)
    statuses, lats = time_call(lambda: client.get("/healthz").status_code, n=args.n)
    results.append(("GET /healthz", statuses, lats))

    # 2. POST /v1/onyx/kb — create KB. We create one KB upfront (outside
    # the timing loop) to use for downstream KB-scoped calls, then time
    # ``args.n`` extra creates so each is its own KB.
    setup_resp = client.post(
        "/v1/onyx/kb",
        json={"display_name": f"Smoke setup KB {datetime.now().isoformat()}"},
        headers=headers_no_kb,
    )
    setup_kb_id: str | None = None
    if setup_resp.status_code == 201:
        setup_kb_id = setup_resp.json().get("kb_id")

    timed_kb_ids: list[str] = []

    def _create_kb() -> int:
        body = {"display_name": f"Smoke KB {uuid.uuid4().hex[:8]}"}
        r = client.post("/v1/onyx/kb", json=body, headers=headers_no_kb)
        if r.status_code == 201:
            kid = r.json().get("kb_id")
            if isinstance(kid, str):
                timed_kb_ids.append(kid)
        return r.status_code

    statuses, lats = time_call(_create_kb, n=args.n)
    results.append(("POST /v1/onyx/kb", statuses, lats))

    # 3. GET /v1/onyx/kb (list)
    statuses, lats = time_call(
        lambda: client.get("/v1/onyx/kb", headers=headers_no_kb).status_code,
        n=args.n,
    )
    results.append(("GET /v1/onyx/kb", statuses, lats))

    # KB-scoped calls reuse setup_kb_id (or fall back to first timed id).
    kb_id_for_scoped = setup_kb_id or (timed_kb_ids[0] if timed_kb_ids else None)
    headers_with_kb = dict(headers_no_kb)
    if kb_id_for_scoped:
        headers_with_kb["X-Onyx-KB-Id"] = kb_id_for_scoped

    # 4. GET /v1/onyx/kb/{kb_id}
    if kb_id_for_scoped:
        statuses, lats = time_call(
            lambda: client.get(
                f"/v1/onyx/kb/{kb_id_for_scoped}", headers=headers_with_kb
            ).status_code,
            n=args.n,
        )
    else:
        statuses, lats = [-1] * args.n, [0.0] * args.n
    results.append(("GET /v1/onyx/kb/{kb_id}", statuses, lats))

    # 5. GET /v1/onyx/jobs/{some-uuid} — expect 404, we just want timing.
    # We pass the KB header so we go past auth into the jobs handler.
    fake_job_id = str(uuid.uuid4())
    statuses, lats = time_call(
        lambda: client.get(
            f"/v1/onyx/jobs/{fake_job_id}", headers=headers_with_kb
        ).status_code,
        n=args.n,
    )
    results.append(("GET /v1/onyx/jobs/{uuid}", statuses, lats))

    # 6. GET /v1/onyx/documents (empty list expected)
    statuses, lats = time_call(
        lambda: client.get(
            "/v1/onyx/documents", headers=headers_with_kb
        ).status_code,
        n=args.n,
    )
    results.append(("GET /v1/onyx/documents", statuses, lats))

    # 7. GET /v1/onyx/kg/stats
    statuses, lats = time_call(
        lambda: client.get(
            "/v1/onyx/kg/stats", headers=headers_with_kb
        ).status_code,
        n=args.n,
    )
    results.append(("GET /v1/onyx/kg/stats", statuses, lats))

    # 8. GET /v1/onyx/kg/entities
    statuses, lats = time_call(
        lambda: client.get(
            "/v1/onyx/kg/entities", headers=headers_with_kb
        ).status_code,
        n=args.n,
    )
    results.append(("GET /v1/onyx/kg/entities", statuses, lats))

    # 9. POST /v1/onyx/query/sync — likely 502 if LLM is unreachable. We
    # record whatever we get + the latency; this is by-design since the
    # dev stack's LLM endpoint is on a private subnet.
    query_body = {
        "question": "ping",
        "history": [],
        "mode": "naive",
        "top_k": 1,
        "include_sources": False,
        "max_history_turns": 0,
    }
    statuses, lats = time_call(
        lambda: client.post(
            "/v1/onyx/query/sync", json=query_body, headers=headers_with_kb
        ).status_code,
        n=args.n,
    )
    results.append(("POST /v1/onyx/query/sync", statuses, lats))

    # 10. DELETE /v1/onyx/kb/{kb_id} — clean up everything we created.
    # First time the setup_kb_id (5x). Each delete after the first will
    # 404 since the KB is gone; that's still valid latency data for the
    # delete path's auth + lookup.
    delete_target = setup_kb_id or (timed_kb_ids[0] if timed_kb_ids else None)
    if delete_target:
        del_headers = dict(headers_no_kb)
        del_headers["X-Onyx-KB-Id"] = delete_target

        def _delete() -> int:
            return client.delete(
                f"/v1/onyx/kb/{delete_target}", headers=del_headers
            ).status_code

        statuses, lats = time_call(_delete, n=args.n)
    else:
        statuses, lats = [-1] * args.n, [0.0] * args.n
    results.append(("DELETE /v1/onyx/kb/{kb_id}", statuses, lats))

    # Mop up any extra KBs we left behind by the timed creates.
    for kid in timed_kb_ids:
        try:
            client.delete(
                f"/v1/onyx/kb/{kid}",
                headers={**headers_no_kb, "X-Onyx-KB-Id": kid},
            )
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass

    client.close()

    # ---------------------------------------------------------------
    # Write the report
    # ---------------------------------------------------------------
    out = args.out
    out_dir = os.path.dirname(out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    with open(out, "w") as f:
        f.write("# /v1/onyx/* perf baseline\n\n")
        f.write(f"Run: {datetime.now(timezone.utc).isoformat()}\n\n")
        f.write(
            f"Stack: local uvicorn ({base}) + dev pg (5432) + dev redis (6379)\n\n"
        )
        f.write(f"Calls per endpoint: {args.n}\n\n")
        f.write("| Endpoint | Statuses | p50 (ms) | p95 (ms) |\n")
        f.write("|---|---|---|---|\n")
        for endpoint, statuses, lats in results:
            p50 = statistics.median(lats) if lats else float("nan")
            p95v = p95(lats)
            f.write(
                f"| `{endpoint}` | {statuses} | {p50:.1f} | {p95v:.1f} |\n"
            )
        f.write("\n")
        f.write(
            "Notes: `POST /v1/onyx/query/sync` returns 502 if the LLM endpoint is "
            "unreachable from this host; that's expected and not a regression. "
            "The five DELETEs reuse a single setup-kb id, so calls 2..5 return 404 "
            "after the first 204 — still useful for measuring auth + lookup cost.\n"
        )
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
