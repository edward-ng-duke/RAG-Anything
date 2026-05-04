"""Shared test fixtures + env bootstrap.

Sets all required env vars BEFORE any rag_service import in any test module.
Test files may still set their own values via os.environ.setdefault() — those
will win because setdefault is a no-op when keys already exist.
"""
import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x@x/x")
os.environ.setdefault("REDIS_URL", "redis://x")
os.environ.setdefault("INTERNAL_TOKEN", "t" * 64)
os.environ.setdefault("LLM_BASE_URL", "http://x")
os.environ.setdefault("LLM_API_KEY", "x")
os.environ.setdefault("LLM_MODEL", "x")
os.environ.setdefault("EMBEDDING_BASE_URL", "http://x")
os.environ.setdefault("EMBEDDING_API_KEY", "x")
os.environ.setdefault("EMBEDDING_MODEL", "x")
os.environ.setdefault("JWT_SECRET_KEY", "a" * 64)
