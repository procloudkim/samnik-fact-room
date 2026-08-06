from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from samnik_backend.agents import (
    EvidenceGate,
    MultiTurnScenarioAgent,
    RagFaqAgent,
    _narrative_hit_matches_topic,
    _narrative_retrieval_text,
    build_normalized_query,
)
from samnik_backend.company_context import (
    company_context_coverage,
    company_context_terms,
    load_company_context_dictionary,
)
from samnik_backend.domain import (
    ClaimOperator,
    Company,
    Evidence,
    EvidenceStatus,
    FinancialFact,
    FsDiv,
    Intent,
    Metric,
    NormalizedQuery,
    PeriodMode,
    RagResult,
    Slots,
    ValueScope,
    Verdict,
    WorkflowStatus,
)
from samnik_backend.errors import EvidenceValidationError
from samnik_backend.providers import HashEmbeddingProvider, RuleBasedLanguageModel
from samnik_backend.repositories import InMemoryEvidenceRepository, InMemoryFinancialRepository


def test_ds_profit_claim_expands_only_the_embedding_query() -> None:
    claim = "삼성전자 DS부문이 2025년에도 적자였다는 글이 사실이야?"

    expanded = _narrative_retrieval_text(claim)

    assert expanded.startswith(claim)
    assert "부문별 요약 재무 현황" in expanded
    assert "연구개발 조직" in _narrative_retrieval_text("연구개발 활동을 알려줘")


def test_company_context_dictionary_covers_both_companies_and_all_annual_years() -> None:
    payload = load_company_context_dictionary()

    assert payload["methodology"] == "GOVERNED_TERM_OCCURRENCE_V1"
    assert set(payload["companies"]) == {company.value for company in Company}
    assert company_context_coverage(Company.SAMSUNG_ELECTRONICS) == tuple(
        range(2015, 2026)
    )
    assert company_context_coverage(Company.SK_HYNIX) == tuple(range(2015, 2026))
    for details in payload["companies"].values():
        assert details["missing_years"] == []
        assert set(details["categories"]) == set(payload["taxonomy"])
        assert len(details["source_fingerprint_sha256"]) == 64


def test_working_capital_query_uses_bounded_corpus_observed_company_terms() -> None:
    samsung_terms = company_context_terms(
        Company.SAMSUNG_ELECTRONICS,
        "WORKING_CAPITAL",
    )
    sk_terms = company_context_terms(Company.SK_HYNIX, "WORKING_CAPITAL")

    assert 1 <= len(samsung_terms) <= 12
    assert 1 <= len(sk_terms) <= 12
    assert "재고자산" in samsung_terms
    expanded = _narrative_retrieval_text(
        "재고자산과 매출채권의 위험 신호를 점검해줘",
        Company.SAMSUNG_ELECTRONICS,
    )
    assert expanded.startswith("재고자산과 매출채권")
    assert all(term in expanded for term in samsung_terms)


def test_narrative_topic_filter_rejects_semantically_similar_wrong_section() -> None:
    research = Evidence(
        evidence_id="chunk:research",
        company=Company.SAMSUNG_ELECTRONICS,
        rcept_no="20260310002820",
        filing_title="사업보고서",
        filed_at=datetime(2026, 3, 10, tzinfo=UTC).date(),
        url="https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260310002820",
        excerpt="연구개발비용과 주요 연구개발 실적",
        section_path="사업의 내용 > 연구개발활동",
        source_kind="FILING_CHUNK",
        evidence_status=EvidenceStatus.OFFICIAL_API_VERIFIED,
    )
    audit = research.model_copy(
        update={
            "evidence_id": "chunk:audit",
            "excerpt": "내부회계관리제도 감사의견",
            "section_path": "감사 또는 검토의견",
        }
    )

    assert _narrative_hit_matches_topic("연구개발 근거를 찾아줘", research)
    assert not _narrative_hit_matches_topic("연구개발 근거를 찾아줘", audit)


def test_scenario_asks_only_for_missing_period_mode() -> None:
    agent = MultiTurnScenarioAgent()
    slots = Slots(
        company=Company.SAMSUNG_ELECTRONICS,
        comparison_company=Company.SK_HYNIX,
        metric=Metric.OPERATING_PROFIT,
        user_claim="삼전 영업이익이 하닉에 따였다던데?",
        claim_operator=ClaimOperator.LT,
    )

    first = agent.run(intent=Intent.COMPARE, slots=slots)
    assert first.workflow_status == WorkflowStatus.NEED_MORE_INFO
    assert first.missing_slots == ["period_mode"]
    assert first.next_question == "가장 최근에 두 회사가 모두 공시한 동일 기간 기준으로 비교할까요?"

    second = agent.run(
        intent=Intent.COMPARE,
        slots=first.collected_slots.merged(Slots(period_mode=PeriodMode.LATEST_COMMON)),
    )
    assert second.workflow_status == WorkflowStatus.READY_FOR_RAG
    assert second.missing_slots == []
    assert second.normalized_query is not None
    assert second.normalized_query.companies == [
        Company.SAMSUNG_ELECTRONICS,
        Company.SK_HYNIX,
    ]


@pytest.mark.parametrize(
    ("report_code", "expected_period"),
    [
        ("11011", "2023"),
        ("11013", "2023Q1"),
        ("11012", "2023H1"),
        ("11014", "2023Q3"),
    ],
)
def test_normalized_query_honors_report_context(report_code: str, expected_period: str) -> None:
    query = build_normalized_query(
        Intent.DIRECT_FACT,
        Slots(
            company=Company.SK_HYNIX,
            metric=Metric.OPERATING_PROFIT,
            period="2023",
            report_codes=[report_code],
            fs_div=FsDiv.OFS,
        ),
    )

    assert query.period == expected_period
    assert query.fs_div == FsDiv.OFS


@pytest.mark.asyncio
async def test_latest_common_interim_excludes_same_year_annual(evidence_items) -> None:
    facts = [
        FinancialFact(
            company=company,
            metric=Metric.OPERATING_PROFIT,
            value=Decimal(value),
            period=period,
            value_scope=ValueScope.CUMULATIVE,
            evidence_id=evidence.evidence_id,
        )
        for company, value, period, evidence in [
            (Company.SAMSUNG_ELECTRONICS, "10", "2026", evidence_items[0]),
            (Company.SK_HYNIX, "20", "2026", evidence_items[1]),
            (Company.SAMSUNG_ELECTRONICS, "6", "2026Q1", evidence_items[2]),
            (Company.SK_HYNIX, "8", "2026Q1", evidence_items[3]),
        ]
    ]
    repository = InMemoryFinancialRepository(facts)

    pair = await repository.get_latest_common_facts(
        companies=(Company.SAMSUNG_ELECTRONICS, Company.SK_HYNIX),
        metric=Metric.OPERATING_PROFIT,
        value_scope=ValueScope.CUMULATIVE,
        fs_div=FsDiv.CFS,
        as_of=datetime.now(UTC),
        interim_only=True,
    )

    assert pair is not None
    assert [fact.period for fact in pair] == ["2026Q1", "2026Q1"]


def test_evidence_gate_rejects_fact_without_evidence() -> None:
    query = NormalizedQuery(
        intent=Intent.DIRECT_FACT,
        companies=[Company.SAMSUNG_ELECTRONICS],
        metric=Metric.OPERATING_PROFIT,
        period="2025",
        value_scope=ValueScope.CUMULATIVE,
        as_of=datetime.now(UTC),
    )
    result = RagResult(
        workflow_status=WorkflowStatus.ANSWER_READY,
        verdict=Verdict.CONFIRMED,
        normalized_query=query,
        facts=[
            FinancialFact(
                company=Company.SAMSUNG_ELECTRONICS,
                metric=Metric.OPERATING_PROFIT,
                value=Decimal("1"),
                period="2025",
                value_scope=ValueScope.CUMULATIVE,
                evidence_id="missing",
            )
        ],
    )
    with pytest.raises(EvidenceValidationError):
        EvidenceGate().validate_rag(result)


@pytest.mark.asyncio
async def test_rag_refuses_mismatched_comparison_basis(evidence_items) -> None:
    facts = [
        FinancialFact(
            company=Company.SAMSUNG_ELECTRONICS,
            metric=Metric.OPERATING_PROFIT,
            value=Decimal("1"),
            currency="KRW",
            unit="KRW",
            period="2026Q1",
            value_scope=ValueScope.CUMULATIVE,
            evidence_id=evidence_items[1].evidence_id,
        ),
        FinancialFact(
            company=Company.SK_HYNIX,
            metric=Metric.OPERATING_PROFIT,
            value=Decimal("2"),
            currency="USD",
            unit="USD",
            period="2026Q1",
            value_scope=ValueScope.CUMULATIVE,
            evidence_id=evidence_items[2].evidence_id,
        ),
    ]
    rag = RagFaqAgent(
        financials=InMemoryFinancialRepository(facts),
        evidence=InMemoryEvidenceRepository(evidence_items),
        embeddings=HashEmbeddingProvider(),
    )
    result = await rag.run(
        NormalizedQuery(
            intent=Intent.COMPARE,
            companies=[Company.SAMSUNG_ELECTRONICS, Company.SK_HYNIX],
            metric=Metric.OPERATING_PROFIT,
            period_mode=PeriodMode.LATEST_COMMON,
            value_scope=ValueScope.CUMULATIVE,
            claim_operator=ClaimOperator.LT,
            as_of=datetime.now(UTC),
        )
    )
    assert result.workflow_status == WorkflowStatus.UNAVAILABLE
    assert result.verdict == Verdict.INSUFFICIENT_INFO
    assert result.comparison_basis is not None
    assert result.comparison_basis.same_currency is False


@pytest.mark.asyncio
async def test_synthetic_unverified_evidence_can_never_confirm() -> None:
    synthetic = Evidence(
        evidence_id="fixture:fact:samsung:2025:op",
        company=Company.SAMSUNG_ELECTRONICS,
        rcept_no="20260310002820",
        filing_title="synthetic fixture",
        filed_at=datetime(2026, 3, 10, tzinfo=UTC).date(),
        url="https://example.test/synthetic",
        source_kind="FINANCIAL_FACT",
        evidence_status=EvidenceStatus.SYNTHETIC_UNVERIFIED,
    )
    fact = FinancialFact(
        company=Company.SAMSUNG_ELECTRONICS,
        metric=Metric.OPERATING_PROFIT,
        value=Decimal("1"),
        period="2025",
        value_scope=ValueScope.CUMULATIVE,
        evidence_id=synthetic.evidence_id,
    )
    rag = RagFaqAgent(
        financials=InMemoryFinancialRepository([fact]),
        evidence=InMemoryEvidenceRepository([synthetic]),
        embeddings=HashEmbeddingProvider(),
    )
    result = await rag.run(
        NormalizedQuery(
            intent=Intent.DIRECT_FACT,
            companies=[Company.SAMSUNG_ELECTRONICS],
            metric=Metric.OPERATING_PROFIT,
            period="2025",
            value_scope=ValueScope.CUMULATIVE,
            as_of=datetime.now(UTC),
        )
    )
    assert result.verdict == Verdict.INSUFFICIENT_INFO
    assert result.workflow_status == WorkflowStatus.UNAVAILABLE
    assert result.facts == []


@pytest.mark.asyncio
async def test_rule_classifier_routes_segment_metric_question_to_narrative_rag() -> None:
    decision = await RuleBasedLanguageModel().classify(
        "2025년 삼성전자 DS 부문 영업이익은 얼마야?",
        None,
    )

    assert decision.intent == Intent.CLAIM_CHECK


@pytest.mark.asyncio
async def test_rule_classifier_understands_public_two_company_quarter_example() -> None:
    decision = await RuleBasedLanguageModel().classify(
        "두 회사의 최근 공통 분기 영업이익을 비교해줘",
        None,
    )

    assert decision.intent == Intent.COMPARE
    assert decision.extracted_slots.company == Company.SAMSUNG_ELECTRONICS
    assert decision.extracted_slots.comparison_company == Company.SK_HYNIX
    assert decision.extracted_slots.period_mode == PeriodMode.LATEST_COMMON_INTERIM


def test_narrative_similarity_alone_cannot_confirm_an_arbitrary_assertion() -> None:
    evidence = Evidence(
        evidence_id="chunk:official:topic-only",
        company=Company.SAMSUNG_ELECTRONICS,
        rcept_no="20260310002820",
        filing_title="사업보고서",
        filed_at=datetime(2026, 3, 10, tzinfo=UTC).date(),
        url="https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260310002820",
        excerpt="회사는 국내 생산시설의 운영 현황을 공시하고 있습니다.",
        source_kind="FILING_CHUNK",
        evidence_status=EvidenceStatus.OFFICIAL_API_VERIFIED,
    )

    verdict = RagFaqAgent._narrative_verdict(
        "삼성전자가 화성에 새 공장을 세웠다는 말이 사실이야?",
        [evidence],
    )

    assert verdict == Verdict.PARTIALLY_CONFIRMED


def test_narrative_evidence_search_request_is_confirmed_when_evidence_exists() -> None:
    evidence = Evidence(
        evidence_id="chunk:official:research",
        company=Company.SAMSUNG_ELECTRONICS,
        business_year=2025,
        rcept_no="20260310002820",
        filing_title="사업보고서",
        filed_at=datetime(2026, 3, 10, tzinfo=UTC).date(),
        url="https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260310002820",
        excerpt="2025년 주요 연구개발 실적과 연구개발 조직을 공시합니다.",
        section_path="사업의 내용 > 주요계약 및 연구개발활동",
        source_kind="FILING_CHUNK",
        evidence_status=EvidenceStatus.OFFICIAL_API_VERIFIED,
    )

    verdict = RagFaqAgent._narrative_verdict(
        "2025년 삼성전자 사업보고서에서 연구개발 활동 관련 근거를 찾아줘.",
        [evidence],
    )

    assert verdict == Verdict.CONFIRMED


def test_ds_matrix_table_uses_ds_column_not_first_dx_amount() -> None:
    evidence = Evidence(
        evidence_id="chunk:official:segment-matrix",
        company=Company.SAMSUNG_ELECTRONICS,
        business_year=2025,
        rcept_no="20260310002820",
        filing_title="사업보고서",
        filed_at=datetime(2026, 3, 10, tzinfo=UTC).date(),
        url="https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260310002820",
        excerpt="구분 | DX | DS\n영업이익 | 10,000 | (5,000)",
        section_path="연결재무제표 주석 > 부문별 보고",
        source_kind="FILING_CHUNK",
        evidence_status=EvidenceStatus.OFFICIAL_API_VERIFIED,
    )

    verdict = RagFaqAgent._narrative_verdict(
        "삼성전자 DS 부문이 2025년에 적자였다는 말이 사실이야?",
        [evidence],
    )

    assert verdict == Verdict.CONFIRMED


def test_ds_mention_without_value_mapping_stays_non_terminal() -> None:
    evidence = Evidence(
        evidence_id="chunk:official:ambiguous-segment-matrix",
        company=Company.SAMSUNG_ELECTRONICS,
        business_year=2025,
        rcept_no="20260310002820",
        filing_title="사업보고서",
        filed_at=datetime(2026, 3, 10, tzinfo=UTC).date(),
        url="https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260310002820",
        excerpt="DS 관련 설명\n영업이익 10,000 (5,000)",
        section_path="연결재무제표 주석 > 부문별 보고",
        source_kind="FILING_CHUNK",
        evidence_status=EvidenceStatus.OFFICIAL_API_VERIFIED,
    )

    verdict = RagFaqAgent._narrative_verdict(
        "삼성전자 DS 부문이 2025년에 적자였다는 말이 사실이야?",
        [evidence],
    )

    assert verdict == Verdict.PARTIALLY_CONFIRMED
