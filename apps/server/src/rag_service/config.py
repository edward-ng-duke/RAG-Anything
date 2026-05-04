"""Application settings loaded from environment variables.

Uses pydantic-settings (pydantic v2). Required fields raise ValidationError
when missing; optional fields fall back to documented defaults.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    jwt_secret_key: str = Field(..., alias="JWT_SECRET_KEY")

    # ---- Optional ----
    vlm_model: str | None = Field(default=None, alias="VLM_MODEL")
    mineru_cloud_api_key: str | None = Field(default=None, alias="MINERU_CLOUD_API_KEY")
    parser_mode: str = Field(default="mineru_cloud", alias="PARSER_MODE")
    data_dir: Path = Field(default=Path("./data"), alias="DATA_DIR")
    max_upload_mb: int = Field(default=1000, alias="MAX_UPLOAD_MB")
    lru_instance_cap: int = Field(default=32, alias="LRU_INSTANCE_CAP")
    access_token_ttl_min: int = Field(default=15, alias="ACCESS_TOKEN_TTL_MIN")
    refresh_token_ttl_days: int = Field(default=7, alias="REFRESH_TOKEN_TTL_DAYS")

    @field_validator("jwt_secret_key")
    @classmethod
    def _validate_jwt_secret_length(cls, v: str) -> str:
        """Reject short JWT secrets — HS256 keys must be ≥ 64 chars to be safe."""
        if not isinstance(v, str) or len(v) < 64:
            raise ValueError("jwt_secret_key must be at least 64 characters long")
        return v

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
