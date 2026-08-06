from __future__ import annotations

import json
from uuid import UUID

import httpx
import pytest

from samnik_backend.api import create_app
from samnik_backend.domain import (
    Actor,
    ChatTurnRequest,
    Company,
    EventType,
    FinalAnswer,
    Intent,
    Metric,
    NormalizedQuery,
    Route,
    TurnResult,
    UnavailableReason,
    Verdict,
    WorkflowEvent,
    WorkflowStatus,
)
from samnik_backend.public_contract import to_public_events
from samnik_backend.runtime import build_local_workflow

from conftest import AS_OF


def _turn_result(
    *,
    verdict: Verdict,
    status: WorkflowStatus,
    route: Route = Route.RAG,
    unavailable_reason: UnavailableReason | None = None,
    stages: tuple[str, ...] = ("SQL",),
) -> TurnResult:
    conversation_id = "public-contract-test"
    events = [
        WorkflowEvent(
            sequence=1,
            event_type=EventType.TURN_STARTED,
            conversation_id=conversation_id,
            producer=Actor.SYSTEM,
        ),
        WorkflowEvent(
            sequence=2,
            event_type=EventType.SUPERVISOR_ROUTED,
            conversation_id=conversation_id,
            producer=Actor.SUPERVISOR,
            payload={"route": route.value},
        ),
    ]
    for mode in stages:
        events.append(
            WorkflowEvent(
                sequence=len(events) + 1,
                event_type=EventType.AGENT_STARTED,
                conversation_id=conversation_id,
                producer=Actor.RAG_AGENT,
                payload={"agent": "RAG_FAQ", "retrieval_mode": mode},
            )
        )
    if stages:
        events.append(
            WorkflowEvent(
                sequence=len(events) + 1,
                event_type=EventType.EVIDENCE_CHECKED,
                conversation_id=conversation_id,
                producer=Actor.EVIDENCE_GATE,
                payload={"passed": True},
            )
        )
    answer = FinalAnswer(
        workflow_status=status,
        verdict=verdict,
        conclusion="synthetic local answer",
        evidence=[],
        claims=[],
        limitations=[],
        unavailable_reason=unavailable_reason,
        as_of=AS_OF,
        route=route,
    )
    events.append(
        WorkflowEvent(
            sequence=len(events) + 1,
            event_type=EventType.ANSWER_COMPLETED,
            conversation_id=conversation_id,
            producer=Actor.SUPERVISOR,
            payload={"answer": answer.model_dump(mode="json")},
        )
    )
    return TurnResult(events=events, answer=answer)


@pytest.mark.parametrize(
    ("answer_verdict", "status", "reason", "expected_public_verdict"),
    [
        (Verdict.CONFIRMED, WorkflowStatus.ANSWER_READY, None, "CONFIRMED"),
        (Verdict.CONTRADICTED, WorkflowStatus.ANSWER_READY, None, "CONTRADICTED"),
        (
            Verdict.PARTIALLY_CONFIRMED,
            WorkflowStatus.UNAVAILABLE,
            None,
            "REVIEW_REQUIRED",
        ),
        (
            Verdict.NO_OFFICIAL_EVIDENCE,
            WorkflowStatus.UNAVAILABLE,
            UnavailableReason.NO_OFFICIAL_EVIDENCE,
            "INSUFFICIENT_EVIDENCE",
        ),
        (
            Verdict.INSUFFICIENT_INFO,
            WorkflowStatus.SYSTEM_ERROR,
            UnavailableReason.REPOSITORY_UNAVAILABLE,
            "SYSTEM_UNAVAILABLE",
        ),
        (Verdict.OUT_OF_SCOPE, WorkflowStatus.UNAVAILABLE, None, "OUT_OF_SCOPE"),
    ],
)
def test_public_contract_maps_every_terminal_answer_to_explicit_public_verdicts(
    answer_verdict,
    status,
    reason,
    expected_public_verdict,
) -> None:
    request = ChatTurnRequest(
        conversation_id="public-contract-test",
        client_request_id="request-public-contract",
        message="synthetic local request",
    )
    result = _turn_result(
        verdict=answer_verdict,
        status=status,
        route=Route.OUT_OF_SCOPE if answer_verdict == Verdict.OUT_OF_SCOPE else Route.RAG,
        unavailable_reason=reason,
        stages=() if answer_verdict == Verdict.OUT_OF_SCOPE else ("SQL",),
    )

    events = to_public_events(request, result)
    terminal = events[-1]

    assert terminal.type == "answer.completed"
    assert terminal.data["result"]["verdict"] == expected_public_verdict
    assert terminal.data["result"]["verdict"] in {
        "CONFIRMED",
        "CONTRADICTED",
        "REVIEW_REQUIRED",
        "INSUFFICIENT_EVIDENCE",
        "SYSTEM_UNAVAILABLE",
        "OUT_OF_SCOPE",
    }


def test_public_progress_is_allowlisted_ordered_and_hides_internal_payloads() -> None:
    request = ChatTurnRequest(
        conversation_id="public-contract-test",
        client_request_id="request-progress",
        message="2025년 삼성전자 영업이익은?",
    )
    events = to_public_events(
        request,
        _turn_result(
            verdict=Verdict.CONFIRMED,
            status=WorkflowStatus.ANSWER_READY,
            stages=("SQL",),
        ),
    )

    assert [event.seq for event in events] == list(range(len(events)))
    assert [event.type for event in events] == [
        "run.started",
        "workflow.progress",
        "workflow.progress",
        "workflow.progress",
        "workflow.progress",
        "answer.completed",
    ]
    assert [
        event.data["stage"] for event in events if event.type == "workflow.progress"
    ] == [
        "SUPERVISOR_ROUTING",
        "SQL_RETRIEVAL",
        "EVIDENCE_VALIDATION",
        "SUPERVISOR_FINALIZING",
    ]
    serialized = "\n".join(event.model_dump_json() for event in events)
    for forbidden in ("prompt", "chain_of_thought", "raw_sql", "api_key", "embedding"):
        assert forbidden not in serialized.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"message": "테스트", "client_request_id": "request-omitted-conversation"},
        {
            "message": "테스트",
            "conversation_id": None,
            "client_request_id": "request-null-conversation",
        },
    ],
)
async def test_jsonl_endpoint_accepts_nullable_or_omitted_conversation_id(payload) -> None:
    app = create_app(build_local_workflow())
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://local.test") as client:
        response = await client.post("/api/v1/chat/turns/stream", json=payload)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/jsonl")
    lines = [json.loads(line) for line in response.text.splitlines() if line]
    assert lines[0]["type"] == "run.started"
    assert lines[-1]["type"] in {"answer.completed", "clarification.required"}
    request_id = lines[0]["request_id"]
    conversation_id = lines[0]["conversation_id"]
    assert all(event["request_id"] == request_id for event in lines)
    assert all(event["conversation_id"] == conversation_id for event in lines)
    assert request_id == payload["client_request_id"]
    UUID(conversation_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"message": "테스트"},
        {"message": "테스트", "client_request_id": ""},
        {"message": "테스트", "client_request_id": "   "},
        {"message": "테스트", "client_request_id": None},
    ],
)
async def test_jsonl_endpoint_rejects_missing_or_blank_client_request_id(payload) -> None:
    app = create_app(build_local_workflow())
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://local.test") as client:
        response = await client.post("/api/v1/chat/turns/stream", json=payload)

    assert response.status_code == 422
