from __future__ import annotations

import json

import httpx

from samnik_backend.config import Settings
from samnik_backend.domain import Intent
from samnik_backend.model_usage import UsageStatus
from samnik_backend.providers import EliceMLAPIProvider, OpenDartClient, RuleBasedLanguageModel


async def test_elice_adapter_uses_verified_openai_compatible_contract() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content)
        content = {
            "intent": "DIRECT_FACT",
            "extracted_slots": {
                "company": "SAMSUNG_ELECTRONICS",
                "metric": "operating_profit",
                "period": "2025",
                "fs_div": "CFS",
            },
            "rationale_code": "CLASSIFIED",
        }
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(content)}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = EliceMLAPIProvider(
        base_url="https://mlapi.run/test/v1",
        api_key="test-only-key",
        model="openai/gpt-5-mini",
        chat_path="/chat/completions",
        client=client,
    )
    try:
        decision = await provider.classify("2025년 삼성전자 영업이익은?", None)
        assert decision.intent == Intent.DIRECT_FACT
        assert captured["url"] == "https://mlapi.run/test/v1/chat/completions"
        assert captured["authorization"] == "Bearer test-only-key"
        assert captured["body"]["model"] == "openai/gpt-5-mini"
        assert captured["body"]["response_format"] == {"type": "json_object"}
    finally:
        await client.aclose()


async def test_elice_adapter_records_reported_usage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        content = {
            "intent": "DIRECT_FACT",
            "extracted_slots": {
                "company": "SAMSUNG_ELECTRONICS",
                "metric": "operating_profit",
                "period": "2025",
                "fs_div": "CFS",
            },
            "rationale_code": "CLASSIFIED",
        }
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(content)}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 18, "total_tokens": 21},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = EliceMLAPIProvider(
        base_url="https://mlapi.run/test/v1",
        api_key="test-only-key",
        model="openai/gpt-5-mini",
        chat_path="/chat/completions",
        client=client,
    )
    try:
        await provider.classify("2025년 삼성전자 영업이익은?", None)
        assert provider.last_usage.called is True
        assert provider.last_usage.usage_status == UsageStatus.REPORTED
        assert provider.last_usage.input_tokens == 3
        assert provider.last_usage.output_tokens == 18
        assert provider.last_usage.total_tokens == 21
    finally:
        await client.aclose()


async def test_elice_adapter_records_unreported_usage_when_missing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        content = {
            "intent": "DIRECT_FACT",
            "extracted_slots": {
                "company": "SAMSUNG_ELECTRONICS",
                "metric": "operating_profit",
                "period": "2025",
                "fs_div": "CFS",
            },
            "rationale_code": "CLASSIFIED",
        }
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(content)}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = EliceMLAPIProvider(
        base_url="https://mlapi.run/test/v1",
        api_key="test-only-key",
        model="openai/gpt-5-mini",
        chat_path="/chat/completions",
        client=client,
    )
    try:
        await provider.classify("2025년 삼성전자 영업이익은?", None)
        assert provider.last_usage.called is True
        assert provider.last_usage.usage_status == UsageStatus.UNREPORTED
        assert provider.last_usage.input_tokens == 0
    finally:
        await client.aclose()


async def test_rule_based_provider_reports_unreported_usage() -> None:
    provider = RuleBasedLanguageModel()
    await provider.classify("2025년 삼성전자 영업이익은?", None)
    # ensure provider-level call accounting remains deterministic for local model
    assert provider.last_usage.called is False
    assert provider.last_usage.usage_status == UsageStatus.UNREPORTED


async def test_rule_based_provider_reports_unreported_usage_after_compose() -> None:
    provider = RuleBasedLanguageModel()
    await provider.compose_answer(
        None,
        facts=[],
        verdict="CONFIRMED",
        limitations=[],
    )
    assert provider.last_usage.called is False
    assert provider.last_usage.usage_status == UsageStatus.UNREPORTED


async def test_opendart_adapter_is_mocked_and_backend_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["crtfc_key"] == "test-key"
        assert request.url.params["corp_code"] == "00126380"
        return httpx.Response(200, json={"status": "000", "list": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenDartClient("test-key", client=client)
    try:
        result = await provider.list_filings("00126380", "20250101", "20251231")
        assert result["status"] == "000"
    finally:
        await client.aclose()


def test_settings_accept_canonical_environment_names(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://example/test")
    monkeypatch.setenv("ELICE_MLAPI_API_KEY", "secret")
    monkeypatch.setenv("ELICE_MLAPI_MODEL", "openai/gpt-5-mini")
    monkeypatch.setenv("OPENDART_API_KEY", "dart-secret")
    settings = Settings(_env_file=None)
    assert settings.postgres_dsn == "postgresql://example/test"
    assert settings.elice_api_key == "secret"
    assert settings.elice_model == "openai/gpt-5-mini"
    assert settings.opendart_api_key == "dart-secret"
