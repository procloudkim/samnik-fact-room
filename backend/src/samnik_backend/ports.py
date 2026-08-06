from __future__ import annotations

from datetime import datetime
from typing import Protocol, Sequence

from .domain import (
    AnswerDraft,
    Company,
    ConversationRecord,
    Evidence,
    EvidenceSearchResult,
    FinancialFact,
    FsDiv,
    Metric,
    NormalizedQuery,
    RouteDecision,
    Slots,
    ValueScope,
)


class LanguageModelPort(Protocol):
    async def classify(self, message: str, prior: ConversationRecord | None) -> RouteDecision: ...

    async def compose_answer(
        self,
        query: NormalizedQuery | None,
        facts: Sequence[FinancialFact],
        verdict: str,
        limitations: Sequence[str],
    ) -> AnswerDraft: ...


class EmbeddingPort(Protocol):
    async def embed(self, text: str) -> list[float]: ...


class FinancialRepository(Protocol):
    async def get_fact(
        self,
        *,
        company: Company,
        metric: Metric,
        period: str,
        value_scope: ValueScope,
        fs_div: FsDiv,
        as_of: datetime,
    ) -> FinancialFact | None: ...

    async def get_latest_common_facts(
        self,
        *,
        companies: tuple[Company, Company],
        metric: Metric,
        value_scope: ValueScope,
        fs_div: FsDiv,
        as_of: datetime,
        interim_only: bool = False,
    ) -> tuple[FinancialFact, FinancialFact] | None: ...


class EvidenceRepository(Protocol):
    async def get_by_ids(self, evidence_ids: Sequence[str]) -> list[Evidence]: ...

    async def search(
        self,
        *,
        company: Company,
        period: str | None = None,
        query_text: str,
        embedding: Sequence[float],
        as_of: datetime,
        limit: int,
    ) -> EvidenceSearchResult: ...


class ConversationRepository(Protocol):
    async def get(self, conversation_id: str) -> ConversationRecord | None: ...

    async def put(self, record: ConversationRecord) -> None: ...

    async def delete(self, conversation_id: str) -> None: ...


class DisclosureProvider(Protocol):
    async def list_filings(self, corp_code: str, begin: str, end: str) -> dict: ...

    async def fetch_financials(self, corp_code: str, year: int, report_code: str) -> dict: ...

    async def fetch_document(self, receipt_no: str) -> bytes: ...
