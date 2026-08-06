from __future__ import annotations

import json

from httpx import ASGITransport, AsyncClient

from samnik_backend.api import create_app


async def _post(app, payload: dict) -> tuple[AsyncClient, object]:
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    response = await client.post("/api/v1/chat/turns/stream", json=payload)
    return client, response


async def test_jsonl_public_contract_and_nullable_conversation_id(workflow) -> None:
    app = create_app(workflow)
    client, response = await _post(
        app,
        {
            "conversation_id": None,
            "client_request_id": "client-001",
            "message": "2025년 삼성전자 연결 영업이익은?",
        },
    )
    try:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/jsonl")
        events = [json.loads(line) for line in response.text.splitlines() if line]
        assert events[0]["type"] == "run.started"
        assert events[-1]["type"] == "answer.completed"
        assert [event["seq"] for event in events] == list(range(len(events)))
        assert {event["request_id"] for event in events} == {"client-001"}
        assert all(event["conversation_id"] for event in events)
        result = events[-1]["data"]["result"]
        assert result["kind"] == "single_fact"
        assert result["status"] == "CONFIRMED"
        assert result["verdict"] == "CONFIRMED"
        assert result["workflow_status"] == "ANSWER_READY"
        assert result["failure_reason"] is None
        fact = result["facts"][0]
        assert fact["raw_value"] == "43601051000000"
        assert fact["value_trillion_krw"] == "43.601051"
        assert fact["value_trillion_krw_display"] == "약 43.60조 원"
        assert fact["value_display"] == "43조 6,010억 5,100만 원"
        assert fact["period"] == "2025"
        assert fact["period_type"] == "ANNUAL"
        assert fact["fs_div"] == "CFS"
        assert fact["fs_div_label"] == "연결"
        assert fact["value_scope"] == "CUMULATIVE"
        assert result["interpretation_notes"] == [
            "2025 사업연도 연결 누적 수치이며, 별도재무제표 또는 분기 단독 수치와 비교할 때 기준을 맞춰야 합니다."
        ]
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
        transitions = result["trace_summary"]["state_transitions"]
        assert [transition["event"] for transition in transitions] == [
            "turn.started",
            "supervisor.route.selected",
            "agent.started",
            "agent.completed",
            "evidence.checked",
            "supervisor.answer.completed",
        ]
        evidence = result["evidence"][0]
        assert evidence["report_name"] == "SAMSUNG_ELECTRONICS 2025 사업보고서"
        assert evidence["rcept_no"] == "20260310002820"
        assert evidence["source_url"] == (
            "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260310002820"
        )
        assert "section" in evidence
        assert evidence["section"] is None
    finally:
        await client.aclose()


async def test_jsonl_clarification_is_terminal(workflow) -> None:
    app = create_app(workflow)
    client, response = await _post(
        app,
        {
            "conversation_id": "api-t2",
            "client_request_id": "client-002",
            "message": "삼전 영업이익이 하닉에 따였다던데 사실이야?",
        },
    )
    try:
        events = [json.loads(line) for line in response.text.splitlines() if line]
        assert events[-1]["type"] == "clarification.required"
        data = events[-1]["data"]
        assert data["missing_slots"] == ["period_mode"]
        assert data["collected_slots"]["company"] == "SAMSUNG_ELECTRONICS"
    finally:
        await client.aclose()


async def test_request_rejects_blank_message(workflow) -> None:
    app = create_app(workflow)
    client, response = await _post(
        app,
        {
            "conversation_id": None,
            "client_request_id": "client-003",
            "message": "   ",
        },
    )
    try:
        assert response.status_code == 422
    finally:
        await client.aclose()


async def test_supplied_conversation_id_is_stable(workflow) -> None:
    app = create_app(workflow)
    client, response = await _post(
        app,
        {
            "conversation_id": "stable-conversation-id",
            "client_request_id": "client-stable",
            "message": "2025년 삼성전자 연결 영업이익은?",
        },
    )
    try:
        events = [json.loads(line) for line in response.text.splitlines() if line]
        assert {event["conversation_id"] for event in events} == {"stable-conversation-id"}
    finally:
        await client.aclose()
