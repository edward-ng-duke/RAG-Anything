"""Conversation persistence layer for the rag_service control plane.

Provides CRUD scoped to ``(tenant_id, user_id)`` for conversations and
their messages. The orchestrator and HTTP routes layered above this
module rely on the repository functions defined in :mod:`.repository`.
"""
