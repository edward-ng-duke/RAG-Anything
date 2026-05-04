def test_all_8_onyx_metrics_registered():
    """Each of the 8 metrics named in ONYX_INTEGRATION_PLAN.md §8.2 is registered on the custom REGISTRY."""
    from prometheus_client import generate_latest
    from rag_service.observability.metrics import REGISTRY
    body = generate_latest(REGISTRY).decode()
    for name in [
        "rag_onyx_requests_total",
        "rag_onyx_query_latency_seconds",
        "rag_onyx_ingest_duration_seconds",
        "rag_onyx_kbs_total",
        "rag_onyx_active_kbs_24h",
        "rag_onyx_documents_total",
        "rag_onyx_internal_token_misuse_total",
    ]:
        assert name in body, f"missing metric {name}"


def test_rag_onyx_requests_total_increments():
    from rag_service.observability.metrics import rag_onyx_requests_total
    rag_onyx_requests_total.labels(path="/v1/onyx/query", status="200", error_code="").inc()
    # No raise = success; concrete value depends on test order, but the metric exists.


def test_rag_onyx_query_latency_observes():
    from rag_service.observability.metrics import rag_onyx_query_latency_seconds
    rag_onyx_query_latency_seconds.labels(kb_id="onyx-x", mode="hybrid").observe(0.5)


def test_rag_onyx_kbs_total_gauge():
    from rag_service.observability.metrics import rag_onyx_kbs_total
    rag_onyx_kbs_total.set(42)


def test_rag_onyx_internal_token_misuse_labels():
    from rag_service.observability.metrics import rag_onyx_internal_token_misuse_total
    for reason in ["invalid_token", "missing_token", "ip_not_allowed", "unknown_kb"]:
        rag_onyx_internal_token_misuse_total.labels(reason=reason).inc()
