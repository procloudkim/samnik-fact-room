from __future__ import annotations

from typing import Any, Mapping
from enum import Enum

from pydantic import BaseModel, Field, field_validator


class UsageStatus(str, Enum):
    REPORTED = "REPORTED"
    UNREPORTED = "UNREPORTED"


class ModelUsageRecord(BaseModel):
    """Validated token accounting payload shared by provider integrations."""

    called: bool
    provider: str
    model: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_tokens: int = Field(ge=0, default=0)
    total_tokens: int = Field(ge=0)
    usage_status: UsageStatus

    @field_validator("provider", "model")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("provider/model must be non-empty")
        return normalized


def _to_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        trimmed = value.strip()
        return int(trimmed) if trimmed.isdigit() else None
    return None


def _get_nested(mapping: Mapping[str, Any], path: str) -> Any:
    node: Any = mapping
    for key in path.split("."):
        if not isinstance(node, Mapping) or key not in node:
            return None
        node = node[key]
    return node


def _first_int(mapping: Mapping[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        if "." in key:
            value = _get_nested(mapping, key)
        else:
            value = mapping.get(key)
        value_int = _to_int(value)
        if value_int is not None:
            return value_int
    return None


def _extract_usage_mapping(payload: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if payload is None:
        return None
    if "usage" in payload and isinstance(payload["usage"], Mapping):
        return payload["usage"]
    if "usageMetadata" in payload and isinstance(payload["usageMetadata"], Mapping):
        return payload["usageMetadata"]
    # Some APIs may already return the usage block directly.
    if any(key in payload for key in ("input_tokens", "prompt_tokens", "total_tokens")):
        return payload
    return None


def _has_token_signal(usage: Mapping[str, Any]) -> bool:
    candidate_keys = (
        "input_tokens",
        "output_tokens",
        "prompt_tokens",
        "completion_tokens",
        "prompt_tokens_details.cached_tokens",
        "completion_tokens_details.cached_tokens",
        "cached_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "total_tokens",
        "totalTokenCount",
    )
    return any(_first_int(usage, (key,)) is not None for key in candidate_keys)


def unreported_model_usage(*, called: bool, provider: str, model: str) -> ModelUsageRecord:
    """Return a safe fallback when usage telemetry is missing or not trusted."""

    return ModelUsageRecord(
        called=called,
        provider=provider,
        model=model,
        input_tokens=0,
        output_tokens=0,
        cached_tokens=0,
        total_tokens=0,
        usage_status=UsageStatus.UNREPORTED,
    )


def normalize_model_usage(
    payload: Mapping[str, Any] | None,
    *,
    provider: str,
    model: str,
    called: bool = True,
) -> ModelUsageRecord:
    """
    Normalize provider response payloads into a stable usage record.

    No prompt text, cost, or other sensitive fields are parsed or returned.
    """

    usage = _extract_usage_mapping(payload)
    if not called or usage is None or not _has_token_signal(usage):
        return unreported_model_usage(called=called, provider=provider, model=model)

    input_tokens = _first_int(
        usage,
        ("input_tokens", "prompt_tokens", "inputTokenCount"),
    ) or 0
    output_tokens = _first_int(
        usage,
        ("output_tokens", "completion_tokens", "outputTokenCount"),
    ) or 0
    cached_tokens = (
        _first_int(
            usage,
            (
                "cached_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
                "prompt_tokens_details.cached_tokens",
                "completion_tokens_details.cached_tokens",
            ),
        )
        or 0
    )
    total_tokens = (
        _first_int(
            usage,
            ("total_tokens", "totalTokenCount"),
        )
        or input_tokens + output_tokens
    )
    return ModelUsageRecord(
        called=called,
        provider=provider,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        total_tokens=total_tokens,
        usage_status=UsageStatus.REPORTED,
    )
