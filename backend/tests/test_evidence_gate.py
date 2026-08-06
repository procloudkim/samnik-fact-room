from __future__ import annotations

from dataclasses import replace

import pytest

from samnik_backend.agents import EvidenceGate
from samnik_backend.domain import (
    ComparisonBasis,
    Company,
    Evidence,
    EvidenceStatus,
    Intent,
    Metric,
    NormalizedQuery,
    RagResult,
    ValueScope,
    Verdict,
    WorkflowStatus,
)
from samnik_backend.errors import EvidenceValidationError

from conftest import AS_OF


def _comparison_query() -> NormalizedQuery:
    return NormalizedQuery(
        intent=Intent.COMPARE,
        companies=[Company.SAMSUNG_ELECTRONICS, Company.SK_HYNIX],
        metric=Metric.OPERATING_PROFIT,
        period="2025",
        value_scope=ValueScope.CUMULATIVE,
        as_of=AS_OF,
    )


def test_evidence_gate_blocks_a_financial_fact_whose_evidence_is_missing(
    samsung_fact,
) -> None:
    result = RagResult(
        workflow_status=WorkflowStatus.ANSWER_READY,
        verdict=Verdict.CONFIRMED,
        normalized_query=NormalizedQuery(
            intent=Intent.DIRECT_FACT,
            companies=[Company.SAMSUNG_ELECTRONICS],
            metric=Metric.OPERATING_PROFIT,
            period="2025",
            value_scope=ValueScope.CUMULATIVE,
            as_of=AS_OF,
        ),
        facts=[samsung_fact],
        evidence=[],
    )

    with pytest.raises(EvidenceValidationError, match="missing evidence"):
        EvidenceGate().validate_rag(result)


def test_evidence_gate_blocks_answer_ready_comparison_with_basis_mismatch(
    samsung_fact,
    samsung_evidence,
    hynix_fact,
    hynix_evidence,
) -> None:
    result = RagResult(
        workflow_status=WorkflowStatus.ANSWER_READY,
        verdict=Verdict.CONFIRMED,
        normalized_query=_comparison_query(),
        facts=[samsung_fact, hynix_fact],
        evidence=[samsung_evidence, hynix_evidence],
        comparison_basis=ComparisonBasis(
            same_period=False,
            same_value_scope=True,
            same_fs_div=True,
            same_currency=True,
        ),
    )

    with pytest.raises(EvidenceValidationError, match="basis mismatch"):
        EvidenceGate().validate_rag(result)


def test_evidence_gate_blocks_terminal_verdict_without_any_evidence() -> None:
    result = RagResult(
        workflow_status=WorkflowStatus.ANSWER_READY,
        verdict=Verdict.CONFIRMED,
        normalized_query=NormalizedQuery(
            intent=Intent.CLAIM_CHECK,
            companies=[Company.SAMSUNG_ELECTRONICS],
            topic_or_claim="합성 주장",
            as_of=AS_OF,
        ),
        evidence=[],
    )

    with pytest.raises(EvidenceValidationError, match="requires evidence"):
        EvidenceGate().validate_rag(result)


def test_evidence_gate_blocks_terminal_verdict_from_synthetic_evidence() -> None:
    synthetic = Evidence(
        evidence_id="fixture:chunk:synthetic",
        company=Company.SAMSUNG_ELECTRONICS,
        rcept_no="20260310002820",
        filing_title="synthetic fixture",
        filed_at=AS_OF.date(),
        url="https://example.test/synthetic",
        excerpt="합성 테스트 청크",
        source_kind="FILING_CHUNK",
        evidence_status=EvidenceStatus.SYNTHETIC_UNVERIFIED,
    )
    result = RagResult(
        workflow_status=WorkflowStatus.ANSWER_READY,
        verdict=Verdict.CONFIRMED,
        normalized_query=NormalizedQuery(
            intent=Intent.CLAIM_CHECK,
            companies=[Company.SAMSUNG_ELECTRONICS],
            topic_or_claim="합성 주장",
            as_of=AS_OF,
        ),
        evidence=[synthetic],
    )

    with pytest.raises(EvidenceValidationError, match="official API evidence"):
        EvidenceGate().validate_rag(result)
