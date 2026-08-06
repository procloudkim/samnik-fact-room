from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from .domain import (
    Actor,
    ChatTurnRequest,
    EventType,
    FinalAnswer,
    WorkflowEvent,
    Route,
    TurnResult,
    UnavailableReason,
    Verdict,
    WorkflowStatus,
)
from .formatting import (
    format_korean_krw,
    format_krw_with_approximation,
    format_period_label,
    format_trillion_krw,
)


PublicEventType = Literal[
    "run.started",
    "workflow.progress",
    "clarification.required",
    "answer.completed",
    "run.failed",
]


class PublicEvent(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    request_id: str
    conversation_id: str
    seq: int = Field(ge=0)
    emitted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    type: PublicEventType
    data: dict[str, Any]


_STAGE_LABELS = {
    "SUPERVISOR_ROUTING": "Supervisor가 질문 의도와 조건을 확인합니다",
    "SCENARIO_SLOT_CHECK": "멀티턴 Agent가 부족한 조건을 확인합니다",
    "SQL_RETRIEVAL": "RAG Agent가 정형 재무 수치를 조회합니다",
    "VECTOR_RETRIEVAL": "RAG Agent가 공시 본문을 검색합니다",
    "CONTEXT_ANALYSIS": "질문별 분석 Agent가 공시 맥락과 직접 근거를 종합합니다",
    "EXPLANATION_SYNTHESIS": "재무 해설 Tutor가 숫자와 공시의 의미를 쉽게 풉니다",
    "EVIDENCE_VALIDATION": "기간·단위·공시 근거를 검증합니다",
    "SUPERVISOR_FINALIZING": "Supervisor가 근거 범위 안에서 답변을 구성합니다",
}

_PUBLIC_EVIDENCE_LIMIT = 4
_PUBLIC_EXCERPT_LIMIT = 420

_EXCERPT_FOCUS_TERMS = {
    "INVENTORY": ("재고자산", "평가충당금", "재고"),
    "RECEIVABLES": ("매출채권", "대손충당금", "기대신용손실", "채권의 회수"),
    "CASH_CYCLE": ("운전자본", "매입채무", "영업활동현금흐름"),
    "IMPAIRMENT": ("손상", "충당금", "신용손실"),
    "KAM": ("핵심감사사항", "핵심감사"),
    "AUDIT_OPINION": ("감사의견", "검토보고서", "감사보고서"),
    "ESTIMATION": ("회계추정", "공정가치", "손상"),
    "CONTROL": ("내부회계", "내부통제"),
}


def _safe_summary_value(value: object) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _fact_row(fact) -> dict[str, Any]:
    company_labels = {
        "SAMSUNG_ELECTRONICS": "삼성전자",
        "SK_HYNIX": "SK하이닉스",
    }
    metric_labels = {
        "revenue": "매출액",
        "operating_profit": "영업이익",
        "net_income": "당기순이익",
        "total_assets": "자산총계",
        "total_liabilities": "부채총계",
        "total_equity": "자본총계",
        "operating_cash_flow": "영업활동현금흐름",
    }
    raw_value = format(fact.value, "f")
    trillion_krw = fact.value / Decimal("1000000000000")
    is_annual = len(fact.period) == 4 and fact.period.isdigit()
    value_display = format_korean_krw(fact.value)
    return {
        "company": fact.company.value,
        "company_label": company_labels[fact.company.value],
        "metric": fact.metric.value,
        "metric_label": metric_labels[fact.metric.value],
        "value": raw_value,
        "raw_value": raw_value,
        "raw_value_display": f"{fact.value:,.0f}원",
        "source_raw_amount": fact.source_raw_amount,
        "source_unit_scale": fact.source_unit_scale,
        "value_display": value_display,
        "value_trillion_krw": format(trillion_krw.normalize(), "f"),
        "value_trillion_krw_display": format_trillion_krw(fact.value),
        "unit": fact.unit,
        "period": fact.period,
        "period_start": fact.period_start.isoformat() if fact.period_start else None,
        "period_end": fact.period_end.isoformat() if fact.period_end else None,
        "report_code": fact.report_code,
        "period_type": "ANNUAL" if is_annual else "INTERIM",
        "period_type_label": "연간" if is_annual else "중간보고",
        "period_label": (
            f"{fact.period} 사업연도"
            if is_annual
            else f"{format_period_label(fact.period)} 누적"
        ),
        "fs_div": fact.fs_div.value,
        "fs_div_label": "연결" if fact.fs_div.value == "CFS" else "별도",
        "value_scope": fact.value_scope.value,
        "section": fact.section,
        "value_scope_label": (
            "누적" if fact.value_scope.value == "CUMULATIVE" else "시점"
        ),
        "basis": f"{fact.fs_div.value}·{fact.value_scope.value}",
        "evidence_ids": [fact.evidence_id],
    }


def _interpretation_notes(answer: FinalAnswer) -> list[str]:
    notes: list[str] = []
    for fact in answer.facts:
        if len(fact.period) == 4 and fact.period.isdigit():
            note = (
                f"{fact.period} 사업연도 연결 누적 수치이며, 별도재무제표 또는 "
                "분기 단독 수치와 비교할 때 기준을 맞춰야 합니다."
            )
        else:
            note = (
                f"{format_period_label(fact.period)} 연결 누적 수치이며, "
                "연간 또는 분기 단독 수치와 "
                "비교할 때 보고기간을 맞춰야 합니다."
            )
        if note not in notes:
            notes.append(note)
    return notes


def _public_verdict(answer: FinalAnswer) -> str:
    if answer.workflow_status == WorkflowStatus.SYSTEM_ERROR:
        return "SYSTEM_UNAVAILABLE"
    if answer.unavailable_reason in {
        UnavailableReason.CORPUS_INCOMPLETE,
        UnavailableReason.SOURCE_OUT_OF_SCOPE,
    }:
        return "INSUFFICIENT_EVIDENCE"
    if answer.verdict == Verdict.PARTIALLY_CONFIRMED:
        return "REVIEW_REQUIRED"
    if answer.verdict == Verdict.INSUFFICIENT_INFO and answer.evidence:
        return "REVIEW_REQUIRED"
    if answer.verdict in {
        Verdict.NO_OFFICIAL_EVIDENCE,
        Verdict.INSUFFICIENT_INFO,
    }:
        return "INSUFFICIENT_EVIDENCE"
    return answer.verdict.value


def _result_kind(answer: FinalAnswer) -> str:
    verdict = _public_verdict(answer)
    if verdict == "SYSTEM_UNAVAILABLE":
        return "system_error"
    if verdict == Verdict.OUT_OF_SCOPE.value:
        return "out_of_scope"
    if verdict == "INSUFFICIENT_EVIDENCE":
        return "no_evidence"
    if answer.comparison_basis is not None and len(answer.facts) == 2:
        return "comparison"
    if answer.facts:
        return "single_fact"
    return "narrative_evidence"


def _public_excerpt(
    excerpt: str | None,
    focus_terms: tuple[str, ...] = (),
) -> tuple[str | None, bool]:
    """Return a concise display excerpt while preserving the stored source verbatim."""

    if excerpt is None:
        return None, False
    cleaned = re.sub(r"&(?:cr|#0*13|#x0*d);?", "\n", excerpt, flags=re.IGNORECASE)
    cleaned = html.unescape(cleaned)
    cleaned = cleaned.replace("\u00a0", " ")
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = re.sub(r"[\t\f\v ]+", " ", cleaned)
    cleaned = re.sub(r" *\n *", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    if len(cleaned) <= _PUBLIC_EXCERPT_LIMIT:
        return cleaned, False

    focus = next(
        (
            cleaned.lower().find(term.lower())
            for term in focus_terms
            if cleaned.lower().find(term.lower()) >= 0
        ),
        -1,
    )
    if focus >= 0:
        start = max(0, focus - 100)
        previous_break = cleaned.rfind("\n", start, focus)
        if previous_break >= 0:
            start = previous_break + 1
        return cleaned[start : start + _PUBLIC_EXCERPT_LIMIT].strip(), True

    candidate = cleaned[:_PUBLIC_EXCERPT_LIMIT]
    sentence_end = max(candidate.rfind("."), candidate.rfind("다."), candidate.rfind("요."))
    if sentence_end >= 280:
        return candidate[: sentence_end + 1], True
    return candidate, True


def _public_progress_stage(event_type: EventType, event: WorkflowEvent) -> str | None:
    if event_type == EventType.SUPERVISOR_ROUTED and event.producer == Actor.SUPERVISOR:
        return "SUPERVISOR_ROUTING"
    if (
        event_type == EventType.AGENT_STARTED
        and event.producer == Actor.SCENARIO_AGENT
    ):
        return "SCENARIO_SLOT_CHECK"
    if event_type == EventType.AGENT_STARTED and event.producer == Actor.RAG_AGENT:
        mode = event.payload.get("retrieval_mode")
        if mode == "SQL":
            return "SQL_RETRIEVAL"
        if mode == "VECTOR":
            return "VECTOR_RETRIEVAL"
    if (
        event_type == EventType.AGENT_STARTED
        and event.producer == Actor.ANALYSIS_AGENT
    ):
        if event.payload.get("retrieval_mode") == "EXPLANATION_SYNTHESIS":
            return "EXPLANATION_SYNTHESIS"
        return "CONTEXT_ANALYSIS"
    if event_type == EventType.EVIDENCE_CHECKED and event.producer == Actor.EVIDENCE_GATE:
        return "EVIDENCE_VALIDATION"
    if event_type == EventType.ANSWER_COMPLETED and event.producer == Actor.SUPERVISOR:
        return "SUPERVISOR_FINALIZING"
    return None


def _public_progress_status(event: WorkflowEvent) -> str:
    event_type = event.event_type
    payload = event.payload

    if event_type == EventType.SUPERVISOR_ROUTED:
        return "routing"
    if event_type == EventType.AGENT_STARTED:
        return "started"
    if event_type == EventType.AGENT_COMPLETED:
        return "completed"
    if event_type == EventType.EVIDENCE_CHECKED:
        if payload.get("passed") is False:
            return "failed"
        if payload.get("passed") is True:
            return "passed"
        return "checked"
    if event_type == EventType.ANSWER_COMPLETED:
        return "finalizing"
    return "updated"


def _public_progress_tool(event: WorkflowEvent) -> str:
    if event.payload.get("agent"):
        return str(event.payload["agent"])
    if event.producer == Actor.SCENARIO_AGENT:
        return "SCENARIO_AGENT"
    if event.producer == Actor.RAG_AGENT:
        return "RAG_FAQ"
    if event.producer == Actor.EVIDENCE_GATE:
        return "EVIDENCE_GATE"
    if event.producer == Actor.ANALYSIS_AGENT:
        return "CONTEXT_DISCLOSURE_ANALYST"
    if event.producer == Actor.SUPERVISOR:
        return "SUPERVISOR"
    return event.producer.value


def _public_progress_summary(
    event: WorkflowEvent,
    result: TurnResult | None = None,
) -> dict[str, Any]:
    payload = event.payload
    summary: dict[str, Any] = {}
    if (route := payload.get("route")) is not None:
        summary["route"] = _safe_summary_value(route)
    if (status := payload.get("workflow_status")) is not None:
        summary["workflow_status"] = _safe_summary_value(status)
    if (returned_to := payload.get("returned_to")) is not None:
        summary["returned_to"] = _safe_summary_value(returned_to)
    if (returned_from := payload.get("returned_from")) is not None:
        summary["returned_from"] = _safe_summary_value(returned_from)
    if event.payload.get("retrieval_mode") is not None:
        summary["retrieval_mode"] = _safe_summary_value(
            event.payload["retrieval_mode"]
        )
    if event.payload.get("facts") is not None and isinstance(
        event.payload.get("facts"), int
    ):
        summary["facts_found"] = event.payload["facts"]
    if event.payload.get("evidence") is not None and isinstance(
        event.payload.get("evidence"), int
    ):
        summary["evidence_found"] = event.payload["evidence"]
    if event.event_type == EventType.ANSWER_COMPLETED and result is not None:
        summary["workflow_status"] = result.answer.workflow_status.value
    return summary


def _public_progress_events(result: TurnResult) -> list[dict[str, Any]]:
    """Build public progress events directly from internal workflow events."""

    seen_stages: set[str] = set()
    entries: list[dict[str, Any]] = []
    for event in result.events:
        stage = _public_progress_stage(event.event_type, event)
        if stage is None or stage in seen_stages:
            continue
        seen_stages.add(stage)
        if event.payload.get("agent") is not None:
            agent = str(event.payload["agent"])
        elif event.payload.get("final_actor") is not None:
            agent = str(event.payload["final_actor"])
        else:
            agent = event.producer.value
        payload: dict[str, Any] = {
            "stage": stage,
            "label": _STAGE_LABELS[stage],
            "status": _public_progress_status(event),
            "agent": agent,
            "tool": _public_progress_tool(event),
        }
        summary = _public_progress_summary(event, result)
        if summary:
            payload["summary"] = summary
        entries.append(payload)

    if "SUPERVISOR_FINALIZING" not in seen_stages:
        entries.append(
            {
                "stage": "SUPERVISOR_FINALIZING",
                "label": _STAGE_LABELS["SUPERVISOR_FINALIZING"],
                "status": "completed",
                "agent": Actor.SUPERVISOR.value,
                "tool": "SUPERVISOR",
                "summary": {"workflow_status": result.answer.workflow_status.value},
            }
        )

    return entries


def to_public_progress_event(
    request: ChatTurnRequest,
    event: WorkflowEvent,
    *,
    seq: int,
) -> PublicEvent | None:
    """Map one completed workflow milestone without exposing its raw payload."""

    stage = _public_progress_stage(event.event_type, event)
    if stage is None:
        return None
    data: dict[str, Any] = {
        "stage": stage,
        "label": _STAGE_LABELS[stage],
        "status": _public_progress_status(event),
        "agent": str(event.payload.get("agent") or event.producer.value),
        "tool": _public_progress_tool(event),
    }
    summary = _public_progress_summary(event)
    if summary:
        data["summary"] = summary
    return PublicEvent(
        request_id=request.client_request_id,
        conversation_id=event.conversation_id,
        seq=seq,
        type="workflow.progress",
        data=data,
    )


def _trace_summary(result: TurnResult) -> dict[str, Any]:
    calls = [
        event.payload.get("agent")
        for event in result.events
        if event.event_type == EventType.AGENT_STARTED and event.payload.get("agent")
    ]
    model_usage = next(
        (
            event.payload["model_usage"]
            for event in result.events
            if isinstance(event.payload.get("model_usage"), dict)
        ),
        None,
    )
    return {
        "route": result.answer.route.value,
        "agent_calls": calls,
        "state_transitions": [
            {
                "sequence": event.sequence,
                "actor": event.producer.value,
                "event": event.event_type.value,
                **(
                    {"route": str(event.payload["route"])}
                    if event.payload.get("route") is not None
                    else {}
                ),
                **(
                    {"workflow_status": str(event.payload["workflow_status"])}
                    if event.payload.get("workflow_status") is not None
                    else {}
                ),
                **(
                    {"agent": event.payload["agent"]}
                    if event.payload.get("agent") is not None
                    else {}
                ),
                **(
                    {"retrieval_mode": event.payload["retrieval_mode"]}
                    if event.payload.get("retrieval_mode") is not None
                    else {}
                ),
                **(
                    {"returned_to": str(event.payload["returned_to"])}
                    if event.payload.get("returned_to") is not None
                    else {}
                ),
                **(
                    {"returned_from": str(event.payload["returned_from"])}
                    if event.payload.get("returned_from") is not None
                    else {}
                ),
            }
            for event in result.events
        ],
        "final_actor": Actor.SUPERVISOR.value,
        "vector_search_called": any(
            event.payload.get("retrieval_mode") == "VECTOR" for event in result.events
        ),
        "elice_mlapi_called": any(
            event.payload.get("model_provider") == "elice_mlapi" for event in result.events
        ),
        "model_usage": model_usage,
    }


def _question_focus_terms(question: str) -> tuple[str, ...]:
    if re.search(r"\bDS\b|DS\s*부문", question, re.IGNORECASE) and re.search(
        r"영업이익|적자|흑자|손실",
        question,
    ):
        return ("영업이익", "DS 부문", "DS")
    if "연구개발" in question or re.search(r"\bR\s*&\s*D\b", question, re.IGNORECASE):
        return ("연구개발", "R&D")
    return ()


def _answer_result(result: TurnResult, *, question: str = "") -> dict[str, Any]:
    answer = result.answer
    verdict = _public_verdict(answer)
    kind = _result_kind(answer)
    selected_evidence = list(answer.evidence[:_PUBLIC_EVIDENCE_LIMIT])
    if answer.analysis_report is not None:
        evidence_by_id = {item.evidence_id: item for item in answer.evidence}
        selected_evidence = []
        selected_ids: set[str] = set()
        for finding in answer.analysis_report.findings:
            for evidence_id in finding.evidence_ids:
                if evidence_id in selected_ids or evidence_id not in evidence_by_id:
                    continue
                selected_evidence.append(evidence_by_id[evidence_id])
                selected_ids.add(evidence_id)
                break
            if len(selected_evidence) >= _PUBLIC_EVIDENCE_LIMIT:
                break
        for item in answer.evidence:
            if len(selected_evidence) >= _PUBLIC_EVIDENCE_LIMIT:
                break
            if item.evidence_id not in selected_ids:
                selected_evidence.append(item)
                selected_ids.add(item.evidence_id)

    evidence = []
    focus_by_evidence_id: dict[str, tuple[str, ...]] = {}
    if answer.analysis_report is not None:
        for finding in answer.analysis_report.findings:
            focus_terms = _EXCERPT_FOCUS_TERMS.get(finding.category_key, ())
            for evidence_id in finding.evidence_ids:
                focus_by_evidence_id[evidence_id] = focus_terms
    default_focus_terms = _question_focus_terms(question)
    for item in selected_evidence:
        excerpt, excerpt_truncated = _public_excerpt(
            item.excerpt,
            focus_by_evidence_id.get(item.evidence_id, default_focus_terms),
        )
        evidence.append(
            {
                "evidence_id": item.evidence_id,
                "report_name": item.filing_title,
                "rcept_no": item.rcept_no,
                "source_url": item.url,
                "rcept_dt": item.filed_at.isoformat(),
                "section": item.section_path,
                "excerpt": excerpt,
                "excerpt_truncated": excerpt_truncated,
            }
        )
    titles = {
        "CONFIRMED": "공시에서 확인했습니다",
        "CONTRADICTED": "공시 내용과 다릅니다",
        "REVIEW_REQUIRED": "관련 공시는 확인됐으며 종합 판단이 필요합니다",
        "INSUFFICIENT_EVIDENCE": "선택한 공시 범위에서 직접 근거를 찾지 못했습니다",
        "OUT_OF_SCOPE": "실적은 확인할 수 있습니다",
        "SYSTEM_UNAVAILABLE": "지금은 확인할 수 없습니다",
    }
    if answer.analysis_report is None:
        title = titles[verdict]
    elif answer.analysis_report.methodology == "FINANCIAL_RATIO_CALCULATION_V1":
        title = f"{answer.analysis_report.lens_label} 한눈에 보기"
    else:
        title = f"{answer.analysis_report.lens_label} 핵심 신호"
    payload: dict[str, Any] = {
        "kind": kind,
        "status": verdict,
        "verdict": verdict,
        "workflow_status": answer.workflow_status.value,
        "failure_reason": (
            answer.unavailable_reason.value if answer.unavailable_reason else None
        ),
        "title": title,
        "summary": answer.conclusion,
        "interpretation": {
            "CONFIRMED": "아래 공식 공시가 답변 내용을 뒷받침합니다.",
            "CONTRADICTED": "아래 공식 공시를 보면 입력한 내용과 실제 내용이 다릅니다.",
            "REVIEW_REQUIRED": "공시 근거는 확인됐으며, 결론에 필요한 추가 확인 항목을 함께 표시했습니다.",
            "INSUFFICIENT_EVIDENCE": "근거를 확인하지 못했다는 사실만으로 해당 내용이 거짓이라고 단정할 수 없습니다.",
            "SYSTEM_UNAVAILABLE": "확인하지 못한 숫자는 추측해서 답하지 않았습니다.",
            "OUT_OF_SCOPE": "매매 결정 대신 실제 실적을 공시 기준으로 확인할 수 있습니다.",
        }[verdict],
        "facts": [_fact_row(fact) for fact in answer.facts],
        "evidence": evidence,
        "evidence_total_count": len(answer.evidence),
        "as_of": answer.as_of.isoformat(),
        "data_status": (
            "STALE_CACHE"
            if answer.stale_data_warning
            else ("NO_DATA" if kind in {"no_evidence", "system_error"} else "FRESH")
        ),
        "limitations": [
            item
            for item in answer.limitations
            if item != "UNTRUSTED_INSTRUCTION_REQUEST"
        ],
        "interpretation_notes": _interpretation_notes(answer),
        "trace_summary": _trace_summary(result),
    }
    if answer.analysis_report is not None:
        public_evidence_ids = {item["evidence_id"] for item in evidence}
        report = answer.analysis_report
        payload["analysis_report"] = {
            "lens_key": report.lens_key,
            "lens_label": report.lens_label,
            "methodology": report.methodology,
            "source_count": report.source_count,
            "limitation": report.limitation,
            "findings": [
                {
                    "category_key": finding.category_key,
                    "category_label": finding.category_label,
                    "summary": finding.summary,
                    "plain_language": finding.plain_language,
                    "next_check": finding.next_check,
                    "evidence_ids": [
                        evidence_id
                        for evidence_id in finding.evidence_ids
                        if evidence_id in public_evidence_ids
                    ],
                }
                for finding in report.findings
            ],
        }
        payload["interpretation"] = report.limitation
    if answer.comparison_basis is not None:
        payload["comparison_basis"] = answer.comparison_basis.model_dump(mode="json")
        if len(answer.facts) == 2:
            difference = answer.facts[0].value - answer.facts[1].value
            payload["difference_display"] = (
                f"차이 {format_krw_with_approximation(abs(difference))}"
            )
    if kind == "no_evidence":
        scopes = [item.removeprefix("검색 범위: ") for item in answer.limitations if item.startswith("검색 범위:")]
        payload["searched_scope"] = scopes[0] if scopes else None
    if kind == "out_of_scope":
        security_refusal = "UNTRUSTED_INSTRUCTION_REQUEST" in answer.limitations
        payload["reason"] = (
            "UNTRUSTED_INSTRUCTION_REQUEST"
            if security_refusal
            else "ADVICE_OR_PREDICTION_REQUEST"
        )
        payload["suggested_questions"] = (
            [
                "삼성전자의 최근 연결 영업이익을 알려줘",
                "공식 공시에서 재무건전성 근거를 확인해줘",
            ]
            if security_refusal
            else [
                "삼성전자의 최근 연결 영업이익을 알려줘",
                "두 회사의 최근 공통 분기 영업이익을 비교해줘",
            ]
        )
    if kind == "system_error":
        payload["code"] = answer.unavailable_reason.value if answer.unavailable_reason else "SYSTEM_ERROR"
        payload["retryable"] = True
    return payload


def to_public_events(request: ChatTurnRequest, result: TurnResult) -> list[PublicEvent]:
    conversation_id = result.events[0].conversation_id
    request_id = request.client_request_id
    events: list[PublicEvent] = []

    def append(event_type: PublicEventType, data: dict[str, Any]) -> None:
        events.append(
            PublicEvent(
                request_id=request_id,
                conversation_id=conversation_id,
                seq=len(events),
                type=event_type,
                data=data,
            )
        )

    append("run.started", {"message": request.message})
    for progress in _public_progress_events(result):
        append("workflow.progress", progress)

    failed_event = next(
        (
            event
            for event in result.events
            if event.event_type == EventType.TURN_FAILED
        ),
        None,
    )
    if failed_event is not None and failed_event.payload.get("error_code") in {
        UnavailableReason.REPOSITORY_UNAVAILABLE.value,
        UnavailableReason.MODEL_PROVIDER_UNAVAILABLE.value,
    }:
        append(
            "answer.completed",
            {"result": _answer_result(result, question=request.message)},
        )
        return events

    if failed_event is not None:
        append(
            "run.failed",
            {
                "code": "UNEXPECTED_WORKFLOW_FAILURE",
                "message": "요청을 처리하는 중 예기치 않은 오류가 발생했습니다.",
                "retryable": True,
            },
        )
        return events

    if result.answer.workflow_status == WorkflowStatus.NEED_MORE_INFO:
        scenario_event = next(
            (
                event
                for event in reversed(result.events)
                if event.producer == Actor.SCENARIO_AGENT
                and event.event_type == EventType.AGENT_COMPLETED
            ),
            None,
        )
        payload = scenario_event.payload if scenario_event else {}
        collected = {
            key: str(value)
            for key, value in payload.get("collected_slots", {}).items()
            if value is not None
        }
        append(
            "clarification.required",
            {
                "question": result.answer.conclusion,
                "collected_slots": collected,
                "missing_slots": payload.get("missing_slots", []),
            },
        )
    else:
        append(
            "answer.completed",
            {"result": _answer_result(result, question=request.message)},
        )
    return events
