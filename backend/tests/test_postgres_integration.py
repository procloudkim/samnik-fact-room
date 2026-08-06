from __future__ import annotations

import json
import os
import re

import asyncpg
import httpx
import pytest

from samnik_backend.agents import EvidenceGate, MultiTurnScenarioAgent, RagFaqAgent
from samnik_backend.api import create_app
from samnik_backend.domain import ChatTurnRequest
from samnik_backend.postgres import PostgresEvidenceRepository, PostgresFinancialRepository
from samnik_backend.providers import (
    HashEmbeddingProvider,
    LocalSentenceTransformerEmbeddingProvider,
    RuleBasedLanguageModel,
)
from samnik_backend.repositories import InMemoryConversationRepository
from samnik_backend.public_contract import to_public_events
from samnik_backend.workflow import FactRoomWorkflow


def _database_url() -> str:
    value = os.environ.get("DATABASE_URL") or os.environ.get("SAMNIK_POSTGRES_DSN")
    if not value:
        pytest.skip("DATABASE_URL is required for the live PostgreSQL integration test")
    return value


def _workflow(pool: asyncpg.Pool) -> FactRoomWorkflow:
    return FactRoomWorkflow(
        language_model=RuleBasedLanguageModel(),
        conversations=InMemoryConversationRepository(),
        scenario_agent=MultiTurnScenarioAgent(),
        rag_agent=RagFaqAgent(
            financials=PostgresFinancialRepository(pool),
            evidence=PostgresEvidenceRepository(pool),
            embeddings=HashEmbeddingProvider(),
        ),
        evidence_gate=EvidenceGate(),
        checkpointer=None,
    )


async def _post_workflow(
    workflow: FactRoomWorkflow,
    message: str,
    request_id: str,
    *,
    conversation_id: str | None = None,
) -> dict:
    transport = httpx.ASGITransport(app=create_app(workflow))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://postgres.integration"
    ) as client:
        response = await client.post(
            "/api/v1/chat/turns/stream",
            json={
                "conversation_id": conversation_id,
                "client_request_id": request_id,
                "message": message,
            },
        )
    assert response.status_code == 200
    events = [json.loads(line) for line in response.text.splitlines() if line]
    last = events[-1]
    if "result" in last["data"]:
        return last["data"]["result"]
    return {"event_type": last["type"], **last["data"]}


async def _post(pool: asyncpg.Pool, message: str, request_id: str) -> dict:
    return await _post_workflow(_workflow(pool), message, request_id)


@pytest.mark.asyncio
async def test_scenario_a_uses_live_postgres_and_returns_confirmed_metadata() -> None:
    pool = await asyncpg.create_pool(_database_url(), min_size=1, max_size=2)
    try:
        async with pool.acquire() as connection:
            expected = await connection.fetchrow(
                """
                SELECT fsl.thstrm_amount AS amount_numeric,
                       fsl.thstrm_amount_raw AS raw_amount,
                       1 AS unit_scale,
                       f.period_start, f.period_end, fsl.sj_div,
                       f.reprt_code, f.rcept_no, f.source_url
                FROM financial_statement_line fsl
                JOIN filing_revision fr
                  ON fr.filing_revision_id = fsl.filing_revision_id
                JOIN filing f
                  ON f.snapshot_id = fsl.snapshot_id AND f.rcept_no = fsl.rcept_no
                JOIN snapshot_import si ON si.snapshot_id = fsl.snapshot_id
                LEFT JOIN LATERAL (
                    SELECT ma.metric_key
                    FROM metric_alias ma
                    WHERE (ma.corp_code IS NULL OR ma.corp_code = fsl.corp_code)
                      AND fsl.business_year BETWEEN ma.valid_from_year AND COALESCE(ma.valid_to_year, 2100)
                      AND (ma.account_id = fsl.account_id OR ma.exact_account_nm = fsl.account_nm)
                    ORDER BY (ma.corp_code IS NOT NULL) DESC, ma.priority, ma.metric_alias_id
                    LIMIT 1
                ) alias ON true
                WHERE fsl.corp_code = '00126380'
                  AND fsl.business_year = 2025
                  AND alias.metric_key = 'operating_profit'
                  AND fsl.fs_div = 'CFS'
                  AND fsl.sj_div = 'IS'
                  AND f.evidence_status = 'OFFICIAL_API_VERIFIED'
                  AND si.evidence_status = 'OFFICIAL_API_VERIFIED'
                ORDER BY CASE WHEN fr.selected_latest THEN 1 ELSE 0 END DESC,
                         fr.filing_date DESC, si.acquired_at DESC,
                         si.snapshot_id DESC
                LIMIT 1
                """
            )
        assert expected is not None

        result = await _post(
            pool,
            "2025년 삼성전자 연결 기준 영업이익은 얼마야?",
            "scenario-a-live-postgres",
        )
        assert result["status"] == "CONFIRMED"
        assert result["verdict"] == "CONFIRMED"
        assert result["trace_summary"]["final_actor"] == "SUPERVISOR"
        assert result["trace_summary"]["agent_calls"] == ["RAG_FAQ"]
        assert result["summary"] == (
            "삼성전자의 2025년 영업이익은 "
            "43조 6,010억 5,100만 원(약 43.60조 원)입니다. "
            "쉽게 말해 '연결'은 회사와 자회사 실적을 합친 기준이고, "
            "'영업이익'은 회사가 본업으로 번 이익입니다."
        )
        assert "E+" not in result["summary"]
        assert "KRW" not in result["summary"]

        fact = result["facts"][0]
        assert fact["raw_value"] == format(expected["amount_numeric"], "f")
        assert fact["source_raw_amount"] == expected["raw_amount"]
        assert fact["source_unit_scale"] == expected["unit_scale"]
        assert fact["period_start"] == expected["period_start"].isoformat()
        assert fact["period_end"] == expected["period_end"].isoformat()
        assert fact["report_code"] == expected["reprt_code"].strip()
        assert fact["fs_div"] == "CFS"
        assert fact["period_type"] == "ANNUAL"
        assert fact["section"] == expected["sj_div"]

        evidence = result["evidence"][0]
        assert evidence["rcept_no"] == expected["rcept_no"].strip()
        assert evidence["source_url"] == expected["source_url"]
        assert evidence["section"] == expected["sj_div"]
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_live_postgres_success_without_a_matching_row_is_insufficient_evidence() -> None:
    pool = await asyncpg.create_pool(_database_url(), min_size=1, max_size=2)
    try:
        result = await _post(
            pool,
            "2099년 삼성전자 연결 기준 영업이익은 얼마야?",
            "scenario-a-no-evidence",
        )
        assert result["status"] == "INSUFFICIENT_EVIDENCE"
        assert result["verdict"] == "INSUFFICIENT_EVIDENCE"
        assert result["failure_reason"] == "NO_OFFICIAL_EVIDENCE"
        assert result["facts"] == []
        assert result["evidence"] == []
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_closed_postgres_pool_is_repository_unavailable() -> None:
    pool = await asyncpg.create_pool(_database_url(), min_size=1, max_size=1)
    await pool.close()

    result = await _post(
        pool,
        "2025년 삼성전자 연결 기준 영업이익은 얼마야?",
        "scenario-a-repository-unavailable",
    )

    assert result["status"] == "SYSTEM_UNAVAILABLE"
    assert result["verdict"] == "SYSTEM_UNAVAILABLE"
    assert result["failure_reason"] == "REPOSITORY_UNAVAILABLE"
    assert result["facts"] == []
    assert result["evidence"] == []


@pytest.mark.asyncio
async def test_live_postgres_supports_b_c_and_t6_with_official_evidence() -> None:
    pool = await asyncpg.create_pool(_database_url(), min_size=1, max_size=3)
    workflow = FactRoomWorkflow(
        language_model=RuleBasedLanguageModel(),
        conversations=InMemoryConversationRepository(),
        scenario_agent=MultiTurnScenarioAgent(),
        rag_agent=RagFaqAgent(
            financials=PostgresFinancialRepository(pool),
            evidence=PostgresEvidenceRepository(pool),
            embeddings=LocalSentenceTransformerEmbeddingProvider(local_files_only=True),
            top_k=8,
        ),
        evidence_gate=EvidenceGate(),
        checkpointer=None,
    )
    try:
        conversation_id = "live-postgres-comparison"
        first_request = ChatTurnRequest(
            conversation_id=conversation_id,
            client_request_id="live-postgres-b-1",
            message="삼전 영업이익이 하닉에 따였다던데 사실이야?",
        )
        first_turn = await workflow.run_turn(first_request)
        first = to_public_events(first_request, first_turn)[-1]
        assert first.type == "clarification.required"
        assert first.data["missing_slots"] == ["period_mode"]

        comparison_request = ChatTurnRequest(
            conversation_id=conversation_id,
            client_request_id="live-postgres-b-2",
            message="응, 2025년 연간 연결 기준으로 봐줘.",
        )
        comparison_turn = await workflow.run_turn(comparison_request)
        comparison = to_public_events(
            comparison_request,
            comparison_turn,
        )[-1].data["result"]
        assert comparison["status"] == "CONFIRMED"
        assert comparison["kind"] == "comparison"
        assert [fact["company"] for fact in comparison["facts"]] == [
            "SAMSUNG_ELECTRONICS",
            "SK_HYNIX",
        ]
        assert {item["rcept_no"] for item in comparison["evidence"]} == {
            "20260310002820",
            "20260317000635",
        }

        vector_workflow = FactRoomWorkflow(
            language_model=RuleBasedLanguageModel(),
            conversations=InMemoryConversationRepository(),
            scenario_agent=MultiTurnScenarioAgent(),
            rag_agent=RagFaqAgent(
                financials=PostgresFinancialRepository(pool),
                evidence=PostgresEvidenceRepository(pool),
                embeddings=LocalSentenceTransformerEmbeddingProvider(local_files_only=True),
                top_k=8,
            ),
            evidence_gate=EvidenceGate(),
            checkpointer=None,
        )
        claim_request = ChatTurnRequest(
            client_request_id="live-postgres-c",
            message="삼성전자 DS부문이 2025년에도 적자였다는 글이 사실이야?",
        )
        claim_turn = await vector_workflow.run_turn(claim_request)
        assert claim_turn.answer.workflow_status.value != "SYSTEM_ERROR", (
            claim_turn.answer.limitations
        )
        claim = to_public_events(claim_request, claim_turn)[-1].data["result"]
        assert claim["status"] == "CONTRADICTED"
        assert claim["trace_summary"]["vector_search_called"] is True
        assert any(
            "24,858,075" in (item["excerpt"] or "")
            for item in claim["evidence"]
        )

        research_request = ChatTurnRequest(
            client_request_id="live-postgres-t6",
            message="2025년 삼성전자 사업보고서에서 연구개발 활동 관련 근거를 찾아줘.",
        )
        research_turn = await vector_workflow.run_turn(research_request)
        research = to_public_events(research_request, research_turn)[-1].data["result"]
        assert research["status"] == "CONFIRMED"
        assert research["trace_summary"]["vector_search_called"] is True
        assert research["evidence"]
        assert all(
            "연구개발" in f"{item['section']} {item['excerpt']}"
            or re.search(r"\bR\s*&\s*D\b", f"{item['section']} {item['excerpt']}")
            for item in research["evidence"]
        )
    finally:
        await pool.close()
