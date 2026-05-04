"""E2E suite bootstrap.

These tests exercise the FastAPI router stack end-to-end with an in-memory
SQLite-backed DB, fakeredis for the Redis surface, and a mocked
``RAGAnything`` so they can run in any CI environment without a live
Postgres / Redis / LLM.

The env-var ``setdefault`` calls below MUST happen before any
``rag_service`` import — :class:`rag_service.config.Settings` is constructed
lazily on first attribute access and would otherwise trip on missing
required vars when other test modules force a settings build first.
"""

from __future__ import annotations

import os

# Bootstrap settings env BEFORE any rag_service import. Use sqlite+aiosqlite
# so the engine in :mod:`rag_service.db.session` could in principle be
# constructed against this URL — but the actual E2E test below always
# overrides the FastAPI dependency to talk to its own engine, so this URL
# only has to satisfy ``Settings`` validation.
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault("INTERNAL_TOKEN", "e2e-secret")
os.environ.setdefault("LLM_BASE_URL", "http://test-llm/v1")
os.environ.setdefault("LLM_API_KEY", "x")
os.environ.setdefault("LLM_MODEL", "test-model")
os.environ.setdefault("EMBEDDING_BASE_URL", "http://test-emb/v1")
os.environ.setdefault("EMBEDDING_API_KEY", "x")
os.environ.setdefault("EMBEDDING_MODEL", "test-embed")
