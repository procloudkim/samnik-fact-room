from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import Mapping, Sequence

from .domain import (
    Company,
    ConversationRecord,
    Evidence,
    EvidenceHit,
    EvidenceSearchResult,
    FinancialFact,
    FsDiv,
    Metric,
    ValueScope,
)
from .formatting import period_sort_key


class InMemoryFinancialRepository:
    def __init__(self, facts: Sequence[FinancialFact] = ()) -> None:
        self._facts = list(facts)

    async def get_fact(
        self,
        *,
        company: Company,
        metric: Metric,
        period: str,
        value_scope: ValueScope,
        fs_div: FsDiv,
        as_of: datetime,
    ) -> FinancialFact | None:
        matches = [
            fact
            for fact in self._facts
            if fact.company == company
            and fact.metric == metric
            and fact.period == period
            and fact.value_scope == value_scope
            and fact.fs_div == fs_div
        ]
        return matches[0] if matches else None

    async def get_latest_common_facts(
        self,
        *,
        companies: tuple[Company, Company],
        metric: Metric,
        value_scope: ValueScope,
        fs_div: FsDiv,
        as_of: datetime,
        interim_only: bool = False,
    ) -> tuple[FinancialFact, FinancialFact] | None:
        by_company: dict[Company, dict[str, FinancialFact]] = {company: {} for company in companies}
        for fact in self._facts:
            if (
                fact.company in by_company
                and fact.metric == metric
                and fact.value_scope == value_scope
                and fact.fs_div == fs_div
                and (not interim_only or len(fact.period) > 4)
            ):
                by_company[fact.company][fact.period] = fact
        common = set(by_company[companies[0]]) & set(by_company[companies[1]])
        if not common:
            return None
        period = max(common, key=period_sort_key)
        return by_company[companies[0]][period], by_company[companies[1]][period]


class InMemoryEvidenceRepository:
    def __init__(
        self,
        evidence: Sequence[Evidence] = (),
        *,
        search_index: Mapping[str, Sequence[EvidenceHit]] | None = None,
        corpus_complete: bool = True,
        corpus_scope: str = "local fixture corpus",
        searched_until: datetime | None = None,
    ) -> None:
        self._evidence = {item.evidence_id: item for item in evidence}
        self._search_index = {key: list(value) for key, value in (search_index or {}).items()}
        self._corpus_complete = corpus_complete
        self._corpus_scope = corpus_scope
        self._searched_until = searched_until or datetime.now(UTC)

    async def get_by_ids(self, evidence_ids: Sequence[str]) -> list[Evidence]:
        return [self._evidence[item] for item in evidence_ids if item in self._evidence]

    async def search(
        self,
        *,
        company: Company,
        period: str | None = None,
        query_text: str,
        embedding: Sequence[float],
        as_of: datetime,
        limit: int,
    ) -> EvidenceSearchResult:
        if query_text in self._search_index:
            hits = [
                hit
                for hit in self._search_index[query_text]
                if hit.evidence.company == company
                and (
                    period is None
                    or hit.evidence.business_year is None
                    or period.startswith(str(hit.evidence.business_year))
                )
            ]
        else:
            query_terms = set(re.findall(r"[0-9A-Za-z가-힣]{2,}", query_text.lower()))
            hits = []
            for item in self._evidence.values():
                if (
                    item.company != company
                    or not item.excerpt
                    or (
                        period is not None
                        and item.business_year is not None
                        and not period.startswith(str(item.business_year))
                    )
                ):
                    continue
                evidence_terms = set(re.findall(r"[0-9A-Za-z가-힣]{2,}", item.excerpt.lower()))
                overlap = len(query_terms & evidence_terms)
                if overlap:
                    score = min(0.95, 0.55 + (0.1 * overlap))
                    hits.append(EvidenceHit(evidence=item, score=score))
            hits.sort(key=lambda hit: hit.score, reverse=True)
        company_filing_ids = sorted(
            {item.rcept_no for item in self._evidence.values() if item.company == company}
        )
        return EvidenceSearchResult(
            hits=hits[:limit],
            searched_filing_ids=company_filing_ids,
            corpus_scope=self._corpus_scope,
            corpus_complete=self._corpus_complete,
            searched_until=min(as_of, self._searched_until),
        )


class InMemoryConversationRepository:
    def __init__(self) -> None:
        self._records: dict[str, ConversationRecord] = {}
        self._lock = asyncio.Lock()

    async def get(self, conversation_id: str) -> ConversationRecord | None:
        async with self._lock:
            record = self._records.get(conversation_id)
            return record.model_copy(deep=True) if record else None

    async def put(self, record: ConversationRecord) -> None:
        async with self._lock:
            self._records[record.conversation_id] = record.model_copy(deep=True)

    async def delete(self, conversation_id: str) -> None:
        async with self._lock:
            self._records.pop(conversation_id, None)
