from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Sequence
import socket

import pytest

from samnik_backend.agents import EvidenceGate, MultiTurnScenarioAgent, RagFaqAgent
from samnik_backend.domain import (
    AnswerDraft,
    Company,
    ConversationRecord,
    Evidence,
    EvidenceHit,
    FinancialFact,
    FsDiv,
    Metric,
    RouteDecision,
    ValueScope,
)
from samnik_backend.providers import HashEmbeddingProvider, RuleBasedLanguageModel
from samnik_backend.repositories import (
    InMemoryConversationRepository,
    InMemoryEvidenceRepository,
    InMemoryFinancialRepository,
)
from samnik_backend.workflow import FactRoomWorkflow


AS_OF = datetime(2026, 7, 19, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def block_real_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("tests must not open real network connections")

    monkeypatch.setattr(socket, "create_connection", denied)

SAMSUNG_RECEIPT = "20260310002820"
HYNIX_RECEIPT = "20260317000635"

SAMSUNG_OPERATING_PROFIT = Decimal("43601051000000")
HYNIX_OPERATING_PROFIT = Decimal("47206319000000")

DS_SOURCE_TEXT = (
    "삼성전자 2025년 사업보고서 연결재무제표 주석 30. 부문별 보고. "
    "영업이익은 DS 부문 24,858,075백만원입니다. (단위: 백만원)"
)
DS_EXCERPT = "영업이익은 DS 부문 24,858,075백만원입니다. (단위: 백만원)"


def make_evidence(
    evidence_id: str,
    company: Company,
    receipt_no: str,
    *,
    excerpt: str | None = None,
    source_kind: str = "FINANCIAL_FACT",
) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        company=company,
        rcept_no=receipt_no,
        filing_title=f"{company.value} 2025 사업보고서",
        filed_at=date(2026, 3, 10 if company == Company.SAMSUNG_ELECTRONICS else 17),
        url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt_no}",
        excerpt=excerpt,
        source_kind=source_kind,
    )


def make_fact(
    company: Company,
    value: Decimal,
    evidence_id: str,
    *,
    period: str = "2025",
    value_scope: ValueScope = ValueScope.CUMULATIVE,
    fs_div: FsDiv = FsDiv.CFS,
    currency: str = "KRW",
    unit: str = "KRW",
) -> FinancialFact:
    return FinancialFact(
        company=company,
        metric=Metric.OPERATING_PROFIT,
        value=value,
        currency=currency,
        unit=unit,
        period=period,
        value_scope=value_scope,
        fs_div=fs_div,
        evidence_id=evidence_id,
    )


@pytest.fixture
def samsung_evidence() -> Evidence:
    return make_evidence(
        "fact:samsung:operating-profit:2025:cfs",
        Company.SAMSUNG_ELECTRONICS,
        SAMSUNG_RECEIPT,
    )


@pytest.fixture
def hynix_evidence() -> Evidence:
    return make_evidence(
        "fact:hynix:operating-profit:2025:cfs",
        Company.SK_HYNIX,
        HYNIX_RECEIPT,
    )


@pytest.fixture
def samsung_fact(samsung_evidence: Evidence) -> FinancialFact:
    return make_fact(
        Company.SAMSUNG_ELECTRONICS,
        SAMSUNG_OPERATING_PROFIT,
        samsung_evidence.evidence_id,
    )


@pytest.fixture
def hynix_fact(hynix_evidence: Evidence) -> FinancialFact:
    return make_fact(
        Company.SK_HYNIX,
        HYNIX_OPERATING_PROFIT,
        hynix_evidence.evidence_id,
    )


class ScriptedLanguageModel:
    provider_name = "elice_mlapi"

    def __init__(self, decisions: dict[str, RouteDecision]) -> None:
        self.decisions = decisions
        self.classify_calls: list[tuple[str, ConversationRecord | None]] = []
        self.compose_calls: list[dict] = []

    async def classify(
        self,
        message: str,
        prior: ConversationRecord | None,
    ) -> RouteDecision:
        self.classify_calls.append((message, prior))
        return self.decisions[message]

    async def compose_answer(
        self,
        query,
        facts: Sequence[FinancialFact],
        verdict: str,
        limitations: Sequence[str],
    ) -> AnswerDraft:
        self.compose_calls.append(
            {
                "query": query,
                "facts": list(facts),
                "verdict": str(verdict),
                "limitations": list(limitations),
            }
        )
        if str(verdict) == "CONTRADICTED":
            conclusion = "공시 근거상 해당 주장은 사실과 다릅니다."
        elif len(facts) == 2:
            conclusion = "동일 기준으로 SK하이닉스의 영업이익이 더 큽니다."
        else:
            conclusion = "공식 공시에서 해당 사실을 확인했습니다."
        return AnswerDraft(conclusion=conclusion)


def build_workflow(
    *,
    language_model,
    facts: Sequence[FinancialFact] = (),
    evidence: Sequence[Evidence] = (),
    search_index=None,
    corpus_complete: bool = True,
    conversations=None,
    scenario_agent=None,
    rag_agent=None,
    evidence_gate=None,
) -> FactRoomWorkflow:
    evidence_repository = InMemoryEvidenceRepository(
        evidence,
        search_index=search_index,
        corpus_complete=corpus_complete,
        corpus_scope="2025 fixed OpenDART fixture",
        searched_until=AS_OF,
    )
    return FactRoomWorkflow(
        language_model=language_model,
        conversations=conversations or InMemoryConversationRepository(),
        scenario_agent=scenario_agent or MultiTurnScenarioAgent(),
        rag_agent=rag_agent
        or RagFaqAgent(
            financials=InMemoryFinancialRepository(facts),
            evidence=evidence_repository,
            embeddings=HashEmbeddingProvider(),
        ),
        evidence_gate=evidence_gate or EvidenceGate(),
    )


@pytest.fixture
def evidence_items(samsung_evidence, hynix_evidence) -> list[Evidence]:
    return [
        samsung_evidence,
        make_evidence(
            "test:samsung:2026q1:op",
            Company.SAMSUNG_ELECTRONICS,
            "20260510000001",
        ),
        make_evidence(
            "test:sk:2026q1:op",
            Company.SK_HYNIX,
            "20260515000001",
        ),
        make_evidence(
            "test:samsung:business:chunk",
            Company.SAMSUNG_ELECTRONICS,
            SAMSUNG_RECEIPT,
            excerpt="[TEST FIXTURE] 삼성전자의 주요 사업은 DX와 DS 부문으로 구성된다.",
            source_kind="FILING_CHUNK",
        ),
    ]


@pytest.fixture
def financial_facts(samsung_fact, evidence_items) -> list[FinancialFact]:
    return [
        samsung_fact,
        make_fact(
            Company.SAMSUNG_ELECTRONICS,
            Decimal("6000000000000"),
            evidence_items[1].evidence_id,
            period="2026Q1",
        ),
        make_fact(
            Company.SK_HYNIX,
            Decimal("8000000000000"),
            evidence_items[2].evidence_id,
            period="2026Q1",
        ),
    ]


@pytest.fixture
def workflow(financial_facts, evidence_items) -> FactRoomWorkflow:
    positive_query = "삼성전자의 주요 사업은?"
    evidence_repository = InMemoryEvidenceRepository(
        evidence_items,
        search_index={
            positive_query: [EvidenceHit(evidence=evidence_items[-1], score=0.95)]
        },
        corpus_complete=True,
        corpus_scope="TEST FIXTURE: 2025 annual and 2026 Q1",
        searched_until=AS_OF,
    )
    return FactRoomWorkflow(
        language_model=RuleBasedLanguageModel(),
        conversations=InMemoryConversationRepository(),
        scenario_agent=MultiTurnScenarioAgent(),
        rag_agent=RagFaqAgent(
            financials=InMemoryFinancialRepository(financial_facts),
            evidence=evidence_repository,
            embeddings=HashEmbeddingProvider(),
        ),
        evidence_gate=EvidenceGate(),
    )
