"""Cross-router service-layer helpers.

Modules under :mod:`rag_service.services` host shared logic invoked by
multiple routers — e.g. the multipart upload / dedup / enqueue pipeline
that both ``/v1/ingest`` (alpha) and ``/v1/onyx/documents`` (ONYX
integration) need to drive.

Routers stay thin (request parsing + response shaping); the service
layer owns the workflow.
"""
