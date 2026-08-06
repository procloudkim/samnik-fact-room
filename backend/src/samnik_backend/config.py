from __future__ import annotations

from enum import StrEnum

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RuntimeMode(StrEnum):
    LOCAL = "local"
    EXTERNAL = "external"


class CheckpointerMode(StrEnum):
    NONE = "none"
    POSTGRES = "postgres"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SAMNIK_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    runtime_mode: RuntimeMode = Field(
        default=RuntimeMode.LOCAL,
        validation_alias=AliasChoices("SAMNIK_RUNTIME_MODE", "RUNTIME_MODE"),
    )
    checkpointer_mode: CheckpointerMode = Field(
        default=CheckpointerMode.NONE,
        validation_alias=AliasChoices(
            "CHECKPOINTER_MODE",
            "SAMNIK_CHECKPOINTER_MODE",
        ),
    )
    postgres_dsn: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DATABASE_URL", "SAMNIK_POSTGRES_DSN"),
    )
    elice_base_url: str = Field(
        default="https://mlapi.run",
        validation_alias=AliasChoices("ELICE_MLAPI_BASE_URL", "SAMNIK_ELICE_BASE_URL"),
    )
    elice_api_key: str | None = Field(
        default=None,
        repr=False,
        validation_alias=AliasChoices("ELICE_MLAPI_API_KEY", "SAMNIK_ELICE_API_KEY"),
    )
    elice_model: str = Field(
        default="openai/gpt-4o-mini",
        validation_alias=AliasChoices("ELICE_MLAPI_MODEL", "SAMNIK_ELICE_MODEL"),
    )
    elice_chat_path: str = Field(
        default="/chat/completions",
        validation_alias=AliasChoices("ELICE_MLAPI_CHAT_PATH", "SAMNIK_ELICE_CHAT_PATH"),
    )
    opendart_api_key: str | None = Field(
        default=None,
        repr=False,
        validation_alias=AliasChoices("OPENDART_API_KEY", "SAMNIK_OPENDART_API_KEY"),
    )
    embedding_base_url: str | None = None
    embedding_api_key: str | None = Field(default=None, repr=False)
    embedding_model: str = Field(
        default="intfloat/multilingual-e5-small",
        validation_alias=AliasChoices("EMBEDDING_MODEL", "SAMNIK_EMBEDDING_MODEL"),
    )
    embedding_path: str = "/v1/embeddings"
    request_timeout_seconds: float = 10.0
    evidence_min_score: float = Field(default=0.78, ge=0.0, le=1.0)
    evidence_top_k: int = Field(default=8, ge=1, le=20)
    embedding_dimension: int = Field(default=384, ge=8)
