from prometheus_client import Counter, Histogram, Gauge, CollectorRegistry, generate_latest, CONTENT_TYPE_LATEST
from fastapi import APIRouter, Response

# Use a custom registry to avoid global pollution
REGISTRY = CollectorRegistry(auto_describe=True)

rag_query_latency_seconds = Histogram(
    "rag_query_latency_seconds", "RAG query latency",
    labelnames=["mode"], registry=REGISTRY,
    buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60),
)

rag_ingest_duration_seconds = Histogram(
    "rag_ingest_duration_seconds", "Document ingest duration",
    registry=REGISTRY,
    buckets=(1, 5, 15, 30, 60, 120, 300, 600),
)

rag_llm_tokens_total = Counter(
    "rag_llm_tokens_total", "Total LLM tokens",
    labelnames=["type"], registry=REGISTRY,  # type: in|out
)

rag_llm_cost_usd_total = Counter(
    "rag_llm_cost_usd_total", "Total LLM cost in USD",
    registry=REGISTRY,
)

rag_active_rag_instances = Gauge(
    "rag_active_rag_instances", "Number of cached RAGAnything instances",
    registry=REGISTRY,
)

rag_queue_depth = Gauge(
    "rag_queue_depth", "arq queue depth",
    registry=REGISTRY,
)

rag_storage_used_mb = Gauge(
    "rag_storage_used_mb", "Storage used per tenant (MB)",
    labelnames=["tenant_id"], registry=REGISTRY,
)

rag_auth_login_total = Counter(
    "rag_auth_login_total", "Auth login attempts",
    labelnames=["result"], registry=REGISTRY,  # result: ok|fail
)

rag_kg_query_latency_seconds = Histogram(
    "rag_kg_query_latency_seconds", "KG endpoint latency",
    labelnames=["endpoint"], registry=REGISTRY,
)

# --- ONYX integration metrics (ONYX_INTEGRATION_PLAN.md §8.2) ---
# The onyx_kg_* endpoints reuse rag_kg_query_latency_seconds via the
# existing 'endpoint' label (e.g. endpoint="onyx_kg_subgraph").

rag_onyx_requests_total = Counter(
    "rag_onyx_requests_total", "ONYX integration HTTP requests",
    labelnames=["path", "status", "error_code"], registry=REGISTRY,
)

rag_onyx_query_latency_seconds = Histogram(
    "rag_onyx_query_latency_seconds", "ONYX query endpoint latency",
    labelnames=["kb_id", "mode"], registry=REGISTRY,
    buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60),
)

rag_onyx_ingest_duration_seconds = Histogram(
    "rag_onyx_ingest_duration_seconds",
    "ONYX ingest end-to-end duration (queue -> done)",
    labelnames=["kb_id"], registry=REGISTRY,
    buckets=(1, 5, 15, 30, 60, 120, 300, 600),
)

rag_onyx_kbs_total = Gauge(
    "rag_onyx_kbs_total",
    "Number of source=onyx tenants on this RAG instance",
    registry=REGISTRY,
)

rag_onyx_active_kbs_24h = Gauge(
    "rag_onyx_active_kbs_24h",
    "Distinct onyx KBs that received a query in the last 24h",
    registry=REGISTRY,
)

rag_onyx_documents_total = Gauge(
    "rag_onyx_documents_total",
    "Documents per onyx KB",
    labelnames=["kb_id"], registry=REGISTRY,
)

rag_onyx_internal_token_misuse_total = Counter(
    "rag_onyx_internal_token_misuse_total",
    "Rejections of /v1/onyx/* requests at the auth layer",
    labelnames=["reason"], registry=REGISTRY,  # reason: invalid_token | missing_token | ip_not_allowed | unknown_kb
)


metrics_router = APIRouter(tags=["metrics"])

@metrics_router.get("/metrics")
def metrics():
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
