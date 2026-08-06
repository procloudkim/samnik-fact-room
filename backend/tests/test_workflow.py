from __future__ import annotations

from samnik_backend.domain import (
    Actor,
    ChatTurnRequest,
    EventType,
    Route,
    Verdict,
    WorkflowStatus,
)


def request(conversation_id: str, message: str, suffix: str = "1") -> ChatTurnRequest:
    return ChatTurnRequest(
        conversation_id=conversation_id,
        client_request_id=f"req-{conversation_id}-{suffix}",
        message=message,
    )


async def test_t1_direct_fact_routes_to_rag_with_evidence(workflow) -> None:
    result = await workflow.run_turn(request("t1", "2025년 삼성전자 연결 영업이익은?"))
    assert result.answer.route == Route.RAG
    assert result.answer.workflow_status == WorkflowStatus.ANSWER_READY
    assert result.answer.verdict == Verdict.CONFIRMED
    assert result.answer.facts[0].period == "2025"
    assert result.answer.evidence[0].rcept_no == "20260310002820"
    calls = [
        event.producer
        for event in result.events
        if event.event_type == EventType.AGENT_STARTED
    ]
    assert calls == [Actor.RAG_AGENT]
    assert result.events[-1].producer == Actor.SUPERVISOR


async def test_t2_multiturn_returns_to_supervisor_then_rag(workflow) -> None:
    first = await workflow.run_turn(
        request("t2", "삼전 영업이익이 하닉에 따였다던데 사실이야?", "1")
    )
    assert first.answer.workflow_status == WorkflowStatus.NEED_MORE_INFO
    assert first.answer.route == Route.SCENARIO
    assert "최근" in first.answer.conclusion

    second = await workflow.run_turn(request("t2", "응, 최근 공통 분기로 봐줘.", "2"))
    assert second.answer.workflow_status == WorkflowStatus.ANSWER_READY
    assert second.answer.verdict == Verdict.CONFIRMED
    assert second.answer.comparison_basis is not None
    assert second.answer.comparison_basis.is_valid
    assert [fact.period for fact in second.answer.facts] == ["2026Q1", "2026Q1"]
    calls = [
        event.producer
        for event in second.events
        if event.event_type == EventType.AGENT_STARTED
    ]
    assert calls == [Actor.SCENARIO_AGENT, Actor.RAG_AGENT]
    returned_routes = [
        event.payload.get("returned_from")
        for event in second.events
        if event.event_type == EventType.SUPERVISOR_ROUTED
    ]
    assert Actor.SCENARIO_AGENT in returned_routes
    assert second.events[-1].producer == Actor.SUPERVISOR


async def test_public_example_returns_latest_common_quarter(workflow) -> None:
    result = await workflow.run_turn(
        request("latest-common-quarter", "두 회사의 최근 공통 분기 영업이익을 비교해줘")
    )

    assert result.answer.workflow_status == WorkflowStatus.ANSWER_READY
    assert result.answer.verdict == Verdict.CONFIRMED
    assert [fact.period for fact in result.answer.facts] == ["2026Q1", "2026Q1"]
    assert "2026년 1분기" in result.answer.conclusion
    assert "연결 누적" in result.answer.conclusion


async def test_t3_no_evidence_never_says_false(workflow) -> None:
    result = await workflow.run_turn(
        request("t3", "SK하이닉스가 특정 고객사로부터 HBM 승인을 받았다는 말이 사실이야?")
    )
    assert result.answer.verdict == Verdict.NO_OFFICIAL_EVIDENCE
    assert result.answer.workflow_status == WorkflowStatus.UNAVAILABLE
    assert "거짓이라고 확정한 것은 아닙니다" in result.answer.conclusion
    assert "거짓입니다" not in result.answer.conclusion
    assert result.answer.facts == []


async def test_t4_out_of_scope_calls_no_specialist(workflow) -> None:
    result = await workflow.run_turn(request("t4", "내일 삼성전자 사도 돼?"))
    assert result.answer.verdict == Verdict.OUT_OF_SCOPE
    assert result.answer.route == Route.OUT_OF_SCOPE
    assert not any(event.event_type == EventType.AGENT_STARTED for event in result.events)
    assert "사야 할지" in result.answer.conclusion
    assert result.events[-1].producer == Actor.SUPERVISOR


async def test_t6_positive_vector_evidence(workflow) -> None:
    result = await workflow.run_turn(request("t6", "삼성전자의 주요 사업은?"))
    assert result.answer.verdict == Verdict.CONFIRMED
    assert result.answer.facts == []
    assert result.answer.evidence[0].source_kind == "FILING_CHUNK"
    rag_started = next(
        event
        for event in result.events
        if event.event_type == EventType.AGENT_STARTED and event.producer == Actor.RAG_AGENT
    )
    assert rag_started.payload["retrieval_mode"] == "VECTOR"


async def test_reused_client_request_id_starts_a_fresh_graph_run(workflow) -> None:
    shared = "req-replay-boundary"
    first = ChatTurnRequest(
        conversation_id="replay-boundary",
        client_request_id=shared,
        message="2025년 삼성전자 연결 영업이익은?",
    )
    second = ChatTurnRequest(
        conversation_id="replay-boundary",
        client_request_id=shared,
        message="내일 삼성전자 사도 돼?",
    )

    first_result = await workflow.run_turn(first)
    second_result = await workflow.run_turn(second)

    assert first_result.answer.route == Route.RAG
    assert second_result.answer.route == Route.OUT_OF_SCOPE
    assert [event.sequence for event in second_result.events] == list(
        range(1, len(second_result.events) + 1)
    )
    assert not any(
        event.event_type == EventType.AGENT_STARTED
        for event in second_result.events
    )
