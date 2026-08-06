from __future__ import annotations

import json

import httpx
import pytest

from samnik_backend.domain import (
    Company,
    ConversationRecord,
    Intent,
    Metric,
    Slots,
    WorkflowStatus,
)
from samnik_backend.errors import ProviderUnavailable
from samnik_backend.providers import EliceMLAPIProvider


@pytest.mark.asyncio
async def test_elice_provider_uses_mock_transport_and_validates_classification_without_network() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "intent": "DIRECT_FACT",
                                    "extracted_slots": {
                                        "company": "SAMSUNG_ELECTRONICS",
                                        "metric": "operating_profit",
                                        "period": "2025",
                                        "value_scope": "CUMULATIVE",
                                        "fs_div": "CFS",
                                    },
                                    "rationale_code": "CLASSIFIED",
                                }
                            )
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = EliceMLAPIProvider(
        base_url="https://mock.elice.invalid/v1",
        api_key="synthetic-test-key",
        model="mock-model",
        chat_path="/chat/completions",
        client=client,
    )
    try:
        decision = await provider.classify("2025년 삼성전자 영업이익은?", None)
    finally:
        await client.aclose()

    assert decision.intent == Intent.DIRECT_FACT
    assert seen["url"] == "https://mock.elice.invalid/v1/chat/completions"
    assert seen["authorization"] == "Bearer synthetic-test-key"
    assert seen["body"]["model"] == "mock-model"
    assert seen["body"]["temperature"] == 0
    assert seen["body"]["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_elice_provider_fails_closed_on_unusable_mock_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "not-json"}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = EliceMLAPIProvider(
        base_url="https://mock.elice.invalid",
        api_key="synthetic-test-key",
        model="mock-model",
        client=client,
    )
    try:
        with pytest.raises(ProviderUnavailable):
            await provider.classify("테스트", None)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_elice_provider_normalizes_integer_year_before_contract_validation() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "intent": "DIRECT_FACT",
                                    "extracted_slots": {
                                        "company": "SAMSUNG_ELECTRONICS",
                                        "metric": "operating_profit",
                                        "period": 2025,
                                        "value_scope": "CUMULATIVE",
                                        "fs_div": "CFS",
                                    },
                                    "rationale_code": "CLASSIFIED",
                                }
                            )
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = EliceMLAPIProvider(
        base_url="https://mock.elice.invalid/v1",
        api_key="synthetic-test-key",
        model="mock-model",
        chat_path="/chat/completions",
        client=client,
    )
    try:
        decision = await provider.classify("2025년 삼성전자 영업이익은?", None)
    finally:
        await client.aclose()

    assert decision.extracted_slots.period == "2025"


@pytest.mark.asyncio
async def test_elice_provider_enforces_compare_intent_from_two_company_claim() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "intent": "CLAIM_CHECK",
                                    "extracted_slots": {
                                        "company": "SAMSUNG_ELECTRONICS",
                                        "comparison_company": "SK_HYNIX",
                                        "metric": "operating_profit",
                                        "value_scope": "CUMULATIVE",
                                        "fs_div": "CFS",
                                    },
                                    "rationale_code": "MODEL_MISCLASSIFIED_COMPARE",
                                }
                            )
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = EliceMLAPIProvider(
        base_url="https://mock.elice.invalid/v1",
        api_key="synthetic-test-key",
        model="mock-model",
        client=client,
    )
    message = "삼전 영업이익이 하닉에 따였다던데 사실이야?"
    try:
        decision = await provider.classify(message, None)
    finally:
        await client.aclose()

    assert decision.intent == Intent.COMPARE
    assert decision.extracted_slots.user_claim == message
    assert decision.extracted_slots.claim_operator.value == "LT"


@pytest.mark.asyncio
async def test_elice_provider_preserves_full_user_text_for_narrative_query() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "intent": "CLAIM_CHECK",
                                    "extracted_slots": {
                                        "company": "SAMSUNG_ELECTRONICS",
                                        "topic_or_claim": "연구개발 활동",
                                        "fs_div": "CFS",
                                    },
                                    "rationale_code": "NARRATIVE_QUERY",
                                }
                            )
                        }
                    }
                ]
            },
        )

    message = "2025년 삼성전자 사업보고서에서 연구개발 활동 관련 근거를 찾아줘."
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = EliceMLAPIProvider(
        base_url="https://mock.elice.invalid/v1",
        api_key="synthetic-test-key",
        model="mock-model",
        client=client,
    )
    try:
        decision = await provider.classify(message, None)
    finally:
        await client.aclose()

    assert decision.intent == Intent.CLAIM_CHECK
    assert decision.extracted_slots.topic_or_claim == message
    assert decision.extracted_slots.user_claim == message


@pytest.mark.asyncio
async def test_elice_provider_corrects_filing_search_misclassified_as_advice() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "intent": "OUT_OF_SCOPE",
                                    "extracted_slots": {},
                                    "rationale_code": "ADVICE_OR_PREDICTION_REQUEST",
                                }
                            )
                        }
                    }
                ]
            },
        )

    message = "삼성전자 2025년 연구개발 활동 관련 공시 근거를 찾아줘."
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = EliceMLAPIProvider(
        base_url="https://mock.elice.invalid/v1",
        api_key="synthetic-test-key",
        model="mock-model",
        client=client,
    )
    try:
        decision = await provider.classify(message, None)
    finally:
        await client.aclose()

    assert decision.intent == Intent.CLAIM_CHECK
    assert decision.extracted_slots.company == Company.SAMSUNG_ELECTRONICS
    assert decision.extracted_slots.topic_or_claim == message
    assert decision.extracted_slots.user_claim == message


@pytest.mark.asyncio
async def test_elice_provider_corrects_public_quarter_example_and_period_mode() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "intent": "OUT_OF_SCOPE",
                                    "extracted_slots": {},
                                    "rationale_code": "MODEL_MISCLASSIFIED",
                                }
                            )
                        }
                    }
                ]
            },
        )

    message = "두 회사의 최근 공통 분기 영업이익을 비교해줘"
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = EliceMLAPIProvider(
        base_url="https://mock.elice.invalid/v1",
        api_key="synthetic-test-key",
        model="mock-model",
        client=client,
    )
    try:
        decision = await provider.classify(message, None)
    finally:
        await client.aclose()

    assert decision.intent == Intent.COMPARE
    assert decision.extracted_slots.company == Company.SAMSUNG_ELECTRONICS
    assert decision.extracted_slots.comparison_company == Company.SK_HYNIX
    assert decision.extracted_slots.period_mode.value == "LATEST_COMMON_INTERIM"


@pytest.mark.asyncio
async def test_elice_provider_keeps_pending_compare_intent_for_short_followup() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "intent": "DIRECT_FACT",
                                    "extracted_slots": {
                                        "period": 2025,
                                        "value_scope": "CUMULATIVE",
                                        "fs_div": "CFS",
                                    },
                                    "rationale_code": "FOLLOWUP_PERIOD",
                                }
                            )
                        }
                    }
                ]
            },
        )

    prior = ConversationRecord(
        conversation_id="compare-followup",
        intent=Intent.COMPARE,
        collected_slots=Slots(
            company=Company.SAMSUNG_ELECTRONICS,
            comparison_company=Company.SK_HYNIX,
            metric=Metric.OPERATING_PROFIT,
        ),
        missing_slots=["period_mode"],
        workflow_status=WorkflowStatus.NEED_MORE_INFO,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = EliceMLAPIProvider(
        base_url="https://mock.elice.invalid/v1",
        api_key="synthetic-test-key",
        model="mock-model",
        client=client,
    )
    try:
        decision = await provider.classify(
            "응, 2025년 연간 연결 기준으로 봐줘.",
            prior,
        )
    finally:
        await client.aclose()

    assert decision.intent == Intent.COMPARE
    assert decision.extracted_slots.period == "2025"


@pytest.mark.asyncio
async def test_elice_http_failure_does_not_retain_bearer_header_cause() -> None:
    secret = "synthetic-bearer-secret"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="synthetic upstream failure")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = EliceMLAPIProvider(
        base_url="https://mock.elice.invalid/v1",
        api_key=secret,
        model="mock-model",
        chat_path="/chat/completions",
        client=client,
    )
    try:
        with pytest.raises(ProviderUnavailable) as caught:
            await provider.classify("테스트", None)
    finally:
        await client.aclose()

    assert secret not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.asyncio
async def test_elice_provider_retries_one_transient_http_failure() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, text="synthetic transient failure")
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "intent": "DIRECT_FACT",
                                    "extracted_slots": {
                                        "company": "SAMSUNG_ELECTRONICS",
                                        "metric": "operating_profit",
                                        "period": "2025",
                                        "value_scope": "CUMULATIVE",
                                        "fs_div": "CFS",
                                    },
                                    "rationale_code": "DIRECT_FACT",
                                }
                            )
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = EliceMLAPIProvider(
        base_url="https://mock.elice.invalid/v1",
        api_key="synthetic-test-key",
        model="mock-model",
        client=client,
    )
    try:
        decision = await provider.classify(
            "2025년 삼성전자 연결 영업이익은 얼마야?",
            None,
        )
    finally:
        await client.aclose()

    assert calls == 2
    assert decision.intent == Intent.DIRECT_FACT
