"""Read-only access layer for LightRAG's knowledge-graph storage.

LightRAG materialises a tenant's KG across a handful of Postgres tables in
the ``lightrag`` schema (``LIGHTRAG_VDB_ENTITY``, ``LIGHTRAG_VDB_RELATION``,
``LIGHTRAG_DOC_CHUNKS``). These tables are not part of our ORM, so this
package wraps them with parameterised ``text()`` queries that always filter
by ``workspace = tenant_id`` for tenant isolation.

Submodules
----------
``repository``
    Async query helpers for flat lookups: list/get entities, list relations,
    get a chunk, and per-tenant counts. Used by the ``/v1/kg/*`` HTTP
    routes.
"""
