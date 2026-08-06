from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from samnik_backend.agents import (
    MeceDisclosureAnalysisAgent,
    MultiTurnScenarioAgent,
    RagFaqAgent,
)
from samnik_backend.domain import (
    Actor,
    AnswerDraft,
    ChatTurnContext,
    ChatTurnRequest,
    ClaimOperator,
    Company,
    EventType,
    EvidenceHit,
    FsDiv,
    Intent,
    Metric,
    NormalizedQuery,
    PeriodMode,
    RagResult,
    Route,
    RouteDecision,
    Slots,
    ValueScope,
    Verdict,
    WorkflowStatus,
)
from samnik_backend.errors import ProviderUnavailable, RepositoryUnavailable
from samnik_backend.providers import HashEmbeddingProvider
from samnik_backend.postgres import PostgresEvidenceRepository, PostgresFinancialRepository
from samnik_backend.public_contract import to_public_events
from samnik_backend.repositories import (
    InMemoryEvidenceRepository,
    InMemoryFinancialRepository,
)
from samnik_backend.workflow import FactRoomWorkflow

from conftest import (
    AS_OF,
    DS_EXCERPT,
    DS_SOURCE_TEXT,
    SAMSUNG_OPERATING_PROFIT,
    HYNIX_OPERATING_PROFIT,
    ScriptedLanguageModel,
    build_workflow,
    make_evidence,
    make_fact,
)


def _request(
    message: str,
    *,
    conversation_id: str = "conversation-test",
    context: ChatTurnContext | None = None,
) -> ChatTurnRequest:
    return ChatTurnRequest(
        conversation_id=conversation_id,
        client_request_id=f"request-{abs(hash(message))}",
        message=message,
        context=context,
    )


def _trace(result) -> list[tuple[str, str, str | None, str | None]]:
    return [
        (
            event.producer.value,
            event.event_type.value,
            event.payload.get("agent"),
            str(event.payload.get("returned_to") or event.payload.get("returned_from"))
            if event.payload.get("returned_to") or event.payload.get("returned_from")
            else None,
        )
        for event in result.events
    ]


@pytest.mark.parametrize(
    ("metric", "metric_label"),
    [
        (Metric.TOTAL_LIABILITIES, "부채총계"),
        (Metric.TOTAL_EQUITY, "자본총계"),
        (Metric.OPERATING_CASH_FLOW, "영업활동현금흐름"),
    ],
)
def test_grounded_conclusion_supports_every_prompt_card_metric(
    metric: Metric,
    metric_label: str,
    samsung_evidence,
) -> None:
    fact = make_fact(
        Company.SAMSUNG_ELECTRONICS,
        Decimal("123000000000"),
        samsung_evidence.evidence_id,
        period="2026Q1",
        value_scope=(
            ValueScope.POINT_IN_TIME
            if metric in {Metric.TOTAL_LIABILITIES, Metric.TOTAL_EQUITY}
            else ValueScope.CUMULATIVE
        ),
    ).model_copy(update={"metric": metric})
    result = RagResult(
        workflow_status=WorkflowStatus.ANSWER_READY,
        verdict=Verdict.CONFIRMED,
        normalized_query=NormalizedQuery(
            intent=Intent.DIRECT_FACT,
            companies=[Company.SAMSUNG_ELECTRONICS],
            metric=metric,
            period="2026Q1",
            value_scope=fact.value_scope,
            fs_div=FsDiv.CFS,
            as_of=AS_OF,
        ),
        facts=[fact],
        evidence=[samsung_evidence],
    )

    conclusion = FactRoomWorkflow._grounded_conclusion(result)

    assert metric_label in conclusion
    assert "1,230억 원" in conclusion


def test_grounded_conclusion_distinguishes_review_from_missing_evidence(
    samsung_evidence,
) -> None:
    result = RagResult(
        workflow_status=WorkflowStatus.UNAVAILABLE,
        verdict=Verdict.INSUFFICIENT_INFO,
        normalized_query=NormalizedQuery(
            intent=Intent.CLAIM_CHECK,
            companies=[Company.SAMSUNG_ELECTRONICS],
            topic_or_claim="삼전의 재무건전성은?",
            fs_div=FsDiv.CFS,
            as_of=AS_OF,
        ),
        evidence=[samsung_evidence],
    )

    conclusion = FactRoomWorkflow._grounded_conclusion(result)

    assert "관련 공식 공시 근거 1건을 확인했습니다" in conclusion
    assert "단일 지표로 단정할 수 없습니다" in conclusion
    assert "아직 확인할 근거가 부족" not in conclusion
    assert "찾았습니다" not in conclusion


def test_working_capital_context_gate_removes_keyword_only_false_positives() -> None:
    relevant_inventory = make_evidence(
        "working:inventory",
        Company.SAMSUNG_ELECTRONICS,
        "20260515002181",
        excerpt="재고자산에 저가법을 적용하고 평가충당금 내역을 공시했습니다.",
        source_kind="FILING_CHUNK",
    )
    relevant_receivables = make_evidence(
        "working:receivables",
        Company.SAMSUNG_ELECTRONICS,
        "20260515002181",
        excerpt="매출채권의 기대신용손실과 대손충당금을 검토했습니다.",
        source_kind="FILING_CHUNK",
    )
    irrelevant = [
        make_evidence(
            "working:guarantee",
            Company.SAMSUNG_ELECTRONICS,
            "20260515002181",
            excerpt="채무보증 수수료를 수취하고 이사회에서 지급보증을 승인합니다.",
            source_kind="FILING_CHUNK",
        ),
        make_evidence(
            "working:fx",
            Company.SAMSUNG_ELECTRONICS,
            "20260515002181",
            excerpt="환율 위험회피 목적으로 통화선도 거래를 체결했습니다.",
            source_kind="FILING_CHUNK",
        ),
    ]
    query = NormalizedQuery(
        intent=Intent.CLAIM_CHECK,
        companies=[Company.SAMSUNG_ELECTRONICS],
        period="2026Q1",
        topic_or_claim="재고자산과 매출채권의 운전자본 리스크를 점검해줘",
        fs_div=FsDiv.CFS,
        as_of=AS_OF,
    )
    result = RagResult(
        workflow_status=WorkflowStatus.ANSWER_READY,
        verdict=Verdict.CONFIRMED,
        normalized_query=query,
        evidence=[relevant_inventory, *irrelevant, relevant_receivables],
    )

    analyzed = MeceDisclosureAnalysisAgent().run(result)

    assert analyzed.analysis_report is not None
    assert analyzed.analysis_report.source_count == 2
    assert [finding.category_key for finding in analyzed.analysis_report.findings] == [
        "INVENTORY",
        "RECEIVABLES",
    ]
    assert {item.evidence_id for item in analyzed.evidence} == {
        relevant_inventory.evidence_id,
        relevant_receivables.evidence_id,
    }
    assert all("지급보증" not in finding.summary for finding in analyzed.analysis_report.findings)
    assert all("통화선도" not in finding.summary for finding in analyzed.analysis_report.findings)


def test_explicit_key_audit_question_excludes_internal_control_review_report() -> None:
    key_audit = make_evidence(
        "audit:key",
        Company.SAMSUNG_ELECTRONICS,
        "20260310002820",
        excerpt="핵심감사사항으로 재고자산 평가를 선정하여 감사절차를 수행했습니다.",
        source_kind="FILING_CHUNK",
    )
    internal_control = make_evidence(
        "audit:control",
        Company.SAMSUNG_ELECTRONICS,
        "20160330003536",
        excerpt=(
            "내부회계관리제도 검토보고서는 재무제표 감사업무와 함께 수행한 "
            "검토 결과를 첨부하는 것입니다."
        ),
        source_kind="FILING_CHUNK",
    )
    query = NormalizedQuery(
        intent=Intent.CLAIM_CHECK,
        companies=[Company.SAMSUNG_ELECTRONICS],
        period="2015",
        topic_or_claim="삼성전자 2015년 핵심감사사항을 점검해줘",
        fs_div=FsDiv.CFS,
        as_of=AS_OF,
    )
    result = RagResult(
        workflow_status=WorkflowStatus.ANSWER_READY,
        verdict=Verdict.CONFIRMED,
        normalized_query=query,
        evidence=[internal_control, key_audit],
    )

    analyzed = MeceDisclosureAnalysisAgent().run(result)

    assert analyzed.analysis_report is not None
    assert [finding.category_key for finding in analyzed.analysis_report.findings] == ["KAM"]
    assert [item.evidence_id for item in analyzed.evidence] == [key_audit.evidence_id]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "lens_key", "finding_label"),
    [
        ("삼성전자 본업 수익성을 진단해줘", "OPERATING_PROFITABILITY", "영업이익률"),
        ("삼성전자 최종 수익성을 진단해줘", "NET_PROFITABILITY", "순이익률"),
        ("삼성전자 현금 전환력을 진단해줘", "CASH_CONVERSION", "영업이익의 현금 전환율"),
        ("삼성전자 재무건전성을 진단해줘", "FINANCIAL_RESILIENCE", "부채비율"),
    ],
)
async def test_core_financial_lenses_use_dedicated_calculation_agent(
    message: str,
    lens_key: str,
    finding_label: str,
) -> None:
    metric_values = {
        Metric.REVENUE: Decimal("1000"),
        Metric.OPERATING_PROFIT: Decimal("200"),
        Metric.NET_INCOME: Decimal("100"),
        Metric.TOTAL_ASSETS: Decimal("1000"),
        Metric.TOTAL_LIABILITIES: Decimal("400"),
        Metric.TOTAL_EQUITY: Decimal("600"),
        Metric.OPERATING_CASH_FLOW: Decimal("150"),
    }
    evidence = [
        make_evidence(
            f"diagnosis:{metric.value}",
            Company.SAMSUNG_ELECTRONICS,
            f"2026031000{index:04d}",
        )
        for index, metric in enumerate(metric_values, start=1)
    ]
    facts = [
        make_fact(
            Company.SAMSUNG_ELECTRONICS,
            value,
            f"diagnosis:{metric.value}",
            period="2026Q1",
            value_scope=(
                ValueScope.POINT_IN_TIME
                if metric in {Metric.TOTAL_ASSETS, Metric.TOTAL_LIABILITIES, Metric.TOTAL_EQUITY}
                else ValueScope.CUMULATIVE
            ),
        ).model_copy(update={"metric": metric})
        for metric, value in metric_values.items()
    ]
    request = _request(
        message,
        conversation_id=f"diagnosis-{lens_key.lower()}",
        context=ChatTurnContext(
            companies=[Company.SAMSUNG_ELECTRONICS],
            business_year=2026,
            report_code="11013",
        ),
    )
    result = await build_workflow(
        language_model=_UnavailableLanguageModel(),
        facts=facts,
        evidence=evidence,
    ).run_turn(request)

    report = result.answer.analysis_report
    assert result.answer.workflow_status == WorkflowStatus.ANSWER_READY
    assert result.answer.verdict == Verdict.CONFIRMED
    assert report is not None
    assert report.lens_key == lens_key
    assert report.methodology == "FINANCIAL_RATIO_CALCULATION_V1"
    assert finding_label in [finding.category_label for finding in report.findings]
    started = next(
        event
        for event in result.events
        if event.event_type == EventType.AGENT_STARTED
        and event.producer == Actor.RAG_AGENT
    )
    assert started.payload["agent"] == f"FINANCIAL_{lens_key}_CALCULATOR"
    assert started.payload["retrieval_mode"] == "SQL"

    public_result = to_public_events(request, result)[-1].data["result"]
    assert public_result["title"].endswith("한눈에 보기")
    assert "좋고 나쁨을 단정" not in public_result["summary"]
    assert public_result["analysis_report"]["methodology"] == (
        "FINANCIAL_RATIO_CALCULATION_V1"
    )
    assert all(
        finding["plain_language"] and finding["next_check"]
        for finding in public_result["analysis_report"]["findings"]
    )
    assert "FINANCIAL_LITERACY_TUTOR" in public_result["trace_summary"]["agent_calls"]


@pytest.mark.asyncio
async def test_core_diagnosis_adds_same_quarter_trend_without_an_llm_calculation() -> None:
    periods = {
        "2024Q1": (Decimal("1000"), Decimal("100")),
        "2025Q1": (Decimal("1000"), Decimal("150")),
        "2026Q1": (Decimal("1000"), Decimal("200")),
    }
    facts = []
    evidence = []
    for period, (revenue, operating_profit) in periods.items():
        for metric, value in (
            (Metric.REVENUE, revenue),
            (Metric.OPERATING_PROFIT, operating_profit),
        ):
            evidence_id = f"trend:{period}:{metric.value}"
            facts.append(
                make_fact(
                    Company.SAMSUNG_ELECTRONICS,
                    value,
                    evidence_id,
                    period=period,
                ).model_copy(update={"metric": metric})
            )
            evidence.append(
                make_evidence(
                    evidence_id,
                    Company.SAMSUNG_ELECTRONICS,
                    f"{period[:4]}0310002820",
                )
            )

    request = _request(
        "2026년 삼성전자 1분기 본업 수익성을 진단해줘",
        conversation_id="diagnosis-three-year-trend",
        context=ChatTurnContext(
            companies=[Company.SAMSUNG_ELECTRONICS],
            business_year=2026,
            report_code="11013",
        ),
    )
    result = await build_workflow(
        language_model=_UnavailableLanguageModel(),
        facts=facts,
        evidence=evidence,
    ).run_turn(request)

    report = result.answer.analysis_report
    assert report is not None
    trend = next(
        finding for finding in report.findings if finding.category_key == "THREE_YEAR_TREND"
    )
    assert "2024Q1 10.0% → 2025Q1 15.0% → 2026Q1 20.0%" in trend.summary
    assert "10.0%p 상승" in trend.summary
    assert trend.plain_language
    assert trend.next_check


@pytest.mark.asyncio
async def test_a_direct_2025_samsung_operating_profit_is_evidence_backed(
    samsung_fact,
    samsung_evidence,
) -> None:
    message = "2025년 삼성전자 연결 기준 영업이익은 얼마야?"
    model = ScriptedLanguageModel(
        {
            message: RouteDecision(
                intent=Intent.DIRECT_FACT,
                extracted_slots=Slots(
                    company=Company.SAMSUNG_ELECTRONICS,
                    metric=Metric.OPERATING_PROFIT,
                    period="2025",
                    value_scope=ValueScope.CUMULATIVE,
                    fs_div=FsDiv.CFS,
                    as_of=AS_OF,
                ),
            )
        }
    )
    workflow = build_workflow(
        language_model=model,
        facts=[samsung_fact],
        evidence=[samsung_evidence],
    )

    result = await workflow.run_turn(_request(message))

    assert result.answer.actor == Actor.SUPERVISOR
    assert result.answer.route == Route.RAG
    assert result.answer.workflow_status == WorkflowStatus.ANSWER_READY
    assert result.answer.verdict == Verdict.CONFIRMED
    routed = next(
        event for event in result.events if event.event_type == EventType.SUPERVISOR_ROUTED
    )
    assert routed.payload["model_usage"]["called"] is False
    public_result = to_public_events(
        _request(message, conversation_id="forbidden-direct"), result
    )[-1].data["result"]
    assert public_result["trace_summary"]["model_usage"]["called"] is False
    assert result.answer.facts[0].value == SAMSUNG_OPERATING_PROFIT
    assert result.answer.facts[0].period == "2025"


class _ScenarioAPostgresConnection:
    def __init__(self) -> None:
        self.fetchrow_calls: list[tuple[str, tuple]] = []
        self.fetch_calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql: str, *args):
        self.fetchrow_calls.append((sql, args))
        return {
            "corp_code": "00126380",
            "metric_key": "operating_profit",
            "amount_numeric": "43601051000000",
            "currency": "KRW",
            "business_year": 2025,
            "reprt_code": "11011",
            "fs_div": "CFS",
            "row_hash": "fact:samsung:operating-profit:2025:cfs",
        }

    async def fetch(self, sql: str, *args):
        self.fetch_calls.append((sql, args))
        return [
            {
                "evidence_id": "fact:samsung:operating-profit:2025:cfs",
                "corp_code": "00126380",
                "business_year": 2025,
                "rcept_no": "20260310002820",
                "report_name": "삼성전자 2025 사업보고서",
                "filing_date": date(2026, 3, 10),
                "source_url": (
                    "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260310002820"
                ),
                "excerpt": None,
                "section_path": None,
                "source_kind": "FINANCIAL_FACT",
            }
        ]


class _ScenarioAAcquire:
    def __init__(self, connection: _ScenarioAPostgresConnection) -> None:
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _ScenarioAPool:
    def __init__(self, connection: _ScenarioAPostgresConnection) -> None:
        self.connection = connection

    def acquire(self):
        return _ScenarioAAcquire(self.connection)


@pytest.mark.asyncio
async def test_a_vertical_slice_uses_parameterized_postgres_sql_and_returns_to_supervisor() -> None:
    message = "2025년 삼성전자 연결 기준 영업이익은 얼마야?"
    model = ScriptedLanguageModel(
        {
            message: RouteDecision(
                intent=Intent.DIRECT_FACT,
                extracted_slots=Slots(
                    company=Company.SAMSUNG_ELECTRONICS,
                    metric=Metric.OPERATING_PROFIT,
                    period="2025",
                    value_scope=ValueScope.CUMULATIVE,
                    fs_div=FsDiv.CFS,
                ),
            )
        }
    )
    connection = _ScenarioAPostgresConnection()
    pool = _ScenarioAPool(connection)
    rag = RagFaqAgent(
        financials=PostgresFinancialRepository(pool),
        evidence=PostgresEvidenceRepository(pool),
        embeddings=HashEmbeddingProvider(),
    )
    workflow = build_workflow(language_model=model, rag_agent=rag)

    result = await workflow.run_turn(
        _request(message, conversation_id="scenario-a-postgres")
    )

    assert result.answer.actor == Actor.SUPERVISOR
    assert result.answer.workflow_status == WorkflowStatus.ANSWER_READY
    assert result.answer.verdict == Verdict.CONFIRMED
    assert result.answer.facts[0].value == SAMSUNG_OPERATING_PROFIT
    assert result.answer.evidence[0].rcept_no == "20260310002820"
    financial_sql, financial_args = connection.fetchrow_calls[0]
    assert "FROM financial_statement_line" in financial_sql
    assert "financial_statement_line_id" in financial_sql
    assert "OFFICIAL_API_VERIFIED" in financial_sql
    assert all(f"${index}" in financial_sql for index in (1, 2, 3, 4, 5, 6, 7))
    assert "$8" not in financial_sql
    assert financial_args[:5] == (
        "00126380",
        "operating_profit",
        2025,
        "11011",
        "CFS",
    )
    evidence_sql, evidence_args = connection.fetch_calls[0]
    assert "FROM financial_statement_line" in evidence_sql
    assert evidence_args == (["fact:samsung:operating-profit:2025:cfs"],)
    assert _trace(result) == [
        ("SYSTEM", "turn.started", None, None),
        ("SUPERVISOR", "supervisor.route.selected", None, None),
        ("RAG_AGENT", "agent.started", "RAG_FAQ", None),
        ("RAG_AGENT", "agent.completed", "RAG_FAQ", "SUPERVISOR"),
        ("EVIDENCE_GATE", "evidence.checked", None, None),
        ("SUPERVISOR", "supervisor.answer.completed", None, None),
    ]


@pytest.mark.asyncio
async def test_supervisor_never_publishes_hallucinated_model_draft(
    samsung_fact,
    samsung_evidence,
) -> None:
    message = "2025년 삼성전자 연결 기준 영업이익은 얼마야?"

    class HallucinatingDraftModel(ScriptedLanguageModel):
        async def compose_answer(self, query, facts, verdict, limitations) -> AnswerDraft:
            self.compose_calls.append({"query": query})
            return AnswerDraft(
                conclusion="근거 없는 전망: 내년 영업이익은 999조 원이고 주가는 두 배가 됩니다."
            )

    model = HallucinatingDraftModel(
        {
            message: RouteDecision(
                intent=Intent.DIRECT_FACT,
                extracted_slots=Slots(
                    company=Company.SAMSUNG_ELECTRONICS,
                    metric=Metric.OPERATING_PROFIT,
                    period="2025",
                    value_scope=ValueScope.CUMULATIVE,
                    fs_div=FsDiv.CFS,
                    as_of=AS_OF,
                ),
            )
        }
    )
    workflow = build_workflow(
        language_model=model,
        facts=[samsung_fact],
        evidence=[samsung_evidence],
    )

    result = await workflow.run_turn(_request(message, conversation_id="draft-escape"))

    assert model.compose_calls == []
    assert "999조" not in result.answer.conclusion
    assert "주가는" not in result.answer.conclusion
    assert result.answer.conclusion == (
        "삼성전자의 2025년 영업이익은 "
        "43조 6,010억 5,100만 원(약 43.60조 원)입니다. "
        "쉽게 말해 '연결'은 회사와 자회사 실적을 합친 기준이고, "
        "'영업이익'은 회사가 본업으로 번 이익입니다."
    )
    assert "E+" not in result.answer.conclusion
    assert "KRW" not in result.answer.conclusion
    assert samsung_fact.evidence_id in result.answer.claims[0].evidence_ids
    assert result.answer.facts[0].fs_div == FsDiv.CFS
    assert result.answer.claims[0].evidence_ids == [samsung_evidence.evidence_id]
    assert result.answer.evidence == [samsung_evidence]
    assert result.events[-1].producer == Actor.SUPERVISOR
    assert result.events[-1].event_type == EventType.ANSWER_COMPLETED


@pytest.mark.asyncio
async def test_supervisor_owns_as_of_even_when_model_returns_future_date(
    samsung_fact,
    samsung_evidence,
) -> None:
    message = "2025년 삼성전자 연결 기준 영업이익은 얼마야?"
    model = ScriptedLanguageModel(
        {
            message: RouteDecision(
                intent=Intent.DIRECT_FACT,
                extracted_slots=Slots(
                    company=Company.SAMSUNG_ELECTRONICS,
                    metric=Metric.OPERATING_PROFIT,
                    period="2024",
                    value_scope=ValueScope.CUMULATIVE,
                    fs_div=FsDiv.CFS,
                    as_of=datetime(2099, 1, 1, tzinfo=UTC),
                ),
            )
        }
    )
    before = datetime.now(UTC)
    workflow = build_workflow(
        language_model=model,
        facts=[samsung_fact],
        evidence=[samsung_evidence],
    )

    result = await workflow.run_turn(
        _request(message, conversation_id="server-owned-as-of")
    )
    after = datetime.now(UTC)

    assert before <= result.answer.as_of <= after
    assert result.answer.as_of.year != 2099
    assert result.answer.facts[0].period == "2025"


@pytest.mark.asyncio
async def test_b_two_turn_comparison_returns_through_scenario_then_rag_with_exact_trace(
    samsung_fact,
    samsung_evidence,
    hynix_fact,
    hynix_evidence,
) -> None:
    first_message = "삼전 영업이익이 하닉에 따였다던데 사실이야?"
    second_message = "응, 2025년 연간 연결 기준으로 봐줘."
    model = ScriptedLanguageModel(
        {
            first_message: RouteDecision(
                intent=Intent.COMPARE,
                extracted_slots=Slots(
                    company=Company.SAMSUNG_ELECTRONICS,
                    comparison_company=Company.SK_HYNIX,
                    metric=Metric.OPERATING_PROFIT,
                    value_scope=ValueScope.CUMULATIVE,
                    fs_div=FsDiv.CFS,
                    user_claim=first_message,
                    claim_operator=ClaimOperator.LT,
                    as_of=AS_OF,
                ),
            ),
            second_message: RouteDecision(
                intent=Intent.COMPARE,
                extracted_slots=Slots(
                    period="2025",
                    value_scope=ValueScope.CUMULATIVE,
                    fs_div=FsDiv.CFS,
                    as_of=AS_OF,
                ),
            ),
        }
    )
    workflow = build_workflow(
        language_model=model,
        facts=[samsung_fact, hynix_fact],
        evidence=[samsung_evidence, hynix_evidence],
    )

    first = await workflow.run_turn(_request(first_message, conversation_id="demo-b-001"))
    second = await workflow.run_turn(_request(second_message, conversation_id="demo-b-001"))

    assert first.answer.workflow_status == WorkflowStatus.NEED_MORE_INFO
    assert first.answer.route == Route.SCENARIO
    assert first.answer.actor == Actor.SUPERVISOR
    assert _trace(first) == [
        ("SYSTEM", "turn.started", None, None),
        ("SUPERVISOR", "supervisor.route.selected", None, None),
        ("SCENARIO_AGENT", "agent.started", "MULTI_TURN_SCENARIO", None),
        ("SCENARIO_AGENT", "agent.completed", "MULTI_TURN_SCENARIO", "SUPERVISOR"),
        ("SUPERVISOR", "supervisor.route.selected", None, "SCENARIO_AGENT"),
        ("SUPERVISOR", "supervisor.answer.completed", None, None),
    ]

    assert second.answer.workflow_status == WorkflowStatus.ANSWER_READY
    assert second.answer.verdict == Verdict.CONFIRMED
    assert [fact.value for fact in second.answer.facts] == [
        samsung_fact.value,
        hynix_fact.value,
    ]
    assert second.answer.comparison_basis is not None
    assert second.answer.comparison_basis.is_valid
    assert _trace(second) == [
        ("SYSTEM", "turn.started", None, None),
        ("SUPERVISOR", "supervisor.route.selected", None, None),
        ("SCENARIO_AGENT", "agent.started", "MULTI_TURN_SCENARIO", None),
        ("SCENARIO_AGENT", "agent.completed", "MULTI_TURN_SCENARIO", "SUPERVISOR"),
        ("SUPERVISOR", "supervisor.route.selected", None, "SCENARIO_AGENT"),
        ("RAG_AGENT", "agent.started", "RAG_FAQ", None),
        ("RAG_AGENT", "agent.completed", "RAG_FAQ", "SUPERVISOR"),
        ("EVIDENCE_GATE", "evidence.checked", None, None),
        ("SUPERVISOR", "supervisor.answer.completed", None, None),
    ]
    assert [event.sequence for event in second.events] == list(range(1, len(second.events) + 1))


@pytest.mark.asyncio
async def test_supervisor_owns_operating_profit_scope_when_model_returns_point_in_time() -> None:
    message = "두 회사의 최근 공통 분기 영업이익을 비교해줘"
    samsung_evidence = make_evidence(
        "fact:samsung:2026Q1:operating_profit",
        Company.SAMSUNG_ELECTRONICS,
        "20260515002181",
    )
    hynix_evidence = make_evidence(
        "fact:sk-hynix:2026Q1:operating_profit",
        Company.SK_HYNIX,
        "20260515002287",
    )
    model = ScriptedLanguageModel(
        {
            message: RouteDecision(
                intent=Intent.COMPARE,
                extracted_slots=Slots(
                    company=Company.SAMSUNG_ELECTRONICS,
                    comparison_company=Company.SK_HYNIX,
                    metric=Metric.OPERATING_PROFIT,
                    period_mode=PeriodMode.LATEST_COMMON_INTERIM,
                    value_scope=ValueScope.POINT_IN_TIME,
                ),
            )
        }
    )
    workflow = build_workflow(
        language_model=model,
        facts=[
            make_fact(
                Company.SAMSUNG_ELECTRONICS,
                SAMSUNG_OPERATING_PROFIT,
                samsung_evidence.evidence_id,
                period="2026Q1",
            ),
            make_fact(
                Company.SK_HYNIX,
                SAMSUNG_OPERATING_PROFIT - 1,
                hynix_evidence.evidence_id,
                period="2026Q1",
            ),
        ],
        evidence=[samsung_evidence, hynix_evidence],
    )

    result = await workflow.run_turn(_request(message))

    assert result.answer.verdict == Verdict.CONFIRMED
    assert all(fact.value_scope == ValueScope.CUMULATIVE for fact in result.answer.facts)


@pytest.mark.asyncio
async def test_c_ds_loss_claim_is_contradicted_and_returns_an_exact_source_substring() -> None:
    message = "삼성전자 DS부문이 2025년에도 적자였다는 글이 사실이야?"
    ds_evidence = make_evidence(
        "chunk:samsung:2025:note30:ds-operating-profit",
        Company.SAMSUNG_ELECTRONICS,
        "20260310002820",
        excerpt=DS_EXCERPT,
        source_kind="FILING_CHUNK",
    )
    model = ScriptedLanguageModel(
        {
            message: RouteDecision(
                intent=Intent.CLAIM_CHECK,
                extracted_slots=Slots(
                    company=Company.SAMSUNG_ELECTRONICS,
                    topic_or_claim=message,
                    user_claim=message,
                    fs_div=FsDiv.CFS,
                    as_of=AS_OF,
                ),
            )
        }
    )
    search_index = {message: [EvidenceHit(evidence=ds_evidence, score=0.99)]}
    workflow = build_workflow(
        language_model=model,
        evidence=[ds_evidence],
        search_index=search_index,
        corpus_complete=True,
    )

    result = await workflow.run_turn(_request(message, conversation_id="demo-c-001"))

    assert result.answer.actor == Actor.SUPERVISOR
    assert result.answer.verdict == Verdict.CONTRADICTED
    assert result.answer.evidence == [ds_evidence]
    assert result.answer.evidence[0].excerpt in DS_SOURCE_TEXT
    assert result.answer.evidence[0].source_kind == "FILING_CHUNK"


class CountingScenarioAgent(MultiTurnScenarioAgent):
    def __init__(self) -> None:
        self.calls = 0

    def run(self, *, intent, slots):
        self.calls += 1
        return super().run(intent=intent, slots=slots)


class FailIfCalledRagAgent:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, query):
        self.calls += 1
        raise AssertionError("out-of-scope requests must not invoke a specialist")


@pytest.mark.asyncio
async def test_d_out_of_scope_financial_advice_invokes_zero_specialist_agents() -> None:
    message = "내일 삼성전자 사도 돼?"
    model = ScriptedLanguageModel(
        {message: RouteDecision(intent=Intent.OUT_OF_SCOPE, extracted_slots=Slots())}
    )
    scenario = CountingScenarioAgent()
    rag = FailIfCalledRagAgent()
    workflow = build_workflow(
        language_model=model,
        scenario_agent=scenario,
        rag_agent=rag,
    )

    result = await workflow.run_turn(_request(message, conversation_id="demo-d-001"))

    assert scenario.calls == 0
    assert rag.calls == 0
    assert result.answer.verdict == Verdict.OUT_OF_SCOPE
    assert result.answer.route == Route.OUT_OF_SCOPE
    assert result.answer.actor == Actor.SUPERVISOR
    assert not any(event.event_type == EventType.AGENT_STARTED for event in result.events)


class UnavailableFinancialRepository(InMemoryFinancialRepository):
    async def get_fact(self, **kwargs):
        raise RepositoryUnavailable("synthetic local repository outage")


class UnavailableConversationRepository:
    async def get(self, conversation_id):
        raise RepositoryUnavailable("synthetic conversation repository outage")

    async def put(self, record):
        raise RepositoryUnavailable("synthetic conversation repository outage")

    async def delete(self, conversation_id):
        raise RepositoryUnavailable("synthetic conversation repository outage")


@pytest.mark.asyncio
async def test_no_evidence_is_distinct_from_repository_system_unavailable(
    samsung_evidence,
) -> None:
    message = "2025년 삼성전자 연결 기준 영업이익은 얼마야?"
    decision = RouteDecision(
        intent=Intent.DIRECT_FACT,
        extracted_slots=Slots(
            company=Company.SAMSUNG_ELECTRONICS,
            metric=Metric.OPERATING_PROFIT,
            period="2025",
            value_scope=ValueScope.CUMULATIVE,
            fs_div=FsDiv.CFS,
            as_of=AS_OF,
        ),
    )

    no_evidence = await build_workflow(
        language_model=ScriptedLanguageModel({message: decision}),
        facts=[],
        evidence=[],
    ).run_turn(_request(message, conversation_id="no-evidence"))

    outage_rag = RagFaqAgent(
        financials=UnavailableFinancialRepository(),
        evidence=InMemoryEvidenceRepository([samsung_evidence]),
        embeddings=HashEmbeddingProvider(),
    )
    unavailable = await build_workflow(
        language_model=ScriptedLanguageModel({message: decision}),
        rag_agent=outage_rag,
    ).run_turn(_request(message, conversation_id="system-unavailable"))

    assert no_evidence.answer.workflow_status == WorkflowStatus.UNAVAILABLE
    assert no_evidence.answer.verdict == Verdict.NO_OFFICIAL_EVIDENCE
    assert no_evidence.answer.unavailable_reason.value == "NO_OFFICIAL_EVIDENCE"
    assert no_evidence.answer.facts == []
    assert no_evidence.answer.evidence == []

    assert unavailable.answer.workflow_status == WorkflowStatus.SYSTEM_ERROR
    assert unavailable.answer.verdict == Verdict.INSUFFICIENT_INFO
    assert unavailable.answer.unavailable_reason.value == "REPOSITORY_UNAVAILABLE"
    assert unavailable.answer.facts == []
    assert unavailable.answer.evidence == []
    no_evidence_public = to_public_events(
        _request(message, conversation_id="no-evidence"), no_evidence
    )[-1].data["result"]
    unavailable_public = to_public_events(
        _request(message, conversation_id="system-unavailable"), unavailable
    )[-1].data["result"]
    assert no_evidence_public["verdict"] == "INSUFFICIENT_EVIDENCE"
    assert no_evidence_public["status"] == "INSUFFICIENT_EVIDENCE"
    assert no_evidence_public["failure_reason"] == "NO_OFFICIAL_EVIDENCE"
    assert unavailable_public["verdict"] == "SYSTEM_UNAVAILABLE"
    assert unavailable_public["status"] == "SYSTEM_UNAVAILABLE"
    assert unavailable_public["failure_reason"] == "REPOSITORY_UNAVAILABLE"


class _UnavailableLanguageModel:
    provider_name = "elice_mlapi"

    async def classify(self, message, prior):
        raise ProviderUnavailable("synthetic MLAPI outage")


@pytest.mark.asyncio
async def test_mlapi_classification_failure_uses_deterministic_fallback() -> None:
    request = _request(
        "영업이익 알려줘",
        conversation_id="mlapi-unavailable",
    )
    result = await build_workflow(language_model=_UnavailableLanguageModel()).run_turn(
        request
    )
    routed = next(
        event for event in result.events if event.event_type == EventType.SUPERVISOR_ROUTED
    )

    assert result.answer.actor == Actor.SUPERVISOR
    assert result.answer.workflow_status == WorkflowStatus.NEED_MORE_INFO
    assert routed.payload["classification_fallback"] is True
    assert any(event.event_type == EventType.AGENT_STARTED for event in result.events)


class _ForbiddenClassifyModel:
    provider_name = "local_rules"

    async def classify(self, message, prior):
        raise AssertionError("external classify should not be called")


@pytest.mark.asyncio
async def test_direct_request_with_full_slots_bypasses_external_classifier() -> None:
    message = "2025년 삼성전자 영업이익은 얼마야?"
    result = await build_workflow(
        language_model=_ForbiddenClassifyModel(),
        facts=[make_fact(Company.SAMSUNG_ELECTRONICS, Decimal("10"), "fact:local")],
        evidence=[make_evidence("fact:local", Company.SAMSUNG_ELECTRONICS, "20260310002820")],
    ).run_turn(_request(message, conversation_id="forbidden-direct"))

    assert result.answer.actor == Actor.SUPERVISOR
    assert result.answer.workflow_status == WorkflowStatus.ANSWER_READY
    assert result.answer.verdict == Verdict.CONFIRMED


@pytest.mark.asyncio
async def test_compare_request_with_full_slots_bypasses_external_classifier() -> None:
    message = "삼성전자와 하이닉스 2025년 영업이익을 비교해줘"
    result = await build_workflow(
        language_model=_ForbiddenClassifyModel(),
        facts=[
            make_fact(Company.SAMSUNG_ELECTRONICS, SAMSUNG_OPERATING_PROFIT, "fact:samsung"),
            make_fact(Company.SK_HYNIX, HYNIX_OPERATING_PROFIT, "fact:hynix"),
        ],
        evidence=[
            make_evidence("fact:samsung", Company.SAMSUNG_ELECTRONICS, "20260310002820"),
            make_evidence("fact:hynix", Company.SK_HYNIX, "20260317000635"),
        ],
    ).run_turn(_request(message, conversation_id="forbidden-compare"))

    assert result.answer.actor == Actor.SUPERVISOR
    assert result.answer.workflow_status == WorkflowStatus.ANSWER_READY
    assert result.answer.verdict == Verdict.CONFIRMED
    assert len(result.answer.facts) == 2
    assert result.answer.facts[0].company == Company.SAMSUNG_ELECTRONICS
    assert result.answer.facts[1].company == Company.SK_HYNIX


@pytest.mark.asyncio
async def test_context_supplies_missing_company_and_year_slots() -> None:
    message = "영업이익은 얼마야?"
    result = await build_workflow(
        language_model=_UnavailableLanguageModel(),
        facts=[
            make_fact(Company.SAMSUNG_ELECTRONICS, SAMSUNG_OPERATING_PROFIT, "fact:context")
        ],
        evidence=[
            make_evidence("fact:context", Company.SAMSUNG_ELECTRONICS, "20260310002820")
        ],
    ).run_turn(
        _request(
            message,
            conversation_id="context-fill",
            context=ChatTurnContext(
                companies=[Company.SAMSUNG_ELECTRONICS],
                business_year=2025,
                metric_key="operating_profit",
            ),
        )
    )

    assert result.answer.actor == Actor.SUPERVISOR
    assert result.answer.workflow_status == WorkflowStatus.ANSWER_READY
    assert result.answer.facts[0].company == Company.SAMSUNG_ELECTRONICS
    assert result.answer.facts[0].period == "2025"


@pytest.mark.asyncio
async def test_compare_ui_context_does_not_force_metric_for_single_company_question() -> None:
    message = "영업이익은 얼마야?"
    result = await build_workflow(
        language_model=_UnavailableLanguageModel(),
        facts=[
            make_fact(Company.SAMSUNG_ELECTRONICS, SAMSUNG_OPERATING_PROFIT, "fact:single")
        ],
        evidence=[
            make_evidence("fact:single", Company.SAMSUNG_ELECTRONICS, "20260310002820")
        ],
    ).run_turn(
        _request(
            message,
            conversation_id="compare-context-single-question",
            context=ChatTurnContext(
                companies=[Company.SAMSUNG_ELECTRONICS, Company.SK_HYNIX],
                business_year=2025,
            ),
        )
    )

    assert result.answer.workflow_status == WorkflowStatus.ANSWER_READY
    assert result.answer.verdict == Verdict.CONFIRMED
    assert [fact.company for fact in result.answer.facts] == [
        Company.SAMSUNG_ELECTRONICS
    ]
    assert not any(
        event.payload.get("missing_slots") == ["metric"] for event in result.events
    )


@pytest.mark.asyncio
async def test_advanced_research_lens_with_two_company_words_does_not_request_metric() -> None:
    message = (
        "삼성전자 · SK하이닉스 2025년 사업보고서에서 전환사채·스톡옵션·유상증자 등 "
        "주식 희석 가능성을 확인해줘."
    )
    evidence = make_evidence(
        "dilution:chunk",
        Company.SAMSUNG_ELECTRONICS,
        "20260310002820",
        excerpt="전환사채 스톡옵션 유상증자 주식 희석 가능성 관련 공시 근거",
        source_kind="FILING_CHUNK",
    )
    result = await build_workflow(
        language_model=_UnavailableLanguageModel(),
        evidence=[evidence],
    ).run_turn(
        _request(
            message,
            conversation_id="advanced-lens-no-metric",
            context=ChatTurnContext(
                companies=[Company.SAMSUNG_ELECTRONICS, Company.SK_HYNIX],
                business_year=2025,
                report_code="11011",
            ),
        )
    )

    assert result.answer.workflow_status == WorkflowStatus.UNAVAILABLE
    assert result.answer.verdict == Verdict.PARTIALLY_CONFIRMED
    assert result.answer.route == Route.RAG
    assert result.answer.evidence
    assert not any(
        event.payload.get("missing_slots") == ["metric"] for event in result.events
    )


@pytest.mark.asyncio
async def test_complex_disclosure_question_produces_mece_analysis_report() -> None:
    message = "삼전 재무건전성 관련 공시 원문 근거를 MECE하게 분석해줘."
    evidence = [
        make_evidence(
            "health:liquidity",
            Company.SAMSUNG_ELECTRONICS,
            "20260515002181",
            excerpt="유동성 위험은 자금수지 예측과 현금 수준을 통해 관리합니다.",
            source_kind="FILING_CHUNK",
        ),
        make_evidence(
            "health:credit",
            Company.SAMSUNG_ELECTRONICS,
            "20260515002181",
            excerpt="신용위험과 부채 관련 한도를 주기적으로 점검합니다.",
            source_kind="FILING_CHUNK",
        ),
        make_evidence(
            "health:market",
            Company.SAMSUNG_ELECTRONICS,
            "20260515002181",
            excerpt="환율과 이자율 변동에 따른 시장위험을 모니터링합니다.",
            source_kind="FILING_CHUNK",
        ),
        make_evidence(
            "health:contingency",
            Company.SAMSUNG_ELECTRONICS,
            "20260515002181",
            excerpt="우발부채와 소송 결과의 재무 영향을 검토하고 있습니다.",
            source_kind="FILING_CHUNK",
        ),
    ]
    result = await build_workflow(
        language_model=_UnavailableLanguageModel(),
        evidence=evidence,
        search_index={
            message: [EvidenceHit(evidence=item, score=0.92) for item in evidence]
        },
    ).run_turn(
        _request(
            message,
            conversation_id="mece-health-report",
            context=ChatTurnContext(
                companies=[Company.SAMSUNG_ELECTRONICS],
                business_year=2026,
                report_code="11013",
            ),
        )
    )

    report = result.answer.analysis_report
    assert report is not None
    assert report.lens_key == "FINANCIAL_RESILIENCE"
    assert [finding.category_key for finding in report.findings] == [
        "LIQUIDITY",
        "CREDIT",
        "MARKET",
        "CONTINGENCY",
    ]
    assigned_ids = [
        evidence_id
        for finding in report.findings
        for evidence_id in finding.evidence_ids
    ]
    assert sorted(assigned_ids) == sorted(item.evidence_id for item in evidence)
    assert len(assigned_ids) == len(set(assigned_ids))
    analysis_started = next(
        event
        for event in result.events
        if event.event_type == EventType.AGENT_STARTED
        and event.producer == Actor.ANALYSIS_AGENT
    )
    assert analysis_started.payload["agent"] == "CONTEXT_FINANCIAL_RESILIENCE_ANALYST"

    public_result = to_public_events(
        _request(message, conversation_id="mece-health-report"), result
    )[-1].data["result"]
    assert public_result["verdict"] == "CONFIRMED"
    assert public_result["title"] == "재무건전성 핵심 신호"
    assert len(public_result["analysis_report"]["findings"]) == 4
    assert all(
        finding["plain_language"] and finding["next_check"]
        for finding in public_result["analysis_report"]["findings"]
    )
    assert "FINANCIAL_LITERACY_TUTOR" in public_result["trace_summary"]["agent_calls"]
    assert public_result["evidence_total_count"] == 4


@pytest.mark.asyncio
async def test_context_does_not_override_explicit_message_slots() -> None:
    message = "하이닉스 2024년 영업이익은 얼마야?"
    result = await build_workflow(
        language_model=_UnavailableLanguageModel(),
        facts=[
            make_fact(
                Company.SK_HYNIX,
                HYNIX_OPERATING_PROFIT,
                "fact:explicit",
                period="2024",
            )
        ],
        evidence=[make_evidence("fact:explicit", Company.SK_HYNIX, "20260317000635")],
    ).run_turn(
        _request(
            message,
            conversation_id="context-override",
            context=ChatTurnContext(
                companies=[Company.SAMSUNG_ELECTRONICS],
                business_year=2026,
                metric_key="total_assets",
            ),
        )
    )

    assert result.answer.actor == Actor.SUPERVISOR
    assert result.answer.workflow_status == WorkflowStatus.ANSWER_READY
    assert result.answer.facts[0].company == Company.SK_HYNIX
    assert result.answer.facts[0].period == "2024"


@pytest.mark.asyncio
async def test_conversation_db_failure_is_publicly_system_unavailable() -> None:
    message = "2025년 삼성전자 연결 기준 영업이익은 얼마야?"
    request = _request(message, conversation_id="conversation-db-unavailable")
    workflow = build_workflow(
        language_model=ScriptedLanguageModel({}),
        conversations=UnavailableConversationRepository(),
    )

    result = await workflow.run_turn(request)
    public_event = to_public_events(request, result)[-1]

    assert any(event.event_type == EventType.TURN_FAILED for event in result.events)
    assert public_event.type == "answer.completed"
    assert public_event.data["result"]["status"] == "SYSTEM_UNAVAILABLE"
    assert public_event.data["result"]["failure_reason"] == "REPOSITORY_UNAVAILABLE"
