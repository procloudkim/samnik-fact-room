from __future__ import annotations

from samnik_backend.domain import (
    Actor,
    ChatTurnRequest,
    Company,
    EventType,
    FinalAnswer,
    Route,
    WorkflowEvent,
    WorkflowStatus,
    Verdict,
    TurnResult,
)
from samnik_backend.public_contract import (
    _public_excerpt,
    _public_progress_events,
    to_public_events,
)
from conftest import AS_OF, make_evidence


def _workflow_event(
    sequence: int,
    event_type: EventType,
    producer: Actor,
    payload: dict[str, object],
) -> WorkflowEvent:
    return WorkflowEvent(
        sequence=sequence,
        event_type=event_type,
        conversation_id="public-contract-test",
        producer=producer,
        payload=payload,
    )


def _turn_result(
    *,
    verdict: Verdict,
    status: WorkflowStatus,
    route: Route = Route.RAG,
    include_scenario: bool = False,
    retrieval_mode: str = "SQL",
    conclusion: str = "synthetic local answer",
    evidence=None,
) -> TurnResult:
    events = [
        _workflow_event(
            sequence=1,
            event_type=EventType.TURN_STARTED,
            producer=Actor.SYSTEM,
            payload={"message_length": 17, "client_request_id": "public-contract"},
        ),
        _workflow_event(
            sequence=2,
            event_type=EventType.SUPERVISOR_ROUTED,
            producer=Actor.SUPERVISOR,
            payload={
                "route": route,
                "model_provider": "secret-model-provider",
                "classification_fallback": False,
                "prompt_snapshot": "should-not-leak-chain",
                "returned_from": None,
            },
        ),
    ]
    next_seq = 3
    if include_scenario:
        events.extend(
            [
                _workflow_event(
                    sequence=next_seq,
                    event_type=EventType.AGENT_STARTED,
                    producer=Actor.SCENARIO_AGENT,
                    payload={"agent": "MULTI_TURN_SCENARIO", "workflow_status": status},
                ),
                _workflow_event(
                    sequence=next_seq + 1,
                    event_type=EventType.AGENT_COMPLETED,
                    producer=Actor.SCENARIO_AGENT,
                    payload={
                        "agent": "MULTI_TURN_SCENARIO",
                        "workflow_status": status,
                        "missing_slots": [],
                        "collected_slots": {},
                    },
                ),
            ]
        )
        next_seq += 2
    events.extend(
        [
            _workflow_event(
                sequence=next_seq,
                event_type=EventType.AGENT_STARTED,
                producer=Actor.RAG_AGENT,
                payload={
                    "agent": "RAG_FAQ",
                    "retrieval_mode": retrieval_mode,
                    "raw_sql": "SELECT * FROM fact_table",  # must be sanitized out
                },
            ),
            _workflow_event(
                sequence=next_seq + 1,
                event_type=EventType.AGENT_COMPLETED,
                producer=Actor.RAG_AGENT,
                payload={"agent": "RAG_FAQ", "workflow_status": status},
            ),
            _workflow_event(
                sequence=next_seq + 2,
                event_type=EventType.EVIDENCE_CHECKED,
                producer=Actor.EVIDENCE_GATE,
                payload={
                    "passed": True,
                    "facts": 2,
                    "evidence": 3,
                },
            ),
        ]
    )
    answer = FinalAnswer(
        workflow_status=status,
        verdict=verdict,
        conclusion=conclusion,
        evidence=evidence or [],
        claims=[],
        limitations=[],
        as_of=AS_OF,
        route=route,
    )
    events.append(
        _workflow_event(
            sequence=next_seq + 3,
            event_type=EventType.ANSWER_COMPLETED,
            producer=Actor.SUPERVISOR,
            payload={"answer": answer.model_dump(mode="json"), "model_provider": "secret-model-provider"},
        )
    )
    return TurnResult(events=events, answer=answer)


def test_public_progress_events_is_additive_and_stage_compatible() -> None:
    request = ChatTurnRequest(
        conversation_id="public-contract-test",
        client_request_id="request-progress",
        message="요약해서 보여줘",
    )
    result = _turn_result(
        verdict=Verdict.CONFIRMED,
        status=WorkflowStatus.ANSWER_READY,
        include_scenario=True,
    )

    events = to_public_events(request, result)
    progress = [event.data for event in events if event.type == "workflow.progress"]

    assert [event["stage"] for event in progress] == [
        "SUPERVISOR_ROUTING",
        "SCENARIO_SLOT_CHECK",
        "SQL_RETRIEVAL",
        "EVIDENCE_VALIDATION",
        "SUPERVISOR_FINALIZING",
    ]
    for entry in progress:
        assert set(["stage", "label", "status"]).issubset(entry)
        assert entry["status"] in {
            "routing",
            "started",
            "completed",
            "passed",
            "finalizing",
        }
        assert entry["agent"]
        assert entry["tool"]

    assert any(
        entry["stage"] == "SQL_RETRIEVAL" and entry["summary"]["retrieval_mode"] == "SQL"
        for entry in progress
    )
    assert progress[-1]["summary"]["workflow_status"] == WorkflowStatus.ANSWER_READY.value


def test_public_events_hide_internal_payload_fields() -> None:
    request = ChatTurnRequest(
        conversation_id="public-contract-test",
        client_request_id="request-sanitized",
        message="내부 payload 값을 노출하지 말아주세요",
    )
    result = _turn_result(
        verdict=Verdict.CONFIRMED,
        status=WorkflowStatus.ANSWER_READY,
        retrieval_mode="VECTOR",
    )
    events = to_public_events(request, result)

    serialized = "\n".join(event.model_dump_json() for event in events)
    for forbidden in (
        "model_provider",
        "prompt_snapshot",
        "chain_of_thought",
        "raw_sql",
        "api_key",
        "secret-model-provider",
    ):
        assert forbidden not in serialized.lower()

    assert [event.type for event in events] == [
        "run.started",
        "workflow.progress",
        "workflow.progress",
        "workflow.progress",
        "workflow.progress",
        "answer.completed",
    ]
    assert [event.data["stage"] for event in events if event.type == "workflow.progress"] == [
        "SUPERVISOR_ROUTING",
        "VECTOR_RETRIEVAL",
        "EVIDENCE_VALIDATION",
        "SUPERVISOR_FINALIZING",
    ]
    assert events[-1].data["result"]["status"] == "CONFIRMED"


def test_review_required_is_distinct_from_missing_evidence_and_limits_public_excerpts() -> None:
    request = ChatTurnRequest(
        conversation_id="public-contract-test",
        client_request_id="request-review-required",
        message="삼전의 재무건전성은?",
    )
    long_excerpt = "유동성과 부채 부담, 현금창출력을 함께 검토해야 합니다. " * 30
    evidence = [
        make_evidence(
            f"health-{index}",
            Company.SAMSUNG_ELECTRONICS,
            f"2026051500218{index}",
            excerpt=long_excerpt,
            source_kind="FILING_CHUNK",
        ).model_copy(update={"section_path": f"재무위험관리 > 핵심 근거 {index}"})
        for index in range(8)
    ]
    result = _turn_result(
        verdict=Verdict.INSUFFICIENT_INFO,
        status=WorkflowStatus.UNAVAILABLE,
        conclusion=(
            "관련 공식 공시 근거 8건을 확인했습니다. 다만 재무건전성은 "
            "단일 지표로 단정할 수 없습니다."
        ),
        evidence=evidence,
    )

    public_result = to_public_events(request, result)[-1].data["result"]

    assert public_result["verdict"] == "REVIEW_REQUIRED"
    assert public_result["kind"] == "narrative_evidence"
    assert public_result["title"] == "관련 공시는 확인됐으며 종합 판단이 필요합니다"
    assert "근거가 부족" not in public_result["title"]
    assert public_result["evidence_total_count"] == 8
    assert len(public_result["evidence"]) == 4
    assert all(len(item["excerpt"]) <= 420 for item in public_result["evidence"])
    assert all(item["excerpt"] in long_excerpt for item in public_result["evidence"])
    assert all(item["excerpt_truncated"] is True for item in public_result["evidence"])


def test_public_excerpt_normalizes_legacy_filing_control_entities() -> None:
    excerpt, truncated = _public_excerpt(
        "삼성전자주식회사&cr;&cr;내부회계관리제도 검토보고서&nbsp;첨부"
    )

    assert excerpt == "삼성전자주식회사\n\n내부회계관리제도 검토보고서 첨부"
    assert truncated is False
    assert "&cr;" not in excerpt


def test_public_excerpt_centers_long_chunk_on_requested_financial_context() -> None:
    excerpt, truncated = _public_excerpt(
        "공정가치 설명 " * 80 + "매출채권 경과기간별 잔액 현황과 대손충당금",
        ("매출채권", "대손충당금"),
    )

    assert truncated is True
    assert "매출채권" in excerpt
    assert len(excerpt) <= 420
