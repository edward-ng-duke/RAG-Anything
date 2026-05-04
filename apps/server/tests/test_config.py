"""Tests for ``rag_service.config.Settings``.

Covers the ONYX-integration extension fields added in Task 1.1:

- ``internal_token`` length validation (≥ 64 chars).
- ``internal_tokens_legacy`` (comma-separated, deduplicated, length-validated).
- ``onyx_backend_allowed_cidrs`` (comma-separated, CIDR-validated).
- ``onyx_ratelimit_overrides`` (JSON dict, key-whitelist, int-only values).

Each test starts from a fresh "minimum viable env" so we never lean on any
ambient ``.env`` file or other tests' leftover ``os.environ`` state. We pass
``_env_file=None`` to disable pydantic-settings' dotenv loading per-call.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from rag_service.config import Settings


def _base_env() -> dict[str, str]:
    """All required env vars set to valid placeholders for an isolated Settings() call."""
    return {
        "DATABASE_URL": "postgresql+asyncpg://x:y@localhost/db",
        "REDIS_URL": "redis://localhost:6379/0",
        "INTERNAL_TOKEN": "x" * 96,
        "LLM_BASE_URL": "http://x",
        "LLM_API_KEY": "k",
        "LLM_MODEL": "m",
        "EMBEDDING_BASE_URL": "http://x",
        "EMBEDDING_API_KEY": "k",
        "EMBEDDING_MODEL": "m",
        "JWT_SECRET_KEY": "z" * 64,
    }


def _apply_env(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    """Set every key in ``env`` and clear the optional new ONYX vars so we
    never inherit ambient values from other tests' setdefault() calls."""
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    for k in (
        "INTERNAL_TOKENS_LEGACY",
        "ONYX_BACKEND_ALLOWED_CIDRS",
        "ONYX_RATELIMIT_OVERRIDES_JSON",
    ):
        monkeypatch.delenv(k, raising=False)


# --- internal_token length ----------------------------------------------------


def test_internal_token_min_length_accepts_64_chars(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _base_env()
    env["INTERNAL_TOKEN"] = "a" * 64
    _apply_env(monkeypatch, env)
    s = Settings(_env_file=None)
    assert s.internal_token == "a" * 64


def test_internal_token_min_length_rejects_63_chars(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _base_env()
    env["INTERNAL_TOKEN"] = "a" * 63
    _apply_env(monkeypatch, env)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


# --- internal_tokens_legacy ---------------------------------------------------


def test_internal_tokens_legacy_empty_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_env(monkeypatch, _base_env())
    # Explicitly ensure unset (delenv already done in _apply_env, but be explicit).
    monkeypatch.delenv("INTERNAL_TOKENS_LEGACY", raising=False)
    s = Settings(_env_file=None)
    assert s.internal_tokens_legacy == []


def test_internal_tokens_legacy_parses_comma_separated(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_env(monkeypatch, _base_env())
    a = "a" * 64
    b = "b" * 64
    monkeypatch.setenv("INTERNAL_TOKENS_LEGACY", f"{a},{b}")
    s = Settings(_env_file=None)
    assert s.internal_tokens_legacy == [a, b]


def test_internal_tokens_legacy_dedupes(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_env(monkeypatch, _base_env())
    a = "a" * 64
    monkeypatch.setenv("INTERNAL_TOKENS_LEGACY", f"{a},{a}")
    s = Settings(_env_file=None)
    assert s.internal_tokens_legacy == [a]


def test_internal_tokens_legacy_rejects_short_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_env(monkeypatch, _base_env())
    a = "a" * 64
    short = "b" * 10
    monkeypatch.setenv("INTERNAL_TOKENS_LEGACY", f"{a},{short}")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


# --- onyx_backend_allowed_cidrs ----------------------------------------------


def test_onyx_backend_allowed_cidrs_empty_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_env(monkeypatch, _base_env())
    monkeypatch.delenv("ONYX_BACKEND_ALLOWED_CIDRS", raising=False)
    s = Settings(_env_file=None)
    assert s.onyx_backend_allowed_cidrs == []


def test_onyx_backend_allowed_cidrs_parses_v4_cidr(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_env(monkeypatch, _base_env())
    monkeypatch.setenv("ONYX_BACKEND_ALLOWED_CIDRS", "10.0.1.0/24")
    s = Settings(_env_file=None)
    assert s.onyx_backend_allowed_cidrs == ["10.0.1.0/24"]


def test_onyx_backend_allowed_cidrs_parses_multi_with_v6(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_env(monkeypatch, _base_env())
    monkeypatch.setenv(
        "ONYX_BACKEND_ALLOWED_CIDRS",
        "10.0.1.0/24, 2001:db8::/32, 192.168.0.5/32",
    )
    s = Settings(_env_file=None)
    assert s.onyx_backend_allowed_cidrs == [
        "10.0.1.0/24",
        "2001:db8::/32",
        "192.168.0.5/32",
    ]


def test_onyx_backend_allowed_cidrs_rejects_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_env(monkeypatch, _base_env())
    monkeypatch.setenv("ONYX_BACKEND_ALLOWED_CIDRS", "not-a-cidr")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


# --- onyx_ratelimit_overrides -------------------------------------------------


def test_onyx_ratelimit_overrides_empty_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_env(monkeypatch, _base_env())
    monkeypatch.delenv("ONYX_RATELIMIT_OVERRIDES_JSON", raising=False)
    s = Settings(_env_file=None)
    assert s.onyx_ratelimit_overrides == {}


def test_onyx_ratelimit_overrides_parses_json(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_env(monkeypatch, _base_env())
    monkeypatch.setenv("ONYX_RATELIMIT_OVERRIDES_JSON", '{"query":60,"docs_post":20}')
    s = Settings(_env_file=None)
    assert s.onyx_ratelimit_overrides == {"query": 60, "docs_post": 20}


def test_onyx_ratelimit_overrides_rejects_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_env(monkeypatch, _base_env())
    monkeypatch.setenv("ONYX_RATELIMIT_OVERRIDES_JSON", "{not json")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_onyx_ratelimit_overrides_rejects_unknown_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_env(monkeypatch, _base_env())
    monkeypatch.setenv("ONYX_RATELIMIT_OVERRIDES_JSON", '{"weird":1}')
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)
    # The unknown key name should appear in the validation error so operators
    # can spot the typo without grepping source.
    assert "weird" in str(excinfo.value)


def test_onyx_ratelimit_overrides_rejects_non_int_value(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_env(monkeypatch, _base_env())
    monkeypatch.setenv("ONYX_RATELIMIT_OVERRIDES_JSON", '{"query":"60"}')
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
