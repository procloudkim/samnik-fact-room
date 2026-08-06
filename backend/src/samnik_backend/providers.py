from __future__ import annotations

import hashlib
import json
import re
import asyncio
from functools import partial
from datetime import UTC, datetime
from decimal import Decimal
from typing import Sequence

import httpx

from .domain import (
    AnswerDraft,
    ClaimOperator,
    Company,
    ConversationRecord,
    FsDiv,
    Intent,
    Metric,
    PeriodMode,
    RouteDecision,
    Slots,
    ValueScope,
    WorkflowStatus,
)
from .errors import ProviderUnavailable
from .model_usage import (
    ModelUsageRecord,
    normalize_model_usage,
    unreported_model_usage,
)


_OUT_OF_SCOPE = re.compile(
    r"(사도\s*돼|팔아야|매수|매도|목표가|수익률|오를까|내릴까|주가\s*예측|보유해)",
    re.IGNORECASE,
)

_FILING_SEARCH = re.compile(
    r"(?=.*(?:공시|사업보고서))(?=.*(?:찾아|검색|근거|확인))",
    re.IGNORECASE,
)

_RESEARCH_LENS = re.compile(
    r"(본업\s*수익성|최종\s*수익성|순이익\s*전환|"
    r"재무\s*(?:건전성|안정성)|현금\s*(?:창출력|전환력)|"
    r"재고자산.*매출채권|일회성\s*손익|손상차손|"
    r"전환사채.*스톡옵션|주식\s*희석|자본배분|"
    r"핵심감사사항|회계\s*위험|사업부문.*(?:주요\s*고객|집중도))",
    re.IGNORECASE,
)


def _is_research_lens(text: str) -> bool:
    """Research lenses need a topic, not a forced single metric slot."""

    return "비교" not in text and bool(_RESEARCH_LENS.search(text))


def _extract_company(text: str) -> list[Company]:
    found: list[tuple[int, Company]] = []
    aliases = {
        Company.SAMSUNG_ELECTRONICS: ("삼성전자", "삼전", "005930"),
        Company.SK_HYNIX: ("SK하이닉스", "하이닉스", "하닉", "000660"),
    }
    lowered = text.lower()
    for company, names in aliases.items():
        positions = [lowered.find(name.lower()) for name in names if lowered.find(name.lower()) >= 0]
        if positions:
            found.append((min(positions), company))
    companies = [company for _, company in sorted(found, key=lambda item: item[0])]
    if not companies and "두 회사" in text:
        return [Company.SAMSUNG_ELECTRONICS, Company.SK_HYNIX]
    return companies


def _extract_period_mode(text: str) -> PeriodMode | None:
    if "최근" not in text or not ("공통" in text or "같은" in text):
        return None
    if "분기" in text:
        return PeriodMode.LATEST_COMMON_INTERIM
    return PeriodMode.LATEST_COMMON


def _extract_metric(text: str) -> Metric | None:
    aliases = (
        (Metric.OPERATING_PROFIT, ("영업이익", "영익")),
        (Metric.NET_INCOME, ("당기순이익", "순이익")),
        (Metric.TOTAL_ASSETS, ("자산총계", "총자산")),
        (Metric.TOTAL_LIABILITIES, ("부채총계", "총부채")),
        (Metric.TOTAL_EQUITY, ("자본총계", "총자본")),
        (Metric.OPERATING_CASH_FLOW, ("영업활동현금흐름", "영업현금흐름")),
        (Metric.REVENUE, ("매출액", "매출")),
    )
    for metric, names in aliases:
        if any(name in text for name in names):
            return metric
    return None


class RuleBasedLanguageModel:
    """Deterministic local provider for development and tests.

    Production uses the same port through :class:`EliceMLAPIProvider`. Keeping
    this provider local prevents accidental external calls in the test suite.
    """

    provider_name = "local_rules"

    def __init__(self) -> None:
        self.last_usage: ModelUsageRecord = unreported_model_usage(
            called=False,
            provider=self.provider_name,
            model=self.provider_name,
        )

    async def classify(self, message: str, prior: ConversationRecord | None) -> RouteDecision:
        self.last_usage = unreported_model_usage(
            called=False,
            provider=self.provider_name,
            model=self.provider_name,
        )
        if _OUT_OF_SCOPE.search(message):
            return RouteDecision(
                intent=Intent.OUT_OF_SCOPE,
                extracted_slots=Slots(),
                rationale_code="ADVICE_OR_PREDICTION_REQUEST",
            )

        companies = _extract_company(message)
        metric = _extract_metric(message)
        period_match = re.search(r"\b(20\d{2})(?:년)?\b", message)
        period = period_match.group(1) if period_match else None
        latest_common = _extract_period_mode(message)

        prior_intent = prior.intent if prior else None
        segment_question = bool(re.search(r"\bDS\b|부문", message, re.IGNORECASE))
        if _is_research_lens(message):
            intent = Intent.CLAIM_CHECK
        elif len(companies) >= 2 or "비교" in message or "따였다" in message:
            intent = Intent.COMPARE
        elif prior_intent == Intent.COMPARE and not companies and (latest_common or len(message) < 80):
            intent = Intent.COMPARE
        elif segment_question or "사실" in message or "소문" in message or "승인" in message:
            intent = Intent.CLAIM_CHECK
        elif metric is not None:
            intent = Intent.DIRECT_FACT
        elif prior_intent is not None:
            intent = prior_intent
        else:
            intent = Intent.CLAIM_CHECK

        value_scope = None
        if metric in {Metric.TOTAL_ASSETS, Metric.TOTAL_LIABILITIES, Metric.TOTAL_EQUITY}:
            value_scope = ValueScope.POINT_IN_TIME
        elif metric is not None:
            value_scope = ValueScope.CUMULATIVE

        slots = Slots(
            company=companies[0] if companies else None,
            comparison_company=companies[1] if len(companies) > 1 else None,
            metric=metric,
            period=period,
            period_mode=latest_common,
            value_scope=value_scope,
            fs_div=FsDiv.CFS,
            topic_or_claim=message if intent == Intent.CLAIM_CHECK else None,
            user_claim=message if intent in {Intent.CLAIM_CHECK, Intent.COMPARE} else None,
            claim_operator=(
                ClaimOperator.LT if intent == Intent.COMPARE and "따였다" in message else None
            ),
            as_of=datetime.now(UTC),
        )
        return RouteDecision(intent=intent, extracted_slots=slots)

    async def compose_answer(
        self,
        query,
        facts,
        verdict: str,
        limitations: Sequence[str],
    ) -> AnswerDraft:
        self.last_usage = unreported_model_usage(
            called=False,
            provider=self.provider_name,
            model=self.provider_name,
        )
        if verdict == "CONFIRMED" and len(facts) == 2:
            left, right = facts
            relation = "낮습니다" if left.value < right.value else "높거나 같습니다"
            return AnswerDraft(
                conclusion=f"동일 기준 공시 수치로 비교하면 {left.company.value}의 값이 {right.company.value}보다 {relation}."
            )
        if verdict == "CONFIRMED":
            return AnswerDraft(conclusion="조회 범위의 공식 공시에서 해당 사실을 확인했습니다.")
        if verdict == "CONTRADICTED":
            return AnswerDraft(conclusion="동일 기준 공시 수치는 입력한 주장과 일치하지 않습니다.")
        return AnswerDraft(conclusion="조회 결과와 확인 한계를 함께 안내합니다.")


class HashEmbeddingProvider:
    """Stable local embedding used only for isolated development/tests."""

    def __init__(self, dimension: int = 32) -> None:
        self.dimension = dimension

    async def embed(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        values = [((digest[index % len(digest)] / 255.0) * 2.0) - 1.0 for index in range(self.dimension)]
        magnitude = sum(value * value for value in values) ** 0.5 or 1.0
        return [value / magnitude for value in values]


class LocalSentenceTransformerEmbeddingProvider:
    """Lazy CPU query embedding for the canonical 384d multilingual E5 model."""

    def __init__(
        self,
        model_name: str = "intfloat/multilingual-e5-small",
        *,
        local_files_only: bool = False,
    ) -> None:
        self.model_name = model_name
        self.local_files_only = local_files_only
        self._model = None
        self._load_lock = asyncio.Lock()

    async def _get_model(self):
        if self._model is None:
            async with self._load_lock:
                if self._model is None:
                    from sentence_transformers import SentenceTransformer

                    self._model = await asyncio.to_thread(
                        partial(
                            SentenceTransformer,
                            self.model_name,
                            device="cpu",
                            local_files_only=self.local_files_only,
                        )
                    )
        return self._model

    async def embed(self, text: str) -> list[float]:
        model = await self._get_model()
        vector = await asyncio.to_thread(
            model.encode,
            f"query: {text}",
            normalize_embeddings=True,
            convert_to_numpy=True,
        )
        values = vector.tolist()
        if len(values) != 384:
            raise ProviderUnavailable(
                f"{self.model_name} returned {len(values)} dimensions; expected 384"
            )
        return [float(value) for value in values]


class OpenAICompatibleEmbeddingProvider:
    """Production embedding adapter, configured independently from chat MLAPI."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        path: str = "/v1/embeddings",
        timeout_seconds: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = f"{base_url.rstrip('/')}{path}"
        self._api_key = api_key
        self._model = model
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_client = client is None

    async def embed(self, text: str) -> list[float]:
        try:
            response = await self._client.post(
                self._url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"model": self._model, "input": text},
            )
            response.raise_for_status()
            return [float(value) for value in response.json()["data"][0]["embedding"]]
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            # Authorization headers live on httpx request objects. Do not keep
            # the upstream exception as a loggable cause.
            raise ProviderUnavailable("embedding provider returned an unusable response") from None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class EliceMLAPIProvider:
    """Configurable OpenAI-compatible adapter for Elice MLAPI.

    The endpoint shape is deliberately isolated here because the institution's
    deployment contract must be confirmed independently of domain code.
    """

    provider_name = "elice_mlapi"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        chat_path: str = "/v1/chat/completions",
        timeout_seconds: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._chat_path = chat_path
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_client = client is None
        self.last_usage: ModelUsageRecord = unreported_model_usage(
            called=False,
            provider=self.provider_name,
            model=self._model,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _json_completion(self, system: str, user: str) -> dict:
        for attempt in range(2):
            try:
                response = await self._client.post(
                    f"{self._base_url}{self._chat_path}",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={
                        "model": self._model,
                        "temperature": 0,
                        "response_format": {"type": "json_object"},
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                    },
                )
                response.raise_for_status()
                payload = response.json()
                self.last_usage = normalize_model_usage(
                    payload,
                    provider=self.provider_name,
                    model=self._model,
                    called=True,
                )
                content = payload["choices"][0]["message"]["content"]
                return content if isinstance(content, dict) else json.loads(content)
            except (httpx.HTTPError, KeyError, TypeError, json.JSONDecodeError):
                self.last_usage = unreported_model_usage(
                    called=True,
                    provider=self.provider_name,
                    model=self._model,
                )
                if attempt == 0:
                    continue
                # The caught httpx exception retains the bearer header on its
                # request object, so exception chaining would keep the secret alive.
                raise ProviderUnavailable("Elice MLAPI returned an unusable response") from None
        raise AssertionError("unreachable")

    async def classify(self, message: str, prior: ConversationRecord | None) -> RouteDecision:
        system = (
            "Classify a Korean Samsung Electronics/SK hynix filing question. "
            "Return one JSON object with exactly these top-level keys: "
            '"intent", "extracted_slots", and "rationale_code". '
            'The value of "intent" must be one of DIRECT_FACT, CLAIM_CHECK, '
            "COMPARE, OUT_OF_SCOPE. The value of extracted_slots must be an object "
            "using only these optional keys: company, comparison_company, metric, "
            "period, period_mode, value_scope, fs_div, topic_or_claim, user_claim, "
            "claim_operator. Company values are SAMSUNG_ELECTRONICS or SK_HYNIX. "
            "Metric values are revenue, operating_profit, net_income, total_assets. "
            "period_mode is LATEST_COMMON or LATEST_COMMON_INTERIM; use the latter "
            "when the user explicitly asks for the latest common quarter. "
            "value_scope is CUMULATIVE or POINT_IN_TIME and fs_div is CFS. "
            "For example, a direct Samsung Electronics 2025 consolidated operating "
            "profit question must produce intent DIRECT_FACT and extracted_slots "
            "containing company SAMSUNG_ELECTRONICS, metric operating_profit, period "
            "2025, value_scope CUMULATIVE, and fs_div CFS. "
            "Never use RouteDecision as a JSON key and never answer the question. "
            "Allowed intents: DIRECT_FACT, CLAIM_CHECK, COMPARE, OUT_OF_SCOPE. "
            "Use CLAIM_CHECK for segment or business-unit questions such as DS, "
            "because consolidated SQL facts do not represent a segment. Use CFS. "
            "Do not invent missing period or company slots."
        )
        user = json.dumps(
            {
                "message": message,
                "prior": prior.model_dump(mode="json") if prior else None,
            },
            ensure_ascii=False,
        )
        payload = await self._json_completion(system, user)
        if isinstance(payload, dict) and isinstance(payload.get("extracted_slots"), dict):
            slots_payload = dict(payload["extracted_slots"])
            period = slots_payload.get("period")
            if isinstance(period, int) and not isinstance(period, bool):
                slots_payload["period"] = str(period)
                payload = {**payload, "extracted_slots": slots_payload}
        try:
            decision = RouteDecision.model_validate(payload)
        except ValueError as exc:
            raise ProviderUnavailable("Elice classification violated the contract") from exc

        slots = decision.extracted_slots
        companies = _extract_company(message)
        intent = decision.intent
        if _is_research_lens(message) and not _OUT_OF_SCOPE.search(message):
            intent = Intent.CLAIM_CHECK
        elif _FILING_SEARCH.search(message) and not _OUT_OF_SCOPE.search(message):
            intent = Intent.CLAIM_CHECK
        elif not _OUT_OF_SCOPE.search(message) and (
            slots.comparison_company is not None
            or len(companies) >= 2
            or "따였다" in message
            or "비교" in message
        ):
            intent = Intent.COMPARE
        elif intent != Intent.OUT_OF_SCOPE:
            if (
                prior is not None
                and prior.workflow_status == WorkflowStatus.NEED_MORE_INFO
                and prior.intent is not None
                and not companies
                and len(message) < 80
            ):
                intent = prior.intent

        updates: dict[str, object] = {}
        if companies and slots.company is None:
            updates["company"] = companies[0]
        if len(companies) >= 2 and slots.comparison_company is None:
            updates["comparison_company"] = companies[1]
        if slots.metric is None:
            inferred_metric = _extract_metric(message)
            if inferred_metric is not None:
                updates["metric"] = inferred_metric
        inferred_period_mode = _extract_period_mode(message)
        if inferred_period_mode is not None:
            updates["period_mode"] = inferred_period_mode
            updates["period"] = None
        if intent == Intent.COMPARE:
            updates["user_claim"] = message
            if slots.claim_operator is None and "따였다" in message:
                updates["claim_operator"] = ClaimOperator.LT
        elif intent == Intent.CLAIM_CHECK:
            updates["topic_or_claim"] = message
            updates["user_claim"] = message

        return decision.model_copy(
            update={
                "intent": intent,
                "extracted_slots": slots.model_copy(update=updates),
            }
        )

    async def compose_answer(
        self,
        query,
        facts,
        verdict: str,
        limitations: Sequence[str],
    ) -> AnswerDraft:
        system = (
            "Write one concise Korean conclusion from only the supplied structured facts. "
            "Do not add numbers, causes, forecasts, recommendations, or uncited facts. "
            "Return JSON with exactly one field: conclusion."
        )
        user = json.dumps(
            {
                "query": query.model_dump(mode="json") if query else None,
                "facts": [fact.model_dump(mode="json") for fact in facts],
                "verdict": verdict,
                "limitations": list(limitations),
            },
            ensure_ascii=False,
        )
        payload = await self._json_completion(system, user)
        try:
            return AnswerDraft.model_validate(payload)
        except ValueError as exc:
            raise ProviderUnavailable("Elice final draft violated the contract") from exc


class OpenDartClient:
    """Thin OpenDART transport. It is not called on the chat request path."""

    BASE_URL = "https://opendart.fss.or.kr/api"

    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _get(self, path: str, params: dict[str, str]) -> httpx.Response:
        try:
            response = await self._client.get(
                f"{self.BASE_URL}/{path}",
                params={"crtfc_key": self._api_key, **params},
            )
            response.raise_for_status()
            return response
        except httpx.HTTPError as exc:
            # The underlying request URL contains `crtfc_key`. Do not retain
            # it as an exception cause that a framework could log.
            raise ProviderUnavailable(f"OpenDART request failed: {path}") from None

    async def list_filings(self, corp_code: str, begin: str, end: str) -> dict:
        response = await self._get(
            "list.json",
            {"corp_code": corp_code, "bgn_de": begin, "end_de": end, "page_count": "100"},
        )
        return response.json()

    async def fetch_financials(self, corp_code: str, year: int, report_code: str) -> dict:
        response = await self._get(
            "fnlttSinglAcntAll.json",
            {
                "corp_code": corp_code,
                "bsns_year": str(year),
                "reprt_code": report_code,
                "fs_div": "CFS",
            },
        )
        return response.json()

    async def fetch_document(self, receipt_no: str) -> bytes:
        response = await self._get("document.xml", {"rcept_no": receipt_no})
        return response.content
