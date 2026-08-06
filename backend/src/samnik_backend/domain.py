from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .financial_contract import STANDARD_METRICS, SUPPORTED_REPORT_CODES


class Intent(StrEnum):
    DIRECT_FACT = "DIRECT_FACT"
    CLAIM_CHECK = "CLAIM_CHECK"
    COMPARE = "COMPARE"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"


class Route(StrEnum):
    RAG = "RAG"
    SCENARIO = "SCENARIO"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"


class WorkflowStatus(StrEnum):
    NEED_MORE_INFO = "NEED_MORE_INFO"
    READY_FOR_RAG = "READY_FOR_RAG"
    ANSWER_READY = "ANSWER_READY"
    UNAVAILABLE = "UNAVAILABLE"
    SYSTEM_ERROR = "SYSTEM_ERROR"


class Verdict(StrEnum):
    CONFIRMED = "CONFIRMED"
    PARTIALLY_CONFIRMED = "PARTIALLY_CONFIRMED"
    CONTRADICTED = "CONTRADICTED"
    NO_OFFICIAL_EVIDENCE = "NO_OFFICIAL_EVIDENCE"
    INSUFFICIENT_INFO = "INSUFFICIENT_INFO"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"


class UnavailableReason(StrEnum):
    NO_OFFICIAL_EVIDENCE = "NO_OFFICIAL_EVIDENCE"
    CORPUS_INCOMPLETE = "CORPUS_INCOMPLETE"
    PERIOD_OR_BASIS_MISMATCH = "PERIOD_OR_BASIS_MISMATCH"
    SOURCE_OUT_OF_SCOPE = "SOURCE_OUT_OF_SCOPE"
    STALE_CACHE = "STALE_CACHE"
    REPOSITORY_UNAVAILABLE = "REPOSITORY_UNAVAILABLE"
    MODEL_PROVIDER_UNAVAILABLE = "MODEL_PROVIDER_UNAVAILABLE"
    ENTAILMENT_UNPROVEN = "ENTAILMENT_UNPROVEN"


class Company(StrEnum):
    SAMSUNG_ELECTRONICS = "SAMSUNG_ELECTRONICS"
    SK_HYNIX = "SK_HYNIX"


class Metric(StrEnum):
    REVENUE = "revenue"
    OPERATING_PROFIT = "operating_profit"
    NET_INCOME = "net_income"
    TOTAL_ASSETS = "total_assets"
    TOTAL_LIABILITIES = "total_liabilities"
    TOTAL_EQUITY = "total_equity"
    OPERATING_CASH_FLOW = "operating_cash_flow"


class PeriodMode(StrEnum):
    LATEST_COMMON = "LATEST_COMMON"
    LATEST_COMMON_INTERIM = "LATEST_COMMON_INTERIM"


class ValueScope(StrEnum):
    CUMULATIVE = "CUMULATIVE"
    POINT_IN_TIME = "POINT_IN_TIME"
    QUARTER_ONLY = "QUARTER_ONLY"
    YTD = "YTD"
    ANNUAL = "ANNUAL"


class FsDiv(StrEnum):
    CFS = "CFS"
    OFS = "OFS"


class ClaimOperator(StrEnum):
    LT = "LT"
    LTE = "LTE"
    EQ = "EQ"
    GTE = "GTE"
    GT = "GT"


class ClaimKind(StrEnum):
    FACT = "FACT"
    POLICY = "POLICY"
    LIMITATION = "LIMITATION"


class EvidenceStatus(StrEnum):
    OFFICIAL_API_VERIFIED = "OFFICIAL_API_VERIFIED"
    SYNTHETIC_UNVERIFIED = "SYNTHETIC_UNVERIFIED"


class Actor(StrEnum):
    SUPERVISOR = "SUPERVISOR"
    RAG_AGENT = "RAG_AGENT"
    SCENARIO_AGENT = "SCENARIO_AGENT"
    EVIDENCE_GATE = "EVIDENCE_GATE"
    ANALYSIS_AGENT = "ANALYSIS_AGENT"
    SYSTEM = "SYSTEM"


class EventType(StrEnum):
    TURN_STARTED = "turn.started"
    SUPERVISOR_ROUTED = "supervisor.route.selected"
    AGENT_STARTED = "agent.started"
    AGENT_COMPLETED = "agent.completed"
    STATE_UPDATED = "workflow.state.updated"
    EVIDENCE_CHECKED = "evidence.checked"
    ANSWER_COMPLETED = "supervisor.answer.completed"
    TURN_FAILED = "turn.failed"


class Slots(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company: Company | None = None
    comparison_company: Company | None = None
    metric: Metric | None = None
    account_query: str | None = None
    start_period: str | None = None
    end_period: str | None = None
    report_codes: list[str] = Field(default_factory=list)
    period: str | None = None
    period_mode: PeriodMode | None = None
    value_scope: ValueScope | None = None
    fs_div: FsDiv = FsDiv.CFS
    topic_or_claim: str | None = None
    user_claim: str | None = None
    claim_operator: ClaimOperator | None = None
    as_of: datetime | None = None

    def merged(self, newer: "Slots") -> "Slots":
        values = self.model_dump()
        for key, value in newer.model_dump().items():
            if value is not None:
                values[key] = value
        return Slots.model_validate(values)


class RouteDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Intent
    extracted_slots: Slots = Field(default_factory=Slots)
    rationale_code: str = "CLASSIFIED"


class NormalizedQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Intent
    companies: list[Company]
    metric: Metric | None = None
    period: str | None = None
    period_mode: PeriodMode | None = None
    value_scope: ValueScope | None = None
    fs_div: FsDiv = FsDiv.CFS
    topic_or_claim: str | None = None
    claim_operator: ClaimOperator | None = None
    as_of: datetime

    @model_validator(mode="after")
    def validate_shape(self) -> "NormalizedQuery":
        if self.intent == Intent.COMPARE and len(self.companies) != 2:
            raise ValueError("COMPARE requires exactly two companies")
        if self.intent == Intent.DIRECT_FACT and (len(self.companies) != 1 or self.metric is None):
            raise ValueError("DIRECT_FACT requires one company and a metric")
        if self.intent == Intent.CLAIM_CHECK and not self.topic_or_claim:
            raise ValueError("CLAIM_CHECK requires topic_or_claim")
        return self


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    company: Company
    business_year: int | None = Field(default=None, ge=1999, le=2100)
    rcept_no: str
    filing_title: str
    filed_at: date
    url: str
    excerpt: str | None = None
    section_path: str | None = None
    source_kind: Literal["FINANCIAL_FACT", "FILING_CHUNK"]
    evidence_status: EvidenceStatus = EvidenceStatus.OFFICIAL_API_VERIFIED


class FinancialFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company: Company
    metric: Metric
    value: Decimal
    currency: str = "KRW"
    unit: str = "KRW"
    period: str
    value_scope: ValueScope
    fs_div: FsDiv = FsDiv.CFS
    evidence_id: str
    source_raw_amount: str | None = None
    source_unit_scale: int = Field(default=1, gt=0)
    period_start: date | None = None
    period_end: date | None = None
    report_code: str | None = None
    section: str | None = None


class EvidenceHit(BaseModel):
    evidence: Evidence
    score: float = Field(ge=0.0, le=1.0)


class EvidenceSearchResult(BaseModel):
    hits: list[EvidenceHit] = Field(default_factory=list)
    searched_filing_ids: list[str] = Field(default_factory=list)
    corpus_scope: str
    corpus_complete: bool
    searched_until: datetime


class AnalysisFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category_key: str
    category_label: str
    summary: str
    plain_language: str | None = None
    next_check: str | None = None
    evidence_ids: list[str] = Field(min_length=1)


class AnalysisReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lens_key: str
    lens_label: str
    methodology: Literal[
        "CONTEXTUAL_DISCLOSURE_ANALYSIS_V2",
        "FINANCIAL_RATIO_CALCULATION_V1",
    ] = "CONTEXTUAL_DISCLOSURE_ANALYSIS_V2"
    findings: list[AnalysisFinding] = Field(min_length=1, max_length=4)
    source_count: int = Field(ge=1)
    limitation: str


class ComparisonBasis(BaseModel):
    same_period: bool
    same_value_scope: bool
    same_fs_div: bool
    same_currency: bool

    @property
    def is_valid(self) -> bool:
        return all(
            (
                self.same_period,
                self.same_value_scope,
                self.same_fs_div,
                self.same_currency,
            )
        )


class ScenarioResult(BaseModel):
    workflow_status: Literal[
        WorkflowStatus.NEED_MORE_INFO,
        WorkflowStatus.READY_FOR_RAG,
    ]
    intent: Intent
    collected_slots: Slots
    missing_slots: list[str] = Field(default_factory=list)
    next_question: str | None = None
    normalized_query: NormalizedQuery | None = None


class RagResult(BaseModel):
    workflow_status: WorkflowStatus
    verdict: Verdict
    normalized_query: NormalizedQuery
    facts: list[FinancialFact] = Field(default_factory=list)
    comparison_basis: ComparisonBasis | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    searched_filing_ids: list[str] = Field(default_factory=list)
    searched_until: datetime | None = None
    unavailable_reason: UnavailableReason | None = None
    stale_data_warning: str | None = None
    analysis_report: AnalysisReport | None = None


class AnswerDraft(BaseModel):
    conclusion: str


class AnswerClaim(BaseModel):
    text: str
    kind: ClaimKind
    evidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def fact_requires_evidence(self) -> "AnswerClaim":
        if self.kind == ClaimKind.FACT and not self.evidence_ids:
            raise ValueError("FACT claims require at least one evidence_id")
        return self


class FinalAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: Literal[Actor.SUPERVISOR] = Actor.SUPERVISOR
    workflow_status: WorkflowStatus
    verdict: Verdict
    conclusion: str
    facts: list[FinancialFact] = Field(default_factory=list)
    comparison_basis: ComparisonBasis | None = None
    claims: list[AnswerClaim] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    unavailable_reason: UnavailableReason | None = None
    stale_data_warning: str | None = None
    analysis_report: AnalysisReport | None = None
    as_of: datetime
    route: Route


class ConversationRecord(BaseModel):
    conversation_id: str
    intent: Intent | None = None
    collected_slots: Slots = Field(default_factory=Slots)
    missing_slots: list[str] = Field(default_factory=list)
    workflow_status: WorkflowStatus | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ChatTurnContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    companies: list[Company] = Field(default_factory=list)
    business_year: int | None = Field(
        default=None,
        ge=2015,
        alias="year",
        validation_alias=AliasChoices("business_year", "year"),
    )
    report_code: str | None = None
    metric_key: str | None = None
    fs_div: FsDiv = FsDiv.CFS

    @field_validator("companies")
    @classmethod
    def validate_companies(cls, value: list[Company]) -> list[Company]:
        if len(value) > 2:
            raise ValueError("companies must contain at most two items")
        if len(set(value)) != len(value):
            raise ValueError("companies must be unique")
        return value

    @field_validator("report_code")
    @classmethod
    def validate_report_code(cls, value: str | None) -> str | None:
        if value is not None and value not in SUPPORTED_REPORT_CODES:
            raise ValueError("unsupported report code")
        return value

    @field_validator("metric_key")
    @classmethod
    def validate_metric_key(cls, value: str | None) -> str | None:
        if value is not None and value not in STANDARD_METRICS:
            raise ValueError("metric_key must be a supported standard metric")
        return value

    def to_slots(self) -> Slots:
        values: dict[str, object] = {}
        if self.companies:
            values["company"] = self.companies[0]
            if len(self.companies) > 1:
                values["comparison_company"] = self.companies[1]
        if self.business_year is not None:
            values["period"] = str(self.business_year)
        if self.metric_key is not None:
            values["metric"] = Metric(self.metric_key)
        if self.report_code is not None:
            values["report_codes"] = [self.report_code]
        if self.fs_div is not None:
            values["fs_div"] = self.fs_div
        return Slots.model_validate(values)


class ChatTurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str | None = Field(default=None, min_length=1, max_length=128)
    client_request_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=4000)
    context: ChatTurnContext | None = None

    @field_validator("client_request_id")
    @classmethod
    def strip_client_request_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("client_request_id cannot be blank")
        return value

    @field_validator("message")
    @classmethod
    def strip_message(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message cannot be blank")
        return value


class WorkflowEvent(BaseModel):
    sequence: int = Field(ge=1)
    event_type: EventType
    conversation_id: str
    producer: Actor
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TurnResult(BaseModel):
    events: list[WorkflowEvent]
    answer: FinalAnswer
