from __future__ import annotations

from samnik_backend.model_usage import (
    UsageStatus,
    ModelUsageRecord,
    normalize_model_usage,
    unreported_model_usage,
)


def test_normalize_openai_style_usage_payload() -> None:
    payload = {
        "usage": {
            "prompt_tokens": 120,
            "completion_tokens": 40,
            "total_tokens": 160,
            "prompt_tokens_details": {"cached_tokens": 8},
        }
    }
    usage = normalize_model_usage(payload, provider="elice_mlapi", model="openai/gpt-5-mini")

    assert isinstance(usage, ModelUsageRecord)
    assert usage.called is True
    assert usage.usage_status == UsageStatus.REPORTED
    assert usage.provider == "elice_mlapi"
    assert usage.model == "openai/gpt-5-mini"
    assert usage.input_tokens == 120
    assert usage.output_tokens == 40
    assert usage.cached_tokens == 8
    assert usage.total_tokens == 160


def test_normalize_anthropic_style_usage_payload() -> None:
    payload = {
        "usage": {
            "input_tokens": 250,
            "output_tokens": 55,
            "cache_read_input_tokens": 32,
        }
    }
    usage = normalize_model_usage(
        payload,
        provider="anthropic/claude",
        model="claude-3",
    )

    assert usage.called is True
    assert usage.usage_status == UsageStatus.REPORTED
    assert usage.input_tokens == 250
    assert usage.output_tokens == 55
    assert usage.cached_tokens == 32
    assert usage.total_tokens == 305


def test_unreported_fallback_when_usage_is_missing() -> None:
    usage = normalize_model_usage(
        {"choices": []},
        provider="elice_mlapi",
        model="openai/gpt-5-mini",
    )

    assert usage.usage_status == UsageStatus.UNREPORTED
    assert usage.input_tokens == usage.output_tokens == usage.cached_tokens == usage.total_tokens == 0


def test_explicit_unreported_helper_for_not_called_models() -> None:
    usage = unreported_model_usage(
        called=False,
        provider="elice_mlapi",
        model="openai/gpt-5-mini",
    )

    assert usage.called is False
    assert usage.usage_status == UsageStatus.UNREPORTED
    assert usage.input_tokens == usage.output_tokens == usage.cached_tokens == usage.total_tokens == 0
