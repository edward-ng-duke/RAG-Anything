"""Application settings loaded from environment variables.

Uses pydantic-settings (pydantic v2). Required fields raise ValidationError
when missing; optional fields fall back to documented defaults.
"""

from __future__ import annotations

import ipaddress
import json
from pathlib import Path
from typing import Annotated, Any

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Whitelist of allowed keys for ``onyx_ratelimit_overrides``. Any key outside
# this set is treated as a typo and rejected, so operators learn about
# misspellings at startup rather than at request time.
_ONYX_RATELIMIT_KEYS: frozenset[str] = frozenset(
    {"query", "docs_post", "kg", "other", "token_total"}
)


class Settings(BaseSettings):
    """Runtime configuration for the rag_service backend."""

    # ---- Required ----
    database_url: str = Field(..., alias="DATABASE_URL")
    redis_url: str = Field(..., alias="REDIS_URL")
    internal_token: str = Field(..., alias="INTERNAL_TOKEN")
    llm_base_url: str = Field(..., alias="LLM_BASE_URL")
    llm_api_key: str = Field(..., alias="LLM_API_KEY")
    llm_model: str = Field(..., alias="LLM_MODEL")
    embedding_base_url: str = Field(..., alias="EMBEDDING_BASE_URL")
    embedding_api_key: str = Field(..., alias="EMBEDDING_API_KEY")
    embedding_model: str = Field(..., alias="EMBEDDING_MODEL")
    # Output dimensionality of the embedding model. Must match what the
    # provider returns (1536 for OpenAI text-embedding-3-small / ada-002,
    # 1024 for Qwen3-Embedding-0.6B, 768 for many small open models).
    embedding_dim: int = Field(default=1536, alias="EMBEDDING_DIM")
    jwt_secret_key: str = Field(..., alias="JWT_SECRET_KEY")

    # ---- Optional ----
    vlm_model: str | None = Field(default=None, alias="VLM_MODEL")
    # Accept both names: ``MINERU_CLOUD_API_KEY`` (task-spec preferred) and
    # ``MINERU_CLOUD_TOKEN`` (the name used by the reference parsing script
    # and many existing ``.env`` files).
    mineru_cloud_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("MINERU_CLOUD_API_KEY", "MINERU_CLOUD_TOKEN"),
    )
    parser_mode: str = Field(default="mineru_cloud", alias="PARSER_MODE")
    data_dir: Path = Field(default=Path("./data"), alias="DATA_DIR")
    max_upload_mb: int = Field(default=1000, alias="MAX_UPLOAD_MB")
    lru_instance_cap: int = Field(default=32, alias="LRU_INSTANCE_CAP")
    access_token_ttl_min: int = Field(default=15, alias="ACCESS_TOKEN_TTL_MIN")
    refresh_token_ttl_days: int = Field(default=7, alias="REFRESH_TOKEN_TTL_DAYS")

    # ---- ONYX integration knobs ----
    # Each of these reads a custom env-string format (comma-separated, JSON).
    # We use ``Annotated[..., NoDecode]`` so pydantic-settings hands the raw
    # env string to our validator instead of trying to JSON-decode it itself.
    #
    # Legacy ``INTERNAL_TOKEN`` values still accepted during a token rotation.
    # Comma-separated; deduplicated; each entry must satisfy the same ≥ 64
    # length floor as the live token.
    internal_tokens_legacy: Annotated[list[str], NoDecode] = Field(
        default_factory=list, alias="INTERNAL_TOKENS_LEGACY"
    )
    # Comma-separated CIDR allowlist for ONYX-side traffic. Empty = no
    # allowlist enforced. Validated as ``ipaddress.ip_network(strict=False)``.
    onyx_backend_allowed_cidrs: Annotated[list[str], NoDecode] = Field(
        default_factory=list, alias="ONYX_BACKEND_ALLOWED_CIDRS"
    )
    # JSON dict of per-bucket rate-limit overrides. Keys constrained to a
    # whitelist (see :data:`_ONYX_RATELIMIT_KEYS`); values must be ints.
    onyx_ratelimit_overrides: Annotated[dict[str, int], NoDecode] = Field(
        default_factory=dict, alias="ONYX_RATELIMIT_OVERRIDES_JSON"
    )

    @field_validator("jwt_secret_key")
    @classmethod
    def _validate_jwt_secret_length(cls, v: str) -> str:
        """Reject short JWT secrets — HS256 keys must be ≥ 64 chars to be safe."""
        if not isinstance(v, str) or len(v) < 64:
            raise ValueError("jwt_secret_key must be at least 64 characters long")
        return v

    @field_validator("internal_token")
    @classmethod
    def _validate_internal_token_length(cls, v: str) -> str:
        """Reject short ``INTERNAL_TOKEN`` values.

        This is the service-grade shared secret authenticating ONYX → RAG
        traffic; short tokens are unacceptable for that role.
        """
        if not isinstance(v, str) or len(v) < 64:
            raise ValueError("internal_token must be at least 64 characters long")
        return v

    @field_validator("internal_tokens_legacy", mode="before")
    @classmethod
    def _parse_internal_tokens_legacy(cls, v: Any) -> list[str]:
        """Parse a comma-separated env string into a deduplicated list.

        Empty / unset / blank → ``[]``. Order of first occurrence is
        preserved when de-duplicating so operators can predict behavior.
        Each non-empty entry must be ≥ 64 chars; otherwise we raise so
        startup fails loudly.
        """
        if v is None or v == "":
            return []
        if isinstance(v, list):
            tokens = [str(t).strip() for t in v if str(t).strip()]
        elif isinstance(v, str):
            tokens = [t.strip() for t in v.split(",") if t.strip()]
        else:
            raise ValueError(
                "INTERNAL_TOKENS_LEGACY must be a comma-separated string"
            )
        # Order-preserving dedup: dict.fromkeys keeps insertion order in
        # Python 3.7+, which is the contract we want for token rotation
        # (newer entries first → older ones at the tail).
        deduped = list(dict.fromkeys(tokens))
        for tok in deduped:
            if len(tok) < 64:
                raise ValueError(
                    "every INTERNAL_TOKENS_LEGACY entry must be at least 64 "
                    "characters long"
                )
        return deduped

    @field_validator("onyx_backend_allowed_cidrs", mode="before")
    @classmethod
    def _parse_onyx_backend_allowed_cidrs(cls, v: Any) -> list[str]:
        """Parse comma-separated CIDRs and validate each one.

        Uses ``strict=False`` so host-bit-set forms like ``192.168.0.5/32``
        round-trip cleanly. Returns the canonical string form of each
        network so downstream code doesn't need to re-parse.
        """
        if v is None or v == "":
            return []
        if isinstance(v, list):
            raw = [str(t).strip() for t in v if str(t).strip()]
        elif isinstance(v, str):
            raw = [t.strip() for t in v.split(",") if t.strip()]
        else:
            raise ValueError(
                "ONYX_BACKEND_ALLOWED_CIDRS must be a comma-separated string"
            )
        out: list[str] = []
        for entry in raw:
            try:
                net = ipaddress.ip_network(entry, strict=False)
            except (ValueError, TypeError) as exc:
                raise ValueError(
                    f"invalid CIDR in ONYX_BACKEND_ALLOWED_CIDRS: {entry!r} ({exc})"
                ) from exc
            out.append(str(net))
        return out

    @field_validator("onyx_ratelimit_overrides", mode="before")
    @classmethod
    def _parse_onyx_ratelimit_overrides(cls, v: Any) -> dict[str, int]:
        """Parse a JSON dict env string with strict key/value typing.

        Empty / unset → ``{}``. Invalid JSON, unknown key, or non-int value
        all raise so misconfiguration is caught at startup rather than at
        the rate-limit decision point.
        """
        if v is None or v == "":
            return {}
        if isinstance(v, dict):
            data: dict[str, Any] = dict(v)
        elif isinstance(v, str):
            try:
                data = json.loads(v)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"ONYX_RATELIMIT_OVERRIDES_JSON is not valid JSON: {exc}"
                ) from exc
            if not isinstance(data, dict):
                raise ValueError(
                    "ONYX_RATELIMIT_OVERRIDES_JSON must be a JSON object"
                )
        else:
            raise ValueError(
                "ONYX_RATELIMIT_OVERRIDES_JSON must be a JSON object string"
            )
        out: dict[str, int] = {}
        for key, value in data.items():
            if key not in _ONYX_RATELIMIT_KEYS:
                raise ValueError(
                    f"unknown key {key!r} in ONYX_RATELIMIT_OVERRIDES_JSON; "
                    f"allowed keys: {sorted(_ONYX_RATELIMIT_KEYS)}"
                )
            # ``bool`` is a subclass of ``int`` in Python; reject it
            # explicitly so ``true``/``false`` don't sneak through as 1/0.
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(
                    f"value for {key!r} in ONYX_RATELIMIT_OVERRIDES_JSON "
                    f"must be an int, got {type(value).__name__}"
                )
            out[key] = value
        return out

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


_settings: Settings | None = None


def __getattr__(name: str) -> Settings:
    """Lazy module-level ``settings`` singleton.

    Accessing ``rag_service.config.settings`` constructs the ``Settings``
    instance on first access. Deferring construction lets the module import
    cleanly in environments where env vars are filled in just-in-time
    (tests, multi-stage app startup). Any subsequent access returns the
    cached instance.
    """
    global _settings
    if name == "settings":
        if _settings is None:
            _settings = Settings()  # type: ignore[call-arg]
        return _settings
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
