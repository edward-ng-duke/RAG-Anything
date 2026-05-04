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


metrics_router = APIRouter(tags=["metrics"])

@metrics_router.get("/metrics")
def metrics():
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
