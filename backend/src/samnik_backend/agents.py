from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Sequence

from .domain import (
    Actor,
    AnalysisFinding,
    AnalysisReport,
    AnswerClaim,
    ClaimKind,
    ClaimOperator,
    ComparisonBasis,
    Company,
    EvidenceStatus,
    ConversationRecord,
    FinancialFact,
    FsDiv,
    Intent,
    Metric,
    NormalizedQuery,
    PeriodMode,
    RagResult,
    Route,
    ScenarioResult,
    Slots,
    UnavailableReason,
    ValueScope,
    Verdict,
    WorkflowStatus,
)
from .errors import EvidenceValidationError, RepositoryUnavailable
from .company_context import company_context_terms
from .ports import EmbeddingPort, EvidenceRepository, FinancialRepository
from .prompt_security import contains_untrusted_instruction


@dataclass(frozen=True)
class _FinancialLens:
    key: str
    label: str
    cues: tuple[str, ...]
    metrics: tuple[Metric, ...]


_FINANCIAL_LENSES = (
    _FinancialLens(
        "OPERATING_PROFITABILITY",
        "본업 수익성",
        ("본업 수익성",),
        (Metric.REVENUE, Metric.OPERATING_PROFIT),
    ),
    _FinancialLens(
        "NET_PROFITABILITY",
        "최종 수익성",
        ("최종 수익성", "순이익 전환"),
        (Metric.REVENUE, Metric.NET_INCOME),
    ),
    _FinancialLens(
        "CASH_CONVERSION",
        "현금 전환력",
        ("현금 전환", "현금전환", "현금 창출", "현금창출"),
        (Metric.OPERATING_PROFIT, Metric.OPERATING_CASH_FLOW),
    ),
    _FinancialLens(
        "FINANCIAL_RESILIENCE",
        "재무건전성",
        ("재무건전", "재무 건전", "재무안정", "재무 안정"),
        (Metric.TOTAL_ASSETS, Metric.TOTAL_LIABILITIES, Metric.TOTAL_EQUITY),
    ),
)


def financial_diagnosis_lens_for(text: str) -> _FinancialLens | None:
    normalized = text.replace(" ", "").lower()
    for lens in _FINANCIAL_LENSES:
        if any(cue.replace(" ", "").lower() in normalized for cue in lens.cues):
            return lens
    return None


def _ratio_percent(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    if denominator == 0:
        return None
    return (numerator / denominator * Decimal("100")).quantize(Decimal("0.1"))


def _diagnostic_ratio_values(
    lens_key: str,
    values: dict[Metric, Decimal],
) -> tuple[tuple[str, Decimal], ...]:
    definitions: tuple[tuple[str, Decimal | None], ...]
    if lens_key == "OPERATING_PROFITABILITY":
        definitions = (
            ("영업이익률", _ratio_percent(values[Metric.OPERATING_PROFIT], values[Metric.REVENUE])),
        )
    elif lens_key == "NET_PROFITABILITY":
        definitions = (
            ("순이익률", _ratio_percent(values[Metric.NET_INCOME], values[Metric.REVENUE])),
        )
    elif lens_key == "CASH_CONVERSION":
        definitions = (
            (
                "현금 전환율",
                _ratio_percent(
                    values[Metric.OPERATING_CASH_FLOW],
                    values[Metric.OPERATING_PROFIT],
                ),
            ),
        )
    else:
        definitions = (
            (
                "부채비율",
                _ratio_percent(values[Metric.TOTAL_LIABILITIES], values[Metric.TOTAL_EQUITY]),
            ),
            (
                "자기자본비율",
                _ratio_percent(values[Metric.TOTAL_EQUITY], values[Metric.TOTAL_ASSETS]),
            ),
        )
    return tuple((label, value) for label, value in definitions if value is not None)


def is_financial_diagnosis_query(query: NormalizedQuery) -> bool:
    topic = query.topic_or_claim or ""
    requests_source_analysis = any(
        cue in topic for cue in ("공시 원문", "원문 근거", "위험 요인", "MECE")
    )
    return bool(
        topic
        and query.period
        and len(query.companies) == 1
        and financial_diagnosis_lens_for(topic)
        and not requests_source_analysis
    )


class FinancialDiagnosisAgent:
    """Calculate one explicit diagnostic lens from verified statement facts."""

    def __init__(
        self,
        *,
        financials: FinancialRepository,
        evidence: EvidenceRepository,
    ) -> None:
        self._financials = financials
        self._evidence = evidence

    def supports(self, query: NormalizedQuery) -> bool:
        return is_financial_diagnosis_query(query)

    async def run(self, query: NormalizedQuery) -> RagResult:
        lens = financial_diagnosis_lens_for(query.topic_or_claim or "")
        assert lens is not None and query.period is not None
        facts: list[FinancialFact] = []
        for metric in lens.metrics:
            scope = default_value_scope(metric)
            assert scope is not None
            fact = await self._financials.get_fact(
                company=query.companies[0],
                metric=metric,
                period=query.period,
                value_scope=scope,
                fs_div=query.fs_div,
                as_of=query.as_of,
            )
            if fact is None:
                return RagResult(
                    workflow_status=WorkflowStatus.UNAVAILABLE,
                    verdict=Verdict.NO_OFFICIAL_EVIDENCE,
                    normalized_query=query,
                    limitations=[
                        f"{lens.label} 계산에 필요한 {metric.value} 수치를 동일 공시 기준에서 찾지 못했습니다."
                    ],
                    unavailable_reason=UnavailableReason.NO_OFFICIAL_EVIDENCE,
                    searched_until=query.as_of,
                )
            facts.append(fact)

        evidence = await self._evidence.get_by_ids([fact.evidence_id for fact in facts])
        if len(evidence) != len(facts) or any(
            item.evidence_status != EvidenceStatus.OFFICIAL_API_VERIFIED
            for item in evidence
        ):
            return RagResult(
                workflow_status=WorkflowStatus.UNAVAILABLE,
                verdict=Verdict.INSUFFICIENT_INFO,
                normalized_query=query,
                limitations=["진단 입력 수치의 공식 공시 근거 연결을 완성하지 못했습니다."],
                unavailable_reason=UnavailableReason.SOURCE_OUT_OF_SCOPE,
                searched_until=query.as_of,
            )

        values = {fact.metric: fact.value for fact in facts}
        evidence_ids = [fact.evidence_id for fact in facts]
        findings: list[AnalysisFinding] = []
        if lens.key == "OPERATING_PROFITABILITY":
            ratio = _ratio_percent(values[Metric.OPERATING_PROFIT], values[Metric.REVENUE])
            if ratio is not None:
                findings.append(
                    AnalysisFinding(
                        category_key="OPERATING_MARGIN",
                        category_label="영업이익률",
                        summary=f"영업이익 ÷ 매출액으로 {ratio}%를 계산했습니다.",
                        evidence_ids=evidence_ids,
                    )
                )
        elif lens.key == "NET_PROFITABILITY":
            ratio = _ratio_percent(values[Metric.NET_INCOME], values[Metric.REVENUE])
            if ratio is not None:
                findings.append(
                    AnalysisFinding(
                        category_key="NET_MARGIN",
                        category_label="순이익률",
                        summary=f"당기순이익 ÷ 매출액으로 {ratio}%를 계산했습니다.",
                        evidence_ids=evidence_ids,
                    )
                )
        elif lens.key == "CASH_CONVERSION":
            ratio = _ratio_percent(
                values[Metric.OPERATING_CASH_FLOW],
                values[Metric.OPERATING_PROFIT],
            )
            if ratio is not None:
                findings.append(
                    AnalysisFinding(
                        category_key="CASH_CONVERSION",
                        category_label="영업이익의 현금 전환율",
                        summary=f"영업활동현금흐름 ÷ 영업이익으로 {ratio}%를 계산했습니다.",
                        evidence_ids=evidence_ids,
                    )
                )
        else:
            assets = values[Metric.TOTAL_ASSETS]
            liabilities = values[Metric.TOTAL_LIABILITIES]
            equity = values[Metric.TOTAL_EQUITY]
            identity_gap = abs(assets - liabilities - equity)
            if assets == 0 or identity_gap / abs(assets) > Decimal("0.01"):
                return RagResult(
                    workflow_status=WorkflowStatus.UNAVAILABLE,
                    verdict=Verdict.INSUFFICIENT_INFO,
                    normalized_query=query,
                    facts=facts,
                    evidence=evidence,
                    limitations=["자산 = 부채 + 자본 검증이 허용 오차 1%를 벗어나 진단을 중단했습니다."],
                    unavailable_reason=UnavailableReason.PERIOD_OR_BASIS_MISMATCH,
                    searched_until=query.as_of,
                )
            debt_ratio = _ratio_percent(liabilities, equity)
            equity_ratio = _ratio_percent(equity, assets)
            if debt_ratio is not None:
                findings.append(
                    AnalysisFinding(
                        category_key="DEBT_TO_EQUITY",
                        category_label="부채비율",
                        summary=f"부채총계 ÷ 자본총계로 {debt_ratio}%를 계산했습니다.",
                        evidence_ids=evidence_ids,
                    )
                )
            if equity_ratio is not None:
                findings.append(
                    AnalysisFinding(
                        category_key="EQUITY_TO_ASSETS",
                        category_label="자기자본비율",
                        summary=f"자본총계 ÷ 자산총계로 {equity_ratio}%를 계산했습니다.",
                        evidence_ids=evidence_ids,
                    )
                )

        if not findings:
            return RagResult(
                workflow_status=WorkflowStatus.UNAVAILABLE,
                verdict=Verdict.INSUFFICIENT_INFO,
                normalized_query=query,
                facts=facts,
                evidence=evidence,
                limitations=["분모가 0이어서 진단 비율을 계산하지 않았습니다."],
                unavailable_reason=UnavailableReason.PERIOD_OR_BASIS_MISMATCH,
                searched_until=query.as_of,
            )

        period_match = re.fullmatch(r"(20\d{2})(Q1|H1|Q3)?", query.period)
        trend_points: list[tuple[str, tuple[tuple[str, Decimal], ...], list[str]]] = []
        trend_evidence = []
        if period_match:
            current_year = int(period_match.group(1))
            suffix = period_match.group(2) or ""
            for historical_year in range(max(2015, current_year - 2), current_year):
                historical_facts: list[FinancialFact] = []
                for metric in lens.metrics:
                    scope = default_value_scope(metric)
                    assert scope is not None
                    historical_fact = await self._financials.get_fact(
                        company=query.companies[0],
                        metric=metric,
                        period=f"{historical_year}{suffix}",
                        value_scope=scope,
                        fs_div=query.fs_div,
                        as_of=query.as_of,
                    )
                    if historical_fact is None:
                        historical_facts = []
                        break
                    historical_facts.append(historical_fact)
                if len(historical_facts) != len(lens.metrics):
                    continue
                historical_values = {
                    fact.metric: fact.value for fact in historical_facts
                }
                ratios = _diagnostic_ratio_values(lens.key, historical_values)
                if not ratios:
                    continue
                historical_evidence = await self._evidence.get_by_ids(
                    [fact.evidence_id for fact in historical_facts]
                )
                if len(historical_evidence) != len(historical_facts) or any(
                    item.evidence_status != EvidenceStatus.OFFICIAL_API_VERIFIED
                    for item in historical_evidence
                ):
                    continue
                trend_evidence.extend(historical_evidence)
                trend_points.append(
                    (
                        f"{historical_year}{suffix}",
                        ratios,
                        [fact.evidence_id for fact in historical_facts],
                    )
                )

        current_ratios = _diagnostic_ratio_values(lens.key, values)
        if trend_points and current_ratios:
            trend_points.append((query.period, current_ratios, evidence_ids))
            ratio_labels = [label for label, _ in current_ratios]
            series = []
            for ratio_label in ratio_labels:
                values_by_period = [
                    (period, dict(ratios)[ratio_label])
                    for period, ratios, _ in trend_points
                    if ratio_label in dict(ratios)
                ]
                if len(values_by_period) < 2:
                    continue
                rendered = " → ".join(
                    f"{period} {value}%" for period, value in values_by_period
                )
                change = values_by_period[-1][1] - values_by_period[0][1]
                direction = "상승" if change > 0 else ("하락" if change < 0 else "유지")
                statement = (
                    f"{ratio_label}은 {rendered}로, 첫 기간 대비 {abs(change)}%p {direction}했습니다."
                )
                if abs(change) >= Decimal("15.0"):
                    statement += (
                        " 변화 폭이 15%p 이상이므로 구조적 개선·악화로 바로 해석하지 않고, "
                        "원문 수치와 보고기간 기준, 일회성 요인을 우선 재확인해야 합니다."
                    )
                series.append(statement)
            if series:
                trend_ids = list(
                    dict.fromkeys(
                        evidence_id
                        for _, _, point_ids in trend_points
                        for evidence_id in point_ids
                    )
                )
                findings.append(
                    AnalysisFinding(
                        category_key="THREE_YEAR_TREND",
                        category_label="동일 분기 추세·이상변동",
                        summary=" ".join(series),
                        evidence_ids=trend_ids,
                    )
                )
                evidence = list(
                    {item.evidence_id: item for item in [*evidence, *trend_evidence]}.values()
                )

        report = AnalysisReport(
            lens_key=lens.key,
            lens_label=lens.label,
            methodology="FINANCIAL_RATIO_CALCULATION_V1",
            findings=findings,
            source_count=len(evidence),
            limitation=(
                "동일 회사·동일 보고기간·연결 기준의 공식 수치로 계산하고, "
                "각 숫자의 의미와 다음 공시에서 이어 볼 항목을 연결했습니다."
            ),
        )
        return RagResult(
            workflow_status=WorkflowStatus.ANSWER_READY,
            verdict=Verdict.CONFIRMED,
            normalized_query=query,
            facts=facts,
            evidence=evidence,
            analysis_report=report,
            searched_filing_ids=[item.rcept_no for item in evidence],
            searched_until=query.as_of,
        )


@dataclass(frozen=True)
class _AnalysisCategory:
    key: str
    label: str
    terms: tuple[str, ...]


@dataclass(frozen=True)
class _DisclosureLens:
    key: str
    label: str
    cues: tuple[str, ...]
    anchors: tuple[str, ...]
    categories: tuple[_AnalysisCategory, ...]


_DISCLOSURE_LENSES = (
    _DisclosureLens(
        key="FINANCIAL_RESILIENCE",
        label="재무건전성",
        cues=("재무건전", "재무 건전", "재무안정", "재무 안정"),
        anchors=("유동성", "신용위험", "부채", "차입", "채무보증", "시장위험", "우발부채"),
        categories=(
            _AnalysisCategory("LIQUIDITY", "유동성·자금조달", ("유동성", "자금수지", "현금", "차입", "만기")),
            _AnalysisCategory("CREDIT", "부채·신용위험", ("부채", "신용위험", "채무", "보증", "담보")),
            _AnalysisCategory("MARKET", "시장위험", ("환율", "이자율", "주가변동", "시장위험", "파생")),
            _AnalysisCategory("CONTINGENCY", "우발·회계위험", ("우발", "소송", "충당", "회계", "손상")),
        ),
    ),
    _DisclosureLens(
        key="WORKING_CAPITAL",
        label="운전자본 리스크",
        cues=("운전자본", "재고자산", "매출채권"),
        anchors=("재고자산", "재고평가", "평가충당금", "매출채권", "대손", "운전자본", "매입채무", "신용손실"),
        categories=(
            _AnalysisCategory("INVENTORY", "재고자산", ("재고", "평가손실", "진부화")),
            _AnalysisCategory("RECEIVABLES", "매출채권", ("매출채권", "대손", "회수")),
            _AnalysisCategory("CASH_CYCLE", "현금회전", ("운전자본", "현금회전", "영업활동현금흐름", "매입채무")),
            _AnalysisCategory("IMPAIRMENT", "손상·충당", ("손상", "충당금", "신용손실")),
        ),
    ),
    _DisclosureLens(
        key="EARNINGS_QUALITY",
        label="이익의 질",
        cues=("이익의 질", "이익의 지속", "일회성", "손상차손"),
        anchors=("영업이익", "일회성", "처분손익", "손상차손", "충당금", "현금흐름", "공정가치"),
        categories=(
            _AnalysisCategory("CORE_EARNINGS", "본업 손익", ("영업이익", "매출", "사업부문")),
            _AnalysisCategory("NON_RECURRING", "일회성 손익", ("일회성", "처분", "기타수익", "기타비용")),
            _AnalysisCategory("ESTIMATES", "손상·추정", ("손상", "충당", "추정", "공정가치")),
            _AnalysisCategory("CASH_ALIGNMENT", "현금 정합성", ("현금흐름", "영업활동", "운전자본")),
        ),
    ),
    _DisclosureLens(
        key="DILUTION",
        label="희석 리스크",
        cues=("희석", "전환사채", "스톡옵션", "유상증자"),
        anchors=("전환사채", "신주인수권", "스톡옵션", "주식선택권", "유상증자", "신주", "자기주식", "발행주식"),
        categories=(
            _AnalysisCategory("CONVERTIBLES", "전환·신주인수권", ("전환사채", "신주인수권", "전환권")),
            _AnalysisCategory("SHARE_PAY", "주식보상", ("스톡옵션", "주식선택권", "주식보상")),
            _AnalysisCategory("EQUITY_ISSUE", "신주 발행", ("유상증자", "신주", "증자")),
            _AnalysisCategory("CAPITAL_CHANGE", "자본 변동", ("자본금", "자기주식", "발행주식")),
        ),
    ),
    _DisclosureLens(
        key="CAPITAL_ALLOCATION",
        label="자본배분 정책",
        cues=("자본배분", "설비투자", "배당", "자사주"),
        anchors=("설비투자", "시설투자", "연구개발", "배당", "주주환원", "자기주식", "자사주", "소각", "자금조달"),
        categories=(
            _AnalysisCategory("CAPEX", "설비·연구개발 투자", ("설비투자", "시설투자", "연구개발", "CAPEX")),
            _AnalysisCategory("DIVIDEND", "배당", ("배당", "배당금", "주주환원")),
            _AnalysisCategory("TREASURY", "자기주식", ("자기주식", "자사주", "소각")),
            _AnalysisCategory("FUNDING", "재원·조달", ("현금", "차입", "사채", "자금조달")),
        ),
    ),
    _DisclosureLens(
        key="AUDIT_ACCOUNTING",
        label="감사·회계 쟁점",
        cues=("핵심감사", "감사사항", "회계 위험"),
        anchors=("핵심감사", "감사의견", "회계추정", "공정가치", "손상", "내부회계", "내부통제", "회계정책"),
        categories=(
            _AnalysisCategory("KAM", "핵심감사사항", ("핵심감사사항", "핵심감사")),
            _AnalysisCategory("AUDIT_OPINION", "감사·검토의견", ("감사의견", "적정의견", "검토보고서", "감사보고서")),
            _AnalysisCategory("ESTIMATION", "회계추정·정책", ("추정", "가정", "공정가치", "손상", "회계정책", "기준서")),
            _AnalysisCategory("CONTROL", "내부통제", ("내부회계", "내부통제", "통제")),
        ),
    ),
    _DisclosureLens(
        key="CONCENTRATION",
        label="사업 집중도",
        cues=("사업 집중", "고객 집중", "주요 고객", "사업부문"),
        anchors=("사업부문", "부문별", "주요 고객", "매출처", "지역별", "원재료", "공급망", "협력사"),
        categories=(
            _AnalysisCategory("SEGMENT", "사업부문", ("사업부문", "부문별", "세그먼트")),
            _AnalysisCategory("CUSTOMER", "고객 집중", ("주요 고객", "고객", "매출처")),
            _AnalysisCategory("GEOGRAPHY", "지역 집중", ("지역", "국가", "해외")),
            _AnalysisCategory("SUPPLY", "공급망 집중", ("공급", "원재료", "매입", "협력사")),
        ),
    ),
)


def disclosure_lens_for(text: str) -> _DisclosureLens | None:
    normalized = text.replace(" ", "").lower()
    for lens in _DISCLOSURE_LENSES:
        if any(cue.replace(" ", "").lower() in normalized for cue in lens.cues):
            return lens
    return None


def _explicitly_requested_categories(
    lens: _DisclosureLens,
    claim: str,
) -> tuple[_AnalysisCategory, ...]:
    normalized = claim.replace(" ", "").lower()
    selected = tuple(
        category
        for category in lens.categories
        if any(term.replace(" ", "").lower() in normalized for term in category.terms)
    )
    return selected or lens.categories


_CATEGORY_INTERPRETATIONS = {
    "INVENTORY": (
        "재고자산의 평가 방식과 평가충당금 문맥을 확인했습니다. 재고 가치 하락 위험을 "
        "판단할 직접 근거이며, 실제 부담은 전기 대비 재고·충당금 증감과 함께 봐야 합니다."
    ),
    "RECEIVABLES": (
        "매출채권의 회수·대손 또는 기대신용손실 문맥을 확인했습니다. 회수 위험 판단에는 "
        "채권 증가율과 손실충당금 변화를 함께 비교해야 합니다."
    ),
    "CASH_CYCLE": (
        "운전자본·매입채무·영업현금흐름 문맥을 확인했습니다. 현금회전 판단에는 재고와 "
        "매출채권, 매입채무의 동행 여부가 필요합니다."
    ),
    "IMPAIRMENT": (
        "재고 또는 채권의 손상·충당 문맥을 확인했습니다. 손실 인식 규모와 적용 기준을 "
        "기간별로 비교해야 위험의 확대 여부를 판단할 수 있습니다."
    ),
    "CONVERTIBLES": (
        "전환사채·신주인수권 조건을 확인했습니다. 잠재 주식수와 행사가격, 행사기간을 "
        "함께 봐야 실제 희석 가능성을 판단할 수 있습니다."
    ),
    "SHARE_PAY": (
        "주식보상·주식선택권 문맥을 확인했습니다. 미행사 수량과 가득 조건이 잠재 희석의 "
        "핵심 확인 항목입니다."
    ),
    "EQUITY_ISSUE": (
        "신주·유상증자 관련 문맥을 확인했습니다. 발행 규모와 목적, 기존 주주 지분 변화가 "
        "실제 희석 판단의 기준입니다."
    ),
    "CAPEX": (
        "설비·연구개발 투자 문맥을 확인했습니다. 투자 규모는 영업현금흐름과 잉여현금, "
        "조달 구조를 함께 봐야 지속 가능성을 판단할 수 있습니다."
    ),
    "DIVIDEND": (
        "배당·주주환원 문맥을 확인했습니다. 배당 규모는 순이익뿐 아니라 현금흐름과 투자 "
        "소요를 함께 비교해야 합니다."
    ),
    "KAM": (
        "핵심감사사항 또는 감사인의 중점 검토 영역을 확인했습니다. 이는 오류 확정이 아니라 "
        "감사에서 특별히 주의를 기울인 회계 판단 영역입니다."
    ),
    "AUDIT_OPINION": (
        "감사·검토보고서의 의견 문맥을 확인했습니다. 보고서 종류와 의견 유형을 구분해야 하며, "
        "내부회계 검토보고서를 재무제표 핵심감사사항으로 해석하지 않습니다."
    ),
    "ESTIMATION": (
        "회계추정·정책 문맥을 확인했습니다. 추정 불확실성은 적용 가정과 민감도, 당기 변경 "
        "내역을 함께 확인해야 판단할 수 있습니다."
    ),
    "CONTROL": (
        "내부회계·내부통제 문맥을 확인했습니다. 이는 핵심감사사항과 별도 영역이며, 검토 또는 "
        "감사의견과 발견된 중요한 취약점 유무를 구분해 읽어야 합니다."
    ),
    "SEGMENT": (
        "사업부문별 실적 문맥을 확인했습니다. 집중도 판단에는 부문별 매출·이익 비중과 "
        "기간별 변화가 필요합니다."
    ),
    "CUSTOMER": (
        "주요 고객·매출처 문맥을 확인했습니다. 특정 고객 의존도는 고객별 매출 비중과 "
        "계약 지속성 정보가 함께 있어야 판단할 수 있습니다."
    ),
}


def _finding_summary(category: _AnalysisCategory, items: Sequence) -> str:
    context = "\n".join(
        part
        for item in items
        for part in (item.section_path, item.excerpt)
        if part
    )
    if category.key == "INVENTORY" and "평가충당금" in context:
        inventory_total = re.search(
            r"합\s*계\s*\|\s*([0-9,]+)\s*\|\s*[0-9,]+\s*\|\s*[△-]?([0-9,]+)",
            context,
        )
        if inventory_total:
            acquisition = Decimal(inventory_total.group(1).replace(",", ""))
            allowance = Decimal(inventory_total.group(2).replace(",", ""))
            ratio = _ratio_percent(allowance, acquisition)
            acquisition_trillion = (acquisition / Decimal("1000000")).quantize(
                Decimal("0.1")
            )
            allowance_trillion = (allowance / Decimal("1000000")).quantize(
                Decimal("0.1")
            )
            return (
                f"연결 기준 재고 취득원가 합계는 약 {acquisition_trillion}조원, "
                f"평가충당금은 약 {allowance_trillion}조원으로 취득원가의 {ratio}%입니다. "
                "가치 하락 위험이 실제 충당금으로 반영된 직접 신호이며, 위험의 확대 여부는 "
                "전기 충당금률과 비교해야 판단할 수 있습니다."
            )
        return (
            "재고자산에 저가법과 평가충당금이 적용된 직접 근거를 확인했습니다. 이는 가치 하락 "
            "위험을 이미 측정하고 있다는 신호이며, 위험 확대 여부는 전기 대비 재고 잔액과 "
            "충당금 비율의 증감을 계산해 판단해야 합니다."
        )
    if category.key == "RECEIVABLES":
        aging = re.search(
            r"구성비율\s*\|\s*([0-9.]+)%\s*\|\s*([0-9.]+)%\s*\|\s*"
            r"([0-9.]+)%\s*\|\s*([0-9.]+)%",
            context,
        )
        receivable_total = re.search(
            r"금\s*액\s*\|\s*[0-9,]+\s*\|\s*[0-9,]+\s*\|\s*[0-9,]+\s*\|\s*"
            r"[0-9,]+\s*\|\s*([0-9,]+)",
            context,
        )
        if aging and receivable_total:
            total = Decimal(receivable_total.group(1).replace(",", ""))
            total_trillion = (total / Decimal("1000000")).quantize(Decimal("0.1"))
            return (
                f"연결 매출채권은 약 {total_trillion}조원이며, 공시된 경과기간 구성은 "
                f"6개월 이하 {aging.group(1)}%, 3년 초과 {aging.group(4)}%입니다. "
                "장기 경과채권 비중은 제한적으로 보이지만, 회수 위험의 증감은 전기 동일 표와 "
                "대손충당금률을 함께 비교해야 판단할 수 있습니다."
            )
        if any(term in context for term in ("대손충당금", "기대신용손실", "손실충당금")):
            return (
                "매출채권에 대손·기대신용손실 충당이 적용된 직접 근거를 확인했습니다. 회수 위험의 "
                "방향은 매출채권 증가율과 충당금률의 전기 대비 변화를 함께 계산해야 판단할 수 있습니다."
            )
        return (
            "매출채권 잔액 문맥은 확인했지만 대손·기대신용손실 변화까지 연결되지 않았습니다. "
            "잔액만으로 회수 위험이 커졌다고 단정하지 않습니다."
        )
    contextual = _CATEGORY_INTERPRETATIONS.get(category.key)
    if contextual:
        return contextual
    return (
        f"{category.label} 관련 직접 공시 문맥을 확인했습니다. 원문에 표시된 적용 범위와 "
        "조건을 함께 확인해야 하며, 이 항목만으로 위험 수준을 단정하지 않습니다."
    )


class MeceDisclosureAnalysisAgent:
    """Assign every evidence item to one exclusive lens category."""

    def supports(self, result: RagResult) -> bool:
        claim = result.normalized_query.topic_or_claim or ""
        return bool(result.evidence and not result.facts and disclosure_lens_for(claim))

    def agent_name(self, result: RagResult) -> str:
        lens = disclosure_lens_for(result.normalized_query.topic_or_claim or "")
        return (
            f"CONTEXT_{lens.key}_ANALYST"
            if lens
            else "CONTEXT_DISCLOSURE_ANALYST"
        )

    def run(self, result: RagResult) -> RagResult:
        claim = result.normalized_query.topic_or_claim or ""
        lens = disclosure_lens_for(claim)
        if lens is None or not result.evidence:
            return result

        candidate_categories = _explicitly_requested_categories(lens, claim)
        relevance_terms = tuple(
            dict.fromkeys(
                term for category in candidate_categories for term in category.terms
            )
        )

        relevant_evidence = []
        for evidence in result.evidence:
            context = "\n".join(
                part for part in (evidence.section_path, evidence.excerpt) if part
            ).lower()
            if any(term.lower() in context for term in relevance_terms):
                relevant_evidence.append(evidence)
        if not relevant_evidence:
            return result.model_copy(
                update={
                    "workflow_status": WorkflowStatus.UNAVAILABLE,
                    "verdict": Verdict.NO_OFFICIAL_EVIDENCE,
                    "evidence": [],
                    "limitations": [
                        *result.limitations,
                        f"검색 결과가 {lens.label}의 필수 문맥 기준을 통과하지 못했습니다.",
                    ],
                    "unavailable_reason": UnavailableReason.NO_OFFICIAL_EVIDENCE,
                }
            )

        assigned: dict[str, list] = {
            category.key: [] for category in candidate_categories
        }
        for evidence in relevant_evidence:
            context = "\n".join(
                part for part in (evidence.section_path, evidence.excerpt) if part
            ).lower()
            scored_categories = [
                (
                    sum(term.lower() in context for term in category.terms),
                    -index,
                    category.key,
                )
                for index, category in enumerate(candidate_categories)
            ]
            best_score, _, best_key = max(scored_categories)
            if best_score > 0:
                assigned[best_key].append(evidence)

        findings: list[AnalysisFinding] = []
        for category in candidate_categories:
            items = assigned[category.key]
            if not items:
                continue
            findings.append(
                AnalysisFinding(
                    category_key=category.key,
                    category_label=category.label,
                    summary=_finding_summary(category, items),
                    evidence_ids=[item.evidence_id for item in items],
                )
            )
        if not findings:
            return result.model_copy(
                update={
                    "workflow_status": WorkflowStatus.UNAVAILABLE,
                    "verdict": Verdict.NO_OFFICIAL_EVIDENCE,
                    "evidence": [],
                    "limitations": [
                        *result.limitations,
                        f"{lens.label}의 분석 범주에 직접 배정할 근거를 찾지 못했습니다.",
                    ],
                    "unavailable_reason": UnavailableReason.NO_OFFICIAL_EVIDENCE,
                }
            )

        assigned_ids = {
            evidence_id
            for finding in findings
            for evidence_id in finding.evidence_ids
        }
        analyzed_evidence = [
            evidence
            for evidence in relevant_evidence
            if evidence.evidence_id in assigned_ids
        ]
        missing_categories = [
            category.label
            for category in candidate_categories
            if not assigned[category.key]
        ]

        report = AnalysisReport(
            lens_key=lens.key,
            lens_label=lens.label,
            findings=findings[:4],
            source_count=len(analyzed_evidence),
            limitation=(
                "질문과 직접 연결되는 공시 근거만 사용하고, 확인된 신호의 의미와 "
                "다음 공시에서 이어 볼 항목을 함께 정리했습니다."
                + (
                    f" 직접 근거를 확인하지 못한 영역: {', '.join(missing_categories)}."
                    if missing_categories
                    else ""
                )
            ),
        )
        return result.model_copy(
            update={
                "analysis_report": report,
                "evidence": analyzed_evidence,
                "searched_filing_ids": sorted(
                    {item.rcept_no for item in analyzed_evidence}
                ),
            }
        )


_TUTOR_GUIDANCE: dict[str, tuple[str, str]] = {
    "OPERATING_MARGIN": (
        "매출 100원 중 본업에서 얼마를 남겼는지 보여 주는 값입니다. 한 번의 수치보다 같은 "
        "분기의 3년 추세가 개선되는지가 더 중요합니다.",
        "매출 증가율, 매출원가율과 사업부문별 영업이익이 이번 변화를 만들었는지 확인하세요.",
    ),
    "NET_MARGIN": (
        "매출 100원 중 이자·세금과 비영업손익까지 반영한 뒤 남은 몫입니다. 영업이익률과 "
        "차이가 크게 벌어지면 일회성 손익이나 금융비용을 확인할 이유가 생깁니다.",
        "영업이익률과의 차이를 만든 기타손익·금융손익·법인세 항목을 확인하세요.",
    ),
    "CASH_CONVERSION": (
        "회계상 영업이익이 실제 영업현금으로 얼마나 바뀌었는지 보는 값입니다. 낮은 상태가 "
        "이어지면 재고나 매출채권에 현금이 묶였는지 확인해야 합니다.",
        "3년 현금 전환율과 재고자산·매출채권·매입채무 증감을 함께 보세요.",
    ),
    "DEBT_TO_EQUITY": (
        "주주자본 100원에 부채를 얼마 사용하고 있는지 보여 줍니다. 낮고 높음의 절대 기준보다 "
        "차입금 만기와 영업현금 창출력까지 감당 가능한지가 중요합니다.",
        "유동부채, 차입금 만기, 이자비용과 영업현금흐름을 함께 확인하세요.",
    ),
    "EQUITY_TO_ASSETS": (
        "전체 자산 중 부채가 아닌 자기자본으로 마련한 비중입니다. 완충력의 기초 지표지만, "
        "자산의 현금화 가능성과 수익성까지 보장하지는 않습니다.",
        "자기자본비율의 3년 추세와 현금성자산·차입금 구성을 함께 보세요.",
    ),
    "THREE_YEAR_TREND": (
        "같은 분기끼리 이어 보면 계절성 때문에 생기는 착시를 줄이고, 수익성이나 재무 구조가 "
        "한 번 튄 것인지 방향이 이어지는지 구분할 수 있습니다. 큰 폭의 변화는 좋은 소식으로 "
        "바로 받아들이기보다 기준 변경이나 일회성 요인부터 확인해야 합니다.",
        "변화 폭이 큰 해의 매출, 원가, 재고·채권과 일회성 손익 원인을 공시 주석에서 확인하세요.",
    ),
    "INVENTORY": (
        "평가충당금은 팔리지 않거나 가격이 내려간 재고의 가치를 장부에서 미리 낮춰 잡은 "
        "금액입니다. 비율이 계속 오르면 수요 둔화나 가격 하락 부담의 신호일 수 있습니다.",
        "전년 동기와 직전 분기의 재고 총액, 평가충당금률, 매출 증가율을 같은 기준으로 비교하세요.",
    ),
    "RECEIVABLES": (
        "매출채권은 매출로 기록했지만 아직 현금으로 받지 못한 금액입니다. 오래된 채권 비중이 "
        "늘면 매출이 현금으로 바뀌는 속도가 느려질 수 있습니다.",
        "경과기간별 채권 비중, 대손충당금률, 매출 대비 매출채권 비중의 추세를 보세요.",
    ),
    "CASH_CYCLE": (
        "재고를 사고 판매한 뒤 대금을 회수할 때까지 현금이 묶이는 구조입니다. 재고와 채권이 "
        "매출보다 빨리 늘면 영업이익이 나도 현금 부담이 커질 수 있습니다.",
        "재고·매출채권·매입채무 증감과 영업현금흐름을 한 표에서 비교하세요.",
    ),
    "IMPAIRMENT": (
        "손상과 충당금은 예상 손실을 현재 재무제표에 먼저 반영한 것입니다. 금액 자체보다 "
        "적용 대상과 전기 대비 증가 이유가 핵심입니다.",
        "손상·충당금의 대상 자산, 전기 대비 증감, 환입 여부를 확인하세요.",
    ),
    "LIQUIDITY": (
        "회사가 가까운 시일 안에 갚아야 할 돈을 제때 마련할 수 있는지를 보는 영역입니다.",
        "현금성자산, 유동부채, 1년 내 차입금 만기와 영업현금흐름을 비교하세요.",
    ),
    "CREDIT": (
        "고객이나 거래상대방이 약속한 돈을 갚지 못할 가능성과 회사가 부담한 보증을 보는 영역입니다.",
        "대손충당금, 연체채권, 지급보증 규모와 실제 차입금의 변화를 확인하세요.",
    ),
    "MARKET": (
        "환율·금리·주가 변화가 손익과 자산가치에 미치는 민감도입니다. 위험 노출이 있다는 "
        "사실만으로 재무 상태가 나쁘다는 뜻은 아닙니다.",
        "통화·금리별 민감도와 위험회피 수단, 실제 손익 영향을 함께 확인하세요.",
    ),
    "CONTINGENCY": (
        "소송이나 보증처럼 결과와 금액이 아직 확정되지 않은 잠재 부담입니다.",
        "가능성, 추정 금액, 충당부채 인식 여부와 다음 공시의 변화를 확인하세요.",
    ),
    "CORE_EARNINGS": (
        "반복되는 본업에서 만든 이익인지 보는 영역입니다. 본업 이익이 유지돼야 실적의 "
        "지속성을 설명하기 쉽습니다.",
        "사업부문별 매출·영업이익의 3년 추세를 동일 분기로 비교하세요.",
    ),
    "NON_RECURRING": (
        "자산 처분이나 일회성 수익은 당기 이익을 높일 수 있지만 매년 반복된다고 보기 어렵습니다.",
        "기타수익·비용, 처분손익과 일회성 항목을 제외한 이익을 비교하세요.",
    ),
    "ESTIMATES": (
        "손상과 공정가치는 경영진의 가정이 들어가는 영역이라 현금 거래보다 판단 불확실성이 큽니다.",
        "주요 가정, 민감도, 전기 추정치와 실제 결과의 차이를 확인하세요.",
    ),
    "CASH_ALIGNMENT": (
        "이익과 영업현금흐름이 같은 방향으로 움직이는지 보면 이익이 현금으로 이어지는지 알 수 있습니다.",
        "영업이익과 영업현금흐름의 3년 누적 추세를 비교하세요.",
    ),
    "CONVERTIBLES": (
        "전환권이 행사되면 주식 수가 늘어 기존 주주의 몫이 작아질 수 있습니다.",
        "미전환 수량, 전환가격, 행사기간과 완전희석 주식 수를 확인하세요.",
    ),
    "SHARE_PAY": (
        "주식보상은 현금 지출을 줄일 수 있지만 향후 주식 수를 늘릴 수 있습니다.",
        "미행사 수량, 가득 조건, 행사 가격과 당기 비용을 확인하세요.",
    ),
    "EQUITY_ISSUE": (
        "신주 발행은 자금을 확보하는 대신 기존 주주의 지분율을 낮출 수 있습니다.",
        "발행 주식 수, 조달 목적, 발행가와 조달 자금의 사용 내역을 확인하세요.",
    ),
    "CAPEX": (
        "설비·연구개발 투자는 미래 생산과 경쟁력을 위한 지출이지만 현금을 먼저 사용합니다.",
        "투자액, 영업현금흐름, 감가상각과 신규 생산능력의 연결을 확인하세요.",
    ),
    "DIVIDEND": (
        "배당은 주주에게 현금을 돌려주는 결정입니다. 투자 재원과 차입 부담을 해치지 않고 "
        "지속 가능한지가 중요합니다.",
        "배당성향, 잉여현금흐름, 설비투자와 순차입금 추세를 함께 보세요.",
    ),
    "KAM": (
        "핵심감사사항은 오류가 확정됐다는 뜻이 아니라 감사인이 특히 많은 주의를 기울인 영역입니다.",
        "선정 이유, 감사 절차, 관련 주석과 다음 연도 반복 여부를 확인하세요.",
    ),
    "AUDIT_OPINION": (
        "감사의견은 재무제표가 기준에 따라 작성됐는지를 보는 결론이며, 내부회계 검토의견과는 구분합니다.",
        "의견 유형, 강조사항, 계속기업 관련 문단과 내부회계 의견을 각각 확인하세요.",
    ),
    "ESTIMATION": (
        "회계추정은 미래를 가정해 현재 금액을 정하는 부분이라 가정 변화에 따라 값이 달라질 수 있습니다.",
        "핵심 가정, 민감도, 추정 변경과 손익 영향을 확인하세요.",
    ),
    "CONTROL": (
        "내부통제는 숫자가 만들어지고 검토되는 절차가 제대로 작동하는지를 보는 장치입니다.",
        "중요한 취약점 유무, 개선 조치와 다음 연도 의견을 확인하세요.",
    ),
    "SEGMENT": (
        "어느 사업부가 매출과 이익을 만드는지 알면 회사 전체 실적의 동력을 구분할 수 있습니다.",
        "부문별 매출·이익 비중과 적자 부문의 변화, 내부거래 조정을 확인하세요.",
    ),
    "CUSTOMER": (
        "소수 고객 의존도가 높으면 한 고객의 주문 변화가 회사 실적에 크게 영향을 줄 수 있습니다.",
        "상위 고객 매출 비중, 계약 기간과 고객군 다변화 추세를 확인하세요.",
    ),
    "GEOGRAPHY": (
        "특정 지역 의존도가 높으면 현지 경기·환율·규제 변화의 영향을 크게 받을 수 있습니다.",
        "지역별 매출과 자산 비중, 환율 민감도를 확인하세요.",
    ),
    "SUPPLY": (
        "특정 원재료나 공급처 의존은 조달 차질과 원가 상승으로 이어질 수 있습니다.",
        "핵심 원재료 가격, 주요 공급처 비중과 대체 조달 계획을 확인하세요.",
    ),
}


class FinancialLiteracyTutorAgent:
    """Translate verified findings into plain-language learning cues."""

    name = "FINANCIAL_LITERACY_TUTOR"

    def supports(self, result: RagResult) -> bool:
        return result.analysis_report is not None

    def run(self, result: RagResult) -> RagResult:
        report = result.analysis_report
        if report is None:
            return result
        findings = []
        for finding in report.findings:
            plain_language, next_check = _TUTOR_GUIDANCE.get(
                finding.category_key,
                (
                    "이 항목은 한 시점의 숫자보다 같은 기준의 기간별 변화와 함께 읽어야 의미가 분명해집니다.",
                    "직전 분기와 전년 동기의 동일 항목을 같은 연결·누적 기준으로 비교하세요.",
                ),
            )
            findings.append(
                finding.model_copy(
                    update={
                        "plain_language": plain_language,
                        "next_check": next_check,
                    }
                )
            )
        return result.model_copy(
            update={"analysis_report": report.model_copy(update={"findings": findings})}
        )


def default_value_scope(metric: Metric | None) -> ValueScope | None:
    if metric in {Metric.TOTAL_ASSETS, Metric.TOTAL_LIABILITIES, Metric.TOTAL_EQUITY}:
        return ValueScope.POINT_IN_TIME
    if metric is not None:
        return ValueScope.CUMULATIVE
    return None


def required_missing_slots(intent: Intent, slots: Slots) -> list[str]:
    missing: list[str] = []
    if intent == Intent.DIRECT_FACT:
        for name in ("company", "metric"):
            if getattr(slots, name) is None:
                missing.append(name)
        if slots.period is None and slots.period_mode is None:
            missing.append("period")
    elif intent == Intent.COMPARE:
        for name in ("company", "comparison_company", "metric"):
            if getattr(slots, name) is None:
                missing.append(name)
        if slots.period is None and slots.period_mode is None:
            missing.append("period_mode")
    elif intent == Intent.CLAIM_CHECK:
        if slots.company is None:
            missing.append("company")
        if not slots.topic_or_claim:
            missing.append("topic_or_claim")
        elif financial_diagnosis_lens_for(slots.topic_or_claim) and slots.period is None:
            missing.append("period")
    return missing


_QUESTIONS = {
    "company": "어느 회사에 대한 질문인가요? 삼성전자 또는 SK하이닉스를 알려주세요.",
    "comparison_company": "어느 회사와 비교할까요?",
    "metric": "매출액, 영업이익, 당기순이익, 자산·부채·자본총계, 영업현금흐름 중 어떤 지표를 확인할까요?",
    "period": "어느 연도 또는 보고기간을 확인할까요?",
    "period_mode": "가장 최근에 두 회사가 모두 공시한 동일 기간 기준으로 비교할까요?",
    "topic_or_claim": "확인하려는 공시 내용이나 주장을 한 문장으로 알려주세요.",
}

_REPORT_PERIOD_SUFFIX = {
    "11011": "",
    "11013": "Q1",
    "11012": "H1",
    "11014": "Q3",
}


_SIGNED_AMOUNT_CELL = re.compile(
    r"^\s*(?P<open>\()?\s*(?P<minus>[-−])?\s*"
    r"(?P<number>[1-9][0-9]{0,2}(?:,[0-9]{3})+)\s*"
    r"(?P<close>\))?\s*(?:백만원|억원|원)?\s*$"
)


def _narrative_retrieval_text(
    claim: str,
    company: Company | None = None,
) -> str:
    if re.search(r"\bDS\b|DS\s*부문", claim, re.IGNORECASE) and re.search(
        r"영업이익|적자|흑자|손실",
        claim,
    ):
        return (
            f"{claim} DS 부문 영업이익 부문별 요약 재무 현황 "
            "주석 30 부문별 보고"
        )
    if "연구개발" in claim or re.search(r"\bR\s*&\s*D\b", claim, re.IGNORECASE):
        return (
            f"{claim} 연구개발 활동 연구개발비용 연구개발 조직 "
            "연구개발 실적 주요계약 및 연구개발활동"
        )
    lens = disclosure_lens_for(claim)
    if lens is not None and company is not None:
        terms = company_context_terms(company, lens.key)
        if terms:
            return f"{claim} {' '.join(terms)}"
    return claim


def _narrative_hit_matches_topic(claim: str, evidence) -> bool:
    context = "\n".join(
        part for part in (evidence.section_path, evidence.excerpt) if part
    )
    if "연구개발" in claim or re.search(r"\bR\s*&\s*D\b", claim, re.IGNORECASE):
        return "연구개발" in context or bool(
            re.search(r"\bR\s*&\s*D\b", context, re.IGNORECASE)
        )
    if re.search(r"\bDS\b|DS\s*부문", claim, re.IGNORECASE) and re.search(
        r"영업이익|적자|흑자|손실",
        claim,
    ):
        return bool(re.search(r"\bDS\b|DS\s*부문", context, re.IGNORECASE)) and (
            "영업이익" in context
        )
    return True


def _amount_sign(value: str) -> int | None:
    match = _SIGNED_AMOUNT_CELL.fullmatch(value)
    if not match:
        return None
    paired_parentheses = bool(match.group("open") and match.group("close"))
    return -1 if match.group("minus") or paired_parentheses else 1


def _ds_operating_profit_signs(context: str) -> list[int]:
    """Extract DS signs only when the amount is structurally attributable."""

    signs: list[int] = []
    amount_token = r"\(?\s*[-−]?\s*[1-9][0-9]{0,2}(?:,[0-9]{3})+\s*\)?(?:백만원|억원|원)?"
    explicit_patterns = (
        rf"DS[ \t]*(?:부문)?[ \t]*(?:의)?[ \t]*영업이익(?:은|금액은)?"
        rf"[ \t:：]{{0,16}}(?P<amount>{amount_token})",
        rf"영업이익(?:은|금액은)?[ \t:：]{{0,16}}DS[ \t]*부문(?:의)?"
        rf"[ \t:：]{{0,16}}(?P<amount>{amount_token})",
        rf"부문[ \t]*=[ \t]*DS[ \t]*\|[ \t]*영업이익[ \t]*=[ \t]*"
        rf"(?P<amount>{amount_token})",
    )
    for pattern in explicit_patterns:
        match = re.search(pattern, context, re.IGNORECASE)
        if match:
            sign = _amount_sign(match.group("amount"))
            if sign is not None:
                signs.append(sign)

    rows = [
        [cell.strip() for cell in line.split("|")]
        for line in context.splitlines()
        if "|" in line
    ]

    # Row-oriented table: `DS | 영업이익 | 24,858,075`.
    for cells in rows:
        ds_indexes = [
            index
            for index, cell in enumerate(cells)
            if re.fullmatch(r"DS(?:\s*부문)?", cell, re.IGNORECASE)
        ]
        metric_indexes = [
            index for index, cell in enumerate(cells) if "영업이익" in cell
        ]
        if not ds_indexes or not metric_indexes:
            continue
        candidate_signs = [
            sign
            for cell in cells[max(metric_indexes) + 1 :]
            if (sign := _amount_sign(cell)) is not None
        ]
        if len(candidate_signs) == 1:
            signs.extend(candidate_signs)

    # Matrix table: header `구분 | DX | DS`, metric row
    # `영업이익 | 10,000 | (5,000)`. Column position is the evidence link.
    for header in rows:
        ds_columns = [
            index
            for index, cell in enumerate(header)
            if re.fullmatch(r"DS(?:\s*부문)?", cell, re.IGNORECASE)
        ]
        for column in ds_columns:
            for metric_row in rows:
                if (
                    metric_row is header
                    or not metric_row
                    or "영업이익" not in metric_row[0]
                    or column >= len(metric_row)
                ):
                    continue
                sign = _amount_sign(metric_row[column])
                if sign is not None:
                    signs.append(sign)
    return signs


class MultiTurnScenarioAgent:
    def run(self, *, intent: Intent, slots: Slots) -> ScenarioResult:
        values = slots.model_dump()
        values["fs_div"] = FsDiv.CFS
        values["value_scope"] = slots.value_scope or default_value_scope(slots.metric)
        values["as_of"] = slots.as_of or datetime.now(UTC)
        normalized_slots = Slots.model_validate(values)
        missing = required_missing_slots(intent, normalized_slots)
        if missing:
            return ScenarioResult(
                workflow_status=WorkflowStatus.NEED_MORE_INFO,
                intent=intent,
                collected_slots=normalized_slots,
                missing_slots=missing,
                next_question=_QUESTIONS[missing[0]],
            )

        query = build_normalized_query(intent, normalized_slots)
        return ScenarioResult(
            workflow_status=WorkflowStatus.READY_FOR_RAG,
            intent=intent,
            collected_slots=normalized_slots,
            missing_slots=[],
            normalized_query=query,
        )


def build_normalized_query(intent: Intent, slots: Slots) -> NormalizedQuery:
    missing = required_missing_slots(intent, slots)
    if missing:
        raise ValueError(f"cannot normalize query with missing slots: {missing}")
    companies = [slots.company]
    if slots.comparison_company is not None:
        companies.append(slots.comparison_company)
    period = slots.period
    if period is not None and re.fullmatch(r"20\d{2}", period) and len(slots.report_codes) == 1:
        suffix = _REPORT_PERIOD_SUFFIX.get(slots.report_codes[0])
        if suffix is not None:
            period = f"{period}{suffix}"
    return NormalizedQuery(
        intent=intent,
        companies=[company for company in companies if company is not None],
        metric=slots.metric,
        period=period,
        period_mode=slots.period_mode,
        value_scope=slots.value_scope or default_value_scope(slots.metric),
        fs_div=slots.fs_div,
        topic_or_claim=slots.topic_or_claim or slots.user_claim,
        claim_operator=slots.claim_operator,
        as_of=slots.as_of or datetime.now(UTC),
    )


class RagFaqAgent:
    def __init__(
        self,
        *,
        financials: FinancialRepository,
        evidence: EvidenceRepository,
        embeddings: EmbeddingPort,
        minimum_score: float = 0.78,
        top_k: int = 5,
    ) -> None:
        self._financials = financials
        self._evidence = evidence
        self._embeddings = embeddings
        self._minimum_score = minimum_score
        self._top_k = top_k
        self._financial_diagnosis = FinancialDiagnosisAgent(
            financials=financials,
            evidence=evidence,
        )

    async def run(self, query: NormalizedQuery) -> RagResult:
        try:
            if self._financial_diagnosis.supports(query):
                return await self._financial_diagnosis.run(query)
            if query.intent in {Intent.DIRECT_FACT, Intent.COMPARE}:
                return await self._run_financial(query)
            return await self._run_narrative(query)
        except RepositoryUnavailable:
            return RagResult(
                workflow_status=WorkflowStatus.SYSTEM_ERROR,
                verdict=Verdict.INSUFFICIENT_INFO,
                normalized_query=query,
                limitations=["공시 저장소를 조회할 수 없어 수치나 사실을 추측하지 않았습니다."],
                unavailable_reason=UnavailableReason.REPOSITORY_UNAVAILABLE,
                searched_until=query.as_of,
            )

    async def _run_financial(self, query: NormalizedQuery) -> RagResult:
        assert query.metric is not None
        value_scope = query.value_scope or default_value_scope(query.metric)
        assert value_scope is not None

        facts: list[FinancialFact] = []
        if query.intent == Intent.COMPARE:
            left, right = query.companies
            pair: tuple[FinancialFact, FinancialFact] | None
            if query.period_mode in {
                PeriodMode.LATEST_COMMON,
                PeriodMode.LATEST_COMMON_INTERIM,
            }:
                pair = await self._financials.get_latest_common_facts(
                    companies=(left, right),
                    metric=query.metric,
                    value_scope=value_scope,
                    fs_div=query.fs_div,
                    as_of=query.as_of,
                    interim_only=(
                        query.period_mode == PeriodMode.LATEST_COMMON_INTERIM
                    ),
                )
            elif query.period is not None:
                first = await self._financials.get_fact(
                    company=left,
                    metric=query.metric,
                    period=query.period,
                    value_scope=value_scope,
                    fs_div=query.fs_div,
                    as_of=query.as_of,
                )
                second = await self._financials.get_fact(
                    company=right,
                    metric=query.metric,
                    period=query.period,
                    value_scope=value_scope,
                    fs_div=query.fs_div,
                    as_of=query.as_of,
                )
                pair = (first, second) if first and second else None
            else:
                pair = None
            if pair:
                facts.extend(pair)
        else:
            assert query.period is not None
            fact = await self._financials.get_fact(
                company=query.companies[0],
                metric=query.metric,
                period=query.period,
                value_scope=value_scope,
                fs_div=query.fs_div,
                as_of=query.as_of,
            )
            if fact:
                facts.append(fact)

        if not facts or (query.intent == Intent.COMPARE and len(facts) != 2):
            return RagResult(
                workflow_status=WorkflowStatus.UNAVAILABLE,
                verdict=Verdict.NO_OFFICIAL_EVIDENCE,
                normalized_query=query,
                limitations=["고정된 OpenDART 적재 범위에서 동일 조건의 재무 수치를 찾지 못했습니다."],
                unavailable_reason=UnavailableReason.NO_OFFICIAL_EVIDENCE,
                searched_until=query.as_of,
            )

        evidence = await self._evidence.get_by_ids([fact.evidence_id for fact in facts])
        if len(evidence) != len(facts) or any(
            item.evidence_status != EvidenceStatus.OFFICIAL_API_VERIFIED for item in evidence
        ):
            return RagResult(
                workflow_status=WorkflowStatus.UNAVAILABLE,
                verdict=Verdict.INSUFFICIENT_INFO,
                normalized_query=query,
                limitations=["공식 API 검증 상태가 아닌 자료는 사실 판정에 사용하지 않았습니다."],
                unavailable_reason=UnavailableReason.SOURCE_OUT_OF_SCOPE,
                searched_until=query.as_of,
            )
        comparison_basis = None
        verdict = Verdict.CONFIRMED
        if query.intent == Intent.COMPARE:
            comparison_basis = self._comparison_basis(facts)
            if not comparison_basis.is_valid:
                return RagResult(
                    workflow_status=WorkflowStatus.UNAVAILABLE,
                    verdict=Verdict.INSUFFICIENT_INFO,
                    normalized_query=query,
                    facts=facts,
                    comparison_basis=comparison_basis,
                    evidence=evidence,
                    limitations=["기간·누적·연결·통화 기준이 일치하지 않아 비교하지 않았습니다."],
                    unavailable_reason=UnavailableReason.PERIOD_OR_BASIS_MISMATCH,
                    searched_until=query.as_of,
                )
            verdict = self._comparison_verdict(query.claim_operator, facts[0].value, facts[1].value)

        return RagResult(
            workflow_status=WorkflowStatus.ANSWER_READY,
            verdict=verdict,
            normalized_query=query,
            facts=facts,
            comparison_basis=comparison_basis,
            evidence=evidence,
            searched_filing_ids=[item.rcept_no for item in evidence],
            searched_until=query.as_of,
        )

    async def _run_narrative(self, query: NormalizedQuery) -> RagResult:
        claim = query.topic_or_claim or ""
        embedding = await self._embeddings.embed(
            _narrative_retrieval_text(claim, query.companies[0])
        )
        search = await self._evidence.search(
            company=query.companies[0],
            period=query.period,
            query_text=claim,
            embedding=embedding,
            as_of=query.as_of,
            limit=self._top_k,
        )
        scored_hits = [hit for hit in search.hits if hit.score >= self._minimum_score]
        official_hits = [
            hit
            for hit in scored_hits
            if hit.evidence.evidence_status == EvidenceStatus.OFFICIAL_API_VERIFIED
        ]
        if scored_hits and not official_hits:
            return RagResult(
                workflow_status=WorkflowStatus.UNAVAILABLE,
                verdict=Verdict.INSUFFICIENT_INFO,
                normalized_query=query,
                limitations=[
                    "검색 결과가 합성 또는 미검증 자료뿐이어서 사실 판정에 사용하지 않았습니다.",
                    f"검색 범위: {search.corpus_scope}",
                ],
                searched_filing_ids=search.searched_filing_ids,
                searched_until=search.searched_until,
                unavailable_reason=UnavailableReason.SOURCE_OUT_OF_SCOPE,
            )
        safe_official_hits = [
            hit
            for hit in official_hits
            if not contains_untrusted_instruction(
                "\n".join(
                    part
                    for part in (
                        hit.evidence.section_path,
                        hit.evidence.excerpt,
                    )
                    if part
                )
            )
        ]
        filtered_instruction_count = len(official_hits) - len(safe_official_hits)
        if official_hits and not safe_official_hits:
            return RagResult(
                workflow_status=WorkflowStatus.UNAVAILABLE,
                verdict=Verdict.INSUFFICIENT_INFO,
                normalized_query=query,
                limitations=[
                    "문서 안의 실행 지시로 보이는 문자열을 근거에서 제외했습니다.",
                    f"검색 범위: {search.corpus_scope}",
                ],
                searched_filing_ids=search.searched_filing_ids,
                searched_until=search.searched_until,
                unavailable_reason=UnavailableReason.SOURCE_OUT_OF_SCOPE,
            )
        hits = [
            hit
            for hit in safe_official_hits
            if _narrative_hit_matches_topic(claim, hit.evidence)
        ]
        if not hits:
            if not search.corpus_complete:
                return RagResult(
                    workflow_status=WorkflowStatus.UNAVAILABLE,
                    verdict=Verdict.INSUFFICIENT_INFO,
                    normalized_query=query,
                    limitations=[
                        "적재 corpus가 완전하지 않아 근거 부재를 판정하지 않았습니다.",
                        f"검색 범위: {search.corpus_scope}",
                    ],
                    searched_filing_ids=search.searched_filing_ids,
                    searched_until=search.searched_until,
                    unavailable_reason=UnavailableReason.CORPUS_INCOMPLETE,
                )
            return RagResult(
                workflow_status=WorkflowStatus.UNAVAILABLE,
                verdict=Verdict.NO_OFFICIAL_EVIDENCE,
                normalized_query=query,
                limitations=[
                    "조회한 공시에서 직접 근거를 찾지 못했습니다. 이는 해당 주장이 거짓이라는 뜻이 아닙니다.",
                    f"검색 범위: {search.corpus_scope}",
                ],
                searched_filing_ids=search.searched_filing_ids,
                searched_until=search.searched_until,
                unavailable_reason=UnavailableReason.NO_OFFICIAL_EVIDENCE,
            )
        evidence = [hit.evidence for hit in hits]
        verdict = self._narrative_verdict(claim, [hit.evidence for hit in hits])
        terminal = verdict in {Verdict.CONFIRMED, Verdict.CONTRADICTED}
        return RagResult(
            workflow_status=(
                WorkflowStatus.ANSWER_READY
                if terminal
                else WorkflowStatus.UNAVAILABLE
            ),
            verdict=verdict,
            normalized_query=query,
            evidence=evidence,
            searched_filing_ids=search.searched_filing_ids,
            searched_until=search.searched_until,
            limitations=[
                f"검색 범위: {search.corpus_scope}",
                *(
                    [
                        f"문서 안의 실행 지시로 보이는 근거 {filtered_instruction_count}건을 제외했습니다."
                    ]
                    if filtered_instruction_count
                    else []
                ),
                *(
                    []
                    if search.corpus_complete
                    else ["관련 근거는 확인했지만 narrative corpus는 부분 적재 상태입니다."]
                ),
                *(
                    []
                    if terminal
                    else ["의미상 유사한 공시 문장은 찾았지만 입력 주장을 직접 입증하지는 못했습니다."]
                ),
            ],
            unavailable_reason=(
                None if terminal else UnavailableReason.ENTAILMENT_UNPROVEN
            ),
        )

    @staticmethod
    def _narrative_verdict(claim: str, evidence: Sequence) -> Verdict:
        negative_profit_claim = bool(re.search(r"(적자|손실|마이너스)", claim))
        positive_profit_claim = bool(re.search(r"(흑자|플러스|양\s*\(\+\))", claim))
        claim_year_match = re.search(r"\b(20\d{2})(?:년)?\b", claim)
        claim_year = int(claim_year_match.group(1)) if claim_year_match else None

        observed_signs: list[int] = []
        for item in evidence:
            context = "\n".join(
                part for part in (item.section_path, item.excerpt) if part
            )
            if not re.search(r"\bDS\b|DS\s*부문", context, re.IGNORECASE):
                continue
            if claim_year is not None and item.business_year not in {None, claim_year}:
                continue
            observed_signs.extend(_ds_operating_profit_signs(context))

        if negative_profit_claim and observed_signs:
            unique_signs = set(observed_signs)
            if unique_signs == {-1}:
                return Verdict.CONFIRMED
            if unique_signs == {1}:
                return Verdict.CONTRADICTED
        if positive_profit_claim and observed_signs:
            unique_signs = set(observed_signs)
            if unique_signs == {1}:
                return Verdict.CONFIRMED
            if unique_signs == {-1}:
                return Verdict.CONTRADICTED
        # A semantic nearest neighbour is not an entailment proof. The M0 gate
        # only gives a terminal verdict to the explicit profit-sign rule above
        # or to an information-seeking FAQ whose answer is presented as source
        # evidence. Other factual assertions remain non-terminal.
        information_question = bool(
            re.search(r"(무엇|어떤|알려|찾아|검색|근거|주요\s*사업|어떻게|얼마)", claim)
        )
        assertion_cue = bool(re.search(r"(사실|소문|맞아|했다|했대|이라던데)", claim))
        if information_question and not assertion_cue:
            return Verdict.CONFIRMED
        return Verdict.PARTIALLY_CONFIRMED

    @staticmethod
    def _comparison_basis(facts: Sequence[FinancialFact]) -> ComparisonBasis:
        left, right = facts
        return ComparisonBasis(
            same_period=left.period == right.period,
            same_value_scope=left.value_scope == right.value_scope,
            same_fs_div=left.fs_div == right.fs_div,
            same_currency=left.currency == right.currency and left.unit == right.unit,
        )

    @staticmethod
    def _comparison_verdict(
        operator: ClaimOperator | None,
        left: Decimal,
        right: Decimal,
    ) -> Verdict:
        if operator is None:
            return Verdict.CONFIRMED
        matches = {
            ClaimOperator.LT: left < right,
            ClaimOperator.LTE: left <= right,
            ClaimOperator.EQ: left == right,
            ClaimOperator.GTE: left >= right,
            ClaimOperator.GT: left > right,
        }
        return Verdict.CONFIRMED if matches[operator] else Verdict.CONTRADICTED


class EvidenceGate:
    """Mechanical gate; prompts cannot override these invariants."""

    def validate_rag(self, result: RagResult) -> None:
        if result.workflow_status == WorkflowStatus.SYSTEM_ERROR:
            return
        evidence_id_list = [item.evidence_id for item in result.evidence]
        evidence_ids = set(evidence_id_list)
        fact_id_list = [fact.evidence_id for fact in result.facts]
        if len(evidence_id_list) != len(evidence_ids):
            raise EvidenceValidationError("duplicate evidence IDs are not allowed")
        if len(fact_id_list) != len(set(fact_id_list)):
            raise EvidenceValidationError("duplicate financial fact IDs are not allowed")
        missing = [evidence_id for evidence_id in fact_id_list if evidence_id not in evidence_ids]
        if missing:
            raise EvidenceValidationError(f"facts reference missing evidence: {missing}")
        terminal_verdicts = {Verdict.CONFIRMED, Verdict.CONTRADICTED}
        if result.verdict in terminal_verdicts:
            if result.workflow_status != WorkflowStatus.ANSWER_READY:
                raise EvidenceValidationError("a terminal verdict requires ANSWER_READY")
            if not result.evidence:
                raise EvidenceValidationError("a terminal verdict requires evidence")
            if any(
                item.evidence_status != EvidenceStatus.OFFICIAL_API_VERIFIED
                for item in result.evidence
            ):
                raise EvidenceValidationError(
                    "a terminal verdict requires only official API evidence"
                )
        if (
            result.workflow_status == WorkflowStatus.ANSWER_READY
            and result.verdict not in terminal_verdicts
        ):
            raise EvidenceValidationError(
                "ANSWER_READY requires CONFIRMED or CONTRADICTED"
            )
        if result.comparison_basis is not None and not result.comparison_basis.is_valid:
            if result.workflow_status == WorkflowStatus.ANSWER_READY:
                raise EvidenceValidationError("basis mismatch cannot produce ANSWER_READY")
        if result.verdict == Verdict.NO_OFFICIAL_EVIDENCE and result.facts:
            raise EvidenceValidationError("NO_OFFICIAL_EVIDENCE cannot include financial facts")
        if result.analysis_report is not None:
            finding_ids = {
                evidence_id
                for finding in result.analysis_report.findings
                for evidence_id in finding.evidence_ids
            }
            if not finding_ids.issubset(evidence_ids):
                raise EvidenceValidationError(
                    "analysis findings reference evidence outside the retrieval allowlist"
                )

    def validate_claims(self, claims: Sequence[AnswerClaim], evidence_ids: set[str]) -> None:
        for claim in claims:
            if claim.kind == ClaimKind.FACT and not set(claim.evidence_ids).issubset(evidence_ids):
                raise EvidenceValidationError("a factual answer claim escaped its evidence allowlist")


def financial_claims(facts: Sequence[FinancialFact]) -> list[AnswerClaim]:
    claims: list[AnswerClaim] = []
    for fact in facts:
        value = format(fact.value, ",f")
        claims.append(
            AnswerClaim(
                text=(
                    f"{fact.company.value} {fact.period} {fact.metric.value}: "
                    f"{value} {fact.unit} ({fact.fs_div.value}, {fact.value_scope.value})"
                ),
                kind=ClaimKind.FACT,
                evidence_ids=[fact.evidence_id],
            )
        )
    return claims


def narrative_claims(result: RagResult) -> list[AnswerClaim]:
    if result.verdict not in {Verdict.CONFIRMED, Verdict.CONTRADICTED}:
        return []
    return [
        AnswerClaim(
            text=item.excerpt or f"{item.filing_title}에서 관련 근거를 확인했습니다.",
            kind=ClaimKind.FACT,
            evidence_ids=[item.evidence_id],
        )
        for item in result.evidence
    ]
