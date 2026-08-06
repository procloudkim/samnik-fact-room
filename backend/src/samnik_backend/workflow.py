from __future__ import annotations

import operator
import re
import traceback
from uuid import uuid4
from datetime import UTC, datetime
from typing import Annotated, AsyncIterator, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from .agents import (
    EvidenceGate,
    FinancialLiteracyTutorAgent,
    MeceDisclosureAnalysisAgent,
    MultiTurnScenarioAgent,
    RagFaqAgent,
    build_normalized_query,
    default_value_scope,
    financial_claims,
    financial_diagnosis_lens_for,
    is_financial_diagnosis_query,
    narrative_claims,
    required_missing_slots,
)
from .domain import (
    Actor,
    AnswerClaim,
    ChatTurnRequest,
    ChatTurnContext,
    ClaimKind,
    ConversationRecord,
    EventType,
    FinalAnswer,
    FsDiv,
    Intent,
    NormalizedQuery,
    RagResult,
    Route,
    RouteDecision,
    ScenarioResult,
    Slots,
    TurnResult,
    UnavailableReason,
    Verdict,
    WorkflowEvent,
    WorkflowStatus,
)
from .errors import EvidenceValidationError, ProviderUnavailable, RepositoryUnavailable
from .formatting import format_krw_with_approximation, format_period_label
from .model_usage import ModelUsageRecord, unreported_model_usage
from .ports import ConversationRepository, LanguageModelPort
from .providers import RuleBasedLanguageModel
from .prompt_security import assess_user_message


class GraphState(TypedDict, total=False):
    request: ChatTurnRequest
    prior: ConversationRecord | None
    decision: RouteDecision
    intent: Intent
    slots: Slots
    route: Route
    normalized_query: NormalizedQuery
    scenario_result: ScenarioResult
    rag_result: RagResult
    evidence_validated: bool
    next_action: Literal["rag", "finalize"]
    answer: FinalAnswer
    error_message: str
    classification_fallback: bool
    events: Annotated[list[WorkflowEvent], operator.add]


def _event(
    state: GraphState,
    event_type: EventType,
    producer: Actor,
    payload: dict | None = None,
    *,
    offset: int = 1,
) -> WorkflowEvent:
    return WorkflowEvent(
        sequence=len(state.get("events", [])) + offset,
        event_type=event_type,
        conversation_id=state["request"].conversation_id,
        producer=producer,
        payload=payload or {},
    )


class FactRoomWorkflow:
    """Thin LangGraph orchestration with explicit agent return boundaries."""

    def __init__(
        self,
        *,
        language_model: LanguageModelPort,
        conversations: ConversationRepository,
        scenario_agent: MultiTurnScenarioAgent,
        rag_agent: RagFaqAgent,
        evidence_gate: EvidenceGate,
        analysis_agent: MeceDisclosureAnalysisAgent | None = None,
        tutor_agent: FinancialLiteracyTutorAgent | None = None,
        checkpointer=None,
    ) -> None:
        self._language_model = language_model
        self._conversations = conversations
        self._scenario_agent = scenario_agent
        self._rag_agent = rag_agent
        self._evidence_gate = evidence_gate
        self._analysis_agent = analysis_agent or MeceDisclosureAnalysisAgent()
        self._tutor_agent = tutor_agent or FinancialLiteracyTutorAgent()
        self._graph = self._build_graph(checkpointer=checkpointer)

    def _build_graph(self, *, checkpointer=None):
        graph = StateGraph(GraphState)
        graph.add_node("supervisor_ingress", self._supervisor_ingress)
        graph.add_node("scenario_agent", self._run_scenario)
        graph.add_node("supervisor_after_scenario", self._supervisor_after_scenario)
        graph.add_node("rag_agent", self._run_rag)
        graph.add_node("analysis_agent", self._run_analysis)
        graph.add_node("tutor_agent", self._run_tutor)
        graph.add_node("evidence_gate", self._run_evidence_gate)
        graph.add_node("supervisor_finalize", self._supervisor_finalize)

        graph.add_edge(START, "supervisor_ingress")
        graph.add_conditional_edges(
            "supervisor_ingress",
            self._route_after_ingress,
            {
                "scenario": "scenario_agent",
                "rag": "rag_agent",
                "finalize": "supervisor_finalize",
            },
        )
        graph.add_edge("scenario_agent", "supervisor_after_scenario")
        graph.add_conditional_edges(
            "supervisor_after_scenario",
            lambda state: state["next_action"],
            {"rag": "rag_agent", "finalize": "supervisor_finalize"},
        )
        graph.add_conditional_edges(
            "rag_agent",
            self._route_after_rag,
            {
                "analysis": "analysis_agent",
                "tutor": "tutor_agent",
                "evidence": "evidence_gate",
            },
        )
        graph.add_conditional_edges(
            "analysis_agent",
            lambda state: (
                "tutor" if self._tutor_agent.supports(state["rag_result"]) else "evidence"
            ),
            {"tutor": "tutor_agent", "evidence": "evidence_gate"},
        )
        graph.add_edge("tutor_agent", "evidence_gate")
        graph.add_edge("evidence_gate", "supervisor_finalize")
        graph.add_edge("supervisor_finalize", END)
        return graph.compile(checkpointer=checkpointer)

    async def run_turn(self, request: ChatTurnRequest) -> TurnResult:
        events: list[WorkflowEvent] = []
        answer: FinalAnswer | None = None
        async for event in self.stream_turn(request):
            events.append(event)
            if event.event_type == EventType.ANSWER_COMPLETED:
                answer = FinalAnswer.model_validate(event.payload["answer"])
        if answer is None:  # pragma: no cover - stream_turn always closes safely
            raise RuntimeError("workflow stream ended without a terminal answer")
        return TurnResult(events=events, answer=answer)

    async def stream_turn(self, request: ChatTurnRequest) -> AsyncIterator[WorkflowEvent]:
        """Yield sanitized internal milestones while LangGraph nodes complete."""

        if request.conversation_id is None:
            request = request.model_copy(update={"conversation_id": str(uuid4())})
        assert request.conversation_id is not None
        execution_id = str(uuid4())
        started = WorkflowEvent(
            sequence=1,
            event_type=EventType.TURN_STARTED,
            conversation_id=request.conversation_id,
            producer=Actor.SYSTEM,
            payload={
                "message_length": len(request.message),
                "client_request_id": request.client_request_id,
            },
        )
        emitted: list[WorkflowEvent] = [started]
        yield started
        try:
            async for update in self._graph.astream(
                {"request": request, "events": [started]},
                config={
                    "configurable": {
                        # `client_request_id` is a public correlation ID, not a
                        # checkpoint identity. Reuse must start a fresh graph
                        # run or stale nodes/events can leak into the response.
                        "thread_id": f"{request.conversation_id}:{execution_id}",
                    }
                },
                stream_mode="updates",
            ):
                for node_update in update.values():
                    if not isinstance(node_update, dict):
                        continue
                    for event in node_update.get("events", []):
                        emitted.append(event)
                        yield event
            if not any(
                event.event_type == EventType.ANSWER_COMPLETED for event in emitted
            ):
                raise RuntimeError("workflow graph ended without a terminal answer")
        except Exception as exc:  # pragma: no cover - last-resort stream safety
            now = datetime.now(UTC)
            if isinstance(exc, RepositoryUnavailable):
                error_code = UnavailableReason.REPOSITORY_UNAVAILABLE.value
                unavailable_reason = UnavailableReason.REPOSITORY_UNAVAILABLE
            elif isinstance(exc, ProviderUnavailable):
                error_code = UnavailableReason.MODEL_PROVIDER_UNAVAILABLE.value
                unavailable_reason = UnavailableReason.MODEL_PROVIDER_UNAVAILABLE
            else:
                error_code = "UNEXPECTED_WORKFLOW_FAILURE"
                unavailable_reason = None
            failed = WorkflowEvent(
                sequence=len(emitted) + 1,
                event_type=EventType.TURN_FAILED,
                conversation_id=request.conversation_id,
                producer=Actor.SYSTEM,
                payload={"error_code": error_code},
            )
            diagnostic = type(exc).__name__
            if unavailable_reason is None:
                frames = traceback.extract_tb(exc.__traceback__)
                if frames:
                    locations = " > ".join(
                        f"{frame.name}:{frame.lineno}" for frame in frames[-5:]
                    )
                    diagnostic = f"{diagnostic} at {locations}"
            answer = FinalAnswer(
                workflow_status=WorkflowStatus.SYSTEM_ERROR,
                verdict=Verdict.INSUFFICIENT_INFO,
                conclusion=(
                    "지금은 공시를 확인할 수 없습니다. 잠시 후 같은 질문으로 다시 시도해 주세요. "
                    "확인하지 못한 숫자는 추측해서 답하지 않았습니다."
                ),
                claims=[],
                evidence=[],
                limitations=[diagnostic],
                unavailable_reason=unavailable_reason,
                as_of=now,
                route=Route.RAG,
            )
            completed = WorkflowEvent(
                sequence=len(emitted) + 2,
                event_type=EventType.ANSWER_COMPLETED,
                conversation_id=request.conversation_id,
                producer=Actor.SUPERVISOR,
                payload={"answer": answer.model_dump(mode="json")},
            )
            yield failed
            yield completed

    async def _supervisor_ingress(self, state: GraphState) -> GraphState:
        request = state["request"]
        prior = await self._conversations.get(request.conversation_id)
        security_assessment = assess_user_message(request.message)
        deterministic_decision = (
            RouteDecision(
                intent=Intent.OUT_OF_SCOPE,
                extracted_slots=Slots(),
                rationale_code=security_assessment.reason_code
                or "UNTRUSTED_INSTRUCTION_REQUEST",
            )
            if security_assessment.blocked
            else await RuleBasedLanguageModel().classify(request.message, prior)
        )
        provisional_continuing = (
            prior is not None
            and prior.workflow_status == WorkflowStatus.NEED_MORE_INFO
            and prior.intent == deterministic_decision.intent
        )
        provisional_slots = self._merge_slots(
            explicit=deterministic_decision.extracted_slots,
            context=request.context,
            prior=prior,
            continuing=provisional_continuing,
            intent=deterministic_decision.intent,
        )
        external_classifier_called = False
        if not security_assessment.blocked and self._needs_external_classifier(
            provider_name=getattr(self._language_model, "provider_name", "external"),
            intent=deterministic_decision.intent,
            slots=provisional_slots,
        ):
            try:
                decision = await self._language_model.classify(request.message, prior)
                external_classifier_called = True
                classification_fallback = False
            except ProviderUnavailable:
                # Classification is not a financial truth boundary. When the
                # external model is temporarily unavailable or returns malformed
                # JSON, keep supported questions usable with the deterministic
                # parser; SQL/vector evidence gates still own the answer.
                decision = deterministic_decision
                classification_fallback = True
        else:
            decision = deterministic_decision
            classification_fallback = False

        provider_name = getattr(self._language_model, "provider_name", "unknown")
        recorded_usage = getattr(self._language_model, "last_usage", None)
        if external_classifier_called and isinstance(recorded_usage, ModelUsageRecord):
            model_usage = recorded_usage
        else:
            model_usage = unreported_model_usage(
                called=False,
                provider=provider_name,
                model=(
                    recorded_usage.model
                    if isinstance(recorded_usage, ModelUsageRecord)
                    else provider_name
                ),
            )

        # The model may extract user-facing slots, but it is not authoritative
        # for temporal or accounting boundaries. Keep `as_of` and CFS under
        # Supervisor control so prompt text cannot move the evidence cutoff.
        server_owned: dict[str, object] = {
            "as_of": datetime.now(UTC),
            "fs_div": FsDiv.CFS,
        }
        continuing = (
            prior is not None
            and prior.workflow_status == WorkflowStatus.NEED_MORE_INFO
            and prior.intent == decision.intent
        )
        slots = self._merge_slots(
            explicit=decision.extracted_slots,
            context=request.context,
            prior=prior,
            continuing=continuing,
            intent=decision.intent,
        )
        metric = slots.metric
        if metric is not None:
            server_owned["value_scope"] = default_value_scope(metric)
        explicit_year = re.search(r"(?<!\d)(20\d{2})(?:\s*년)?(?!\d)", request.message)
        if explicit_year:
            server_owned["period"] = explicit_year.group(1)
        slots = slots.model_copy(update=server_owned)
        decision = decision.model_copy(update={"extracted_slots": slots})
        if decision.intent == Intent.OUT_OF_SCOPE:
            slots = Slots(as_of=datetime.now(UTC))
            route = Route.OUT_OF_SCOPE
        else:
            route = (
                Route.SCENARIO
                if continuing or required_missing_slots(decision.intent, slots)
                else Route.RAG
            )

        payload = {
            "intent": decision.intent,
            "route": route,
            "missing_slots": required_missing_slots(decision.intent, slots),
            "returned_from": None,
            "model_provider": provider_name,
            "model_usage": model_usage.model_dump(mode="json"),
            "classification_fallback": classification_fallback,
        }
        update: GraphState = {
            "prior": prior,
            "decision": decision,
            "intent": decision.intent,
            "slots": slots,
            "route": route,
            "classification_fallback": classification_fallback,
            "events": [_event(state, EventType.SUPERVISOR_ROUTED, Actor.SUPERVISOR, payload)],
        }
        if route == Route.RAG:
            update["normalized_query"] = build_normalized_query(decision.intent, slots)
        return update

    @staticmethod
    def _needs_external_classifier(
        *,
        provider_name: str,
        intent: Intent,
        slots: Slots,
    ) -> bool:
        if intent == Intent.OUT_OF_SCOPE:
            return False
        if provider_name == "local_rules":
            return bool(required_missing_slots(intent, slots))
        return bool(required_missing_slots(intent, slots))

    @staticmethod
    def _merge_slots(
        *,
        explicit: Slots,
        context: ChatTurnContext | None,
        prior: ConversationRecord | None,
        continuing: bool,
        intent: Intent,
    ) -> Slots:
        merged = explicit.model_dump()
        if intent != Intent.COMPARE:
            merged["comparison_company"] = None
        if context is not None:
            context_slots = context.to_slots().model_dump()
            for name, value in context_slots.items():
                if name == "comparison_company" and intent != Intent.COMPARE:
                    continue
                if name == "report_codes":
                    if not merged.get("report_codes"):
                        merged[name] = value
                    continue
                if merged.get(name) is None:
                    merged[name] = value
        if continuing and prior is not None:
            prior_slots = prior.collected_slots.model_dump()
            for name, value in prior_slots.items():
                if name == "comparison_company" and intent != Intent.COMPARE:
                    continue
                if name == "as_of":
                    continue
                if name == "report_codes":
                    if not merged.get("report_codes"):
                        merged[name] = value
                    continue
                if merged.get(name) is None:
                    merged[name] = value
        return Slots.model_validate(merged)

    @staticmethod
    def _route_after_ingress(state: GraphState) -> str:
        if state.get("error_message") or state["route"] == Route.OUT_OF_SCOPE:
            return "finalize"
        return "scenario" if state["route"] == Route.SCENARIO else "rag"

    async def _run_scenario(self, state: GraphState) -> GraphState:
        started = _event(
            state,
            EventType.AGENT_STARTED,
            Actor.SCENARIO_AGENT,
            {"agent": "MULTI_TURN_SCENARIO"},
        )
        result = self._scenario_agent.run(intent=state["intent"], slots=state["slots"])
        record = ConversationRecord(
            conversation_id=state["request"].conversation_id,
            intent=result.intent,
            collected_slots=result.collected_slots,
            missing_slots=result.missing_slots,
            workflow_status=result.workflow_status,
        )
        await self._conversations.put(record)
        completed = _event(
            state,
            EventType.AGENT_COMPLETED,
            Actor.SCENARIO_AGENT,
            {
                "agent": "MULTI_TURN_SCENARIO",
                "workflow_status": result.workflow_status,
                "missing_slots": result.missing_slots,
                "collected_slots": result.collected_slots.model_dump(mode="json", exclude_none=True),
                "next_question": result.next_question,
                "returned_to": Actor.SUPERVISOR,
            },
            offset=2,
        )
        return {
            "scenario_result": result,
            "slots": result.collected_slots,
            "events": [started, completed],
        }

    async def _supervisor_after_scenario(self, state: GraphState) -> GraphState:
        result = state["scenario_result"]
        if result.workflow_status == WorkflowStatus.READY_FOR_RAG:
            assert result.normalized_query is not None
            next_action: Literal["rag", "finalize"] = "rag"
            payload = {
                "route": Route.RAG,
                "workflow_status": result.workflow_status,
                "returned_from": Actor.SCENARIO_AGENT,
            }
            return {
                "normalized_query": result.normalized_query,
                "next_action": next_action,
                "events": [_event(state, EventType.SUPERVISOR_ROUTED, Actor.SUPERVISOR, payload)],
            }
        payload = {
            "route": Route.SCENARIO,
            "workflow_status": result.workflow_status,
            "returned_from": Actor.SCENARIO_AGENT,
        }
        return {
            "next_action": "finalize",
            "events": [_event(state, EventType.SUPERVISOR_ROUTED, Actor.SUPERVISOR, payload)],
        }

    async def _run_rag(self, state: GraphState) -> GraphState:
        query = state["normalized_query"]
        financial_lens = (
            financial_diagnosis_lens_for(query.topic_or_claim or "")
            if is_financial_diagnosis_query(query)
            else None
        )
        retrieval_mode = (
            "SQL"
            if query.intent in {Intent.DIRECT_FACT, Intent.COMPARE} or financial_lens
            else "VECTOR"
        )
        agent_name = (
            f"FINANCIAL_{financial_lens.key}_CALCULATOR"
            if financial_lens
            else "RAG_FAQ"
        )
        started = _event(
            state,
            EventType.AGENT_STARTED,
            Actor.RAG_AGENT,
            {"agent": agent_name, "retrieval_mode": retrieval_mode},
        )
        result = await self._rag_agent.run(query)
        completed = _event(
            state,
            EventType.AGENT_COMPLETED,
            Actor.RAG_AGENT,
            {
                "agent": agent_name,
                "workflow_status": result.workflow_status,
                "verdict": result.verdict,
                "returned_to": Actor.SUPERVISOR,
                "evidence_ids": [item.evidence_id for item in result.evidence],
            },
            offset=2,
        )
        return {"rag_result": result, "events": [started, completed]}

    def _route_after_rag(self, state: GraphState) -> str:
        if self._tutor_agent.supports(state["rag_result"]):
            return "tutor"
        return (
            "analysis"
            if self._analysis_agent.supports(state["rag_result"])
            else "evidence"
        )

    async def _run_analysis(self, state: GraphState) -> GraphState:
        result = state["rag_result"]
        agent_name = self._analysis_agent.agent_name(result)
        started = _event(
            state,
            EventType.AGENT_STARTED,
            Actor.ANALYSIS_AGENT,
            {"agent": agent_name, "retrieval_mode": "CONTEXT_ANALYSIS"},
        )
        analyzed = self._analysis_agent.run(result)
        completed = _event(
            state,
            EventType.AGENT_COMPLETED,
            Actor.ANALYSIS_AGENT,
            {
                "agent": agent_name,
                "workflow_status": analyzed.workflow_status,
                "analysis_lens": (
                    analyzed.analysis_report.lens_key
                    if analyzed.analysis_report
                    else None
                ),
                "findings": (
                    len(analyzed.analysis_report.findings)
                    if analyzed.analysis_report
                    else 0
                ),
                "returned_to": Actor.SUPERVISOR,
            },
            offset=2,
        )
        return {"rag_result": analyzed, "events": [started, completed]}

    async def _run_tutor(self, state: GraphState) -> GraphState:
        result = state["rag_result"]
        started = _event(
            state,
            EventType.AGENT_STARTED,
            Actor.ANALYSIS_AGENT,
            {
                "agent": self._tutor_agent.name,
                "retrieval_mode": "EXPLANATION_SYNTHESIS",
            },
        )
        explained = self._tutor_agent.run(result)
        completed = _event(
            state,
            EventType.AGENT_COMPLETED,
            Actor.ANALYSIS_AGENT,
            {
                "agent": self._tutor_agent.name,
                "workflow_status": explained.workflow_status,
                "findings": len(explained.analysis_report.findings)
                if explained.analysis_report
                else 0,
                "returned_to": Actor.SUPERVISOR,
            },
            offset=2,
        )
        return {"rag_result": explained, "events": [started, completed]}

    async def _run_evidence_gate(self, state: GraphState) -> GraphState:
        result = state["rag_result"]
        try:
            self._evidence_gate.validate_rag(result)
            passed = True
            payload = {
                "passed": True,
                "facts": len(result.facts),
                "evidence": len(result.evidence),
                "comparison_basis": (
                    result.comparison_basis.model_dump(mode="json")
                    if result.comparison_basis
                    else None
                ),
            }
        except EvidenceValidationError as exc:
            passed = False
            payload = {"passed": False, "error_code": "EVIDENCE_CONTRACT_VIOLATION"}
            result = result.model_copy(
                update={
                    "workflow_status": WorkflowStatus.SYSTEM_ERROR,
                    "verdict": Verdict.INSUFFICIENT_INFO,
                    "facts": [],
                    "evidence": [],
                    "limitations": [str(exc)],
                    "unavailable_reason": UnavailableReason.REPOSITORY_UNAVAILABLE,
                }
            )
        return {
            "rag_result": result,
            "evidence_validated": passed,
            "events": [_event(state, EventType.EVIDENCE_CHECKED, Actor.EVIDENCE_GATE, payload)],
        }

    async def _supervisor_finalize(self, state: GraphState) -> GraphState:
        now = datetime.now(UTC)
        route = state["route"]
        if state.get("error_message"):
            answer = FinalAnswer(
                workflow_status=WorkflowStatus.SYSTEM_ERROR,
                verdict=Verdict.INSUFFICIENT_INFO,
                conclusion=(
                    "지금은 질문을 처리할 수 없습니다. 잠시 후 같은 질문으로 다시 시도해 주세요. "
                    "확인하지 못한 숫자는 추측해서 답하지 않았습니다."
                ),
                claims=[],
                evidence=[],
                limitations=[state["error_message"]],
                unavailable_reason=UnavailableReason.MODEL_PROVIDER_UNAVAILABLE,
                as_of=now,
                route=route,
            )
        elif route == Route.OUT_OF_SCOPE:
            security_refusal = (
                state["decision"].rationale_code
                == "UNTRUSTED_INSTRUCTION_REQUEST"
            )
            answer = FinalAnswer(
                workflow_status=WorkflowStatus.UNAVAILABLE,
                verdict=Verdict.OUT_OF_SCOPE,
                conclusion=(
                    "문서나 대화에 포함된 지시로 시스템 규칙을 바꾸거나 비밀값을 공개할 수 없습니다. "
                    "재무 질문으로 바꾸면 공식 공시만 근거로 안전하게 확인해 드립니다."
                    if security_refusal
                    else (
                        "주식을 지금 사야 할지, 팔아야 할지는 대신 결정해 드릴 수 없습니다. "
                        "대신 회사가 실제로 얼마를 벌었는지는 공식 공시로 쉽게 확인해 드릴 수 있습니다."
                    )
                ),
                claims=[
                    AnswerClaim(
                        text=(
                            "문서 속 명령과 비밀값 요청은 실행하지 않습니다."
                            if security_refusal
                            else "매매추천·주가예측은 서비스 범위 밖입니다."
                        ),
                        kind=ClaimKind.POLICY,
                    )
                ],
                evidence=[],
                limitations=(
                    ["UNTRUSTED_INSTRUCTION_REQUEST"]
                    if security_refusal
                    else []
                ),
                as_of=now,
                route=route,
            )
            await self._conversations.delete(state["request"].conversation_id)
        elif "scenario_result" in state and state["scenario_result"].workflow_status == WorkflowStatus.NEED_MORE_INFO:
            scenario = state["scenario_result"]
            answer = FinalAnswer(
                workflow_status=WorkflowStatus.NEED_MORE_INFO,
                verdict=Verdict.INSUFFICIENT_INFO,
                conclusion=scenario.next_question or "판정에 필요한 정보를 더 알려주세요.",
                claims=[],
                evidence=[],
                limitations=[f"누락 슬롯: {', '.join(scenario.missing_slots)}"],
                as_of=scenario.collected_slots.as_of or now,
                route=Route.SCENARIO,
            )
        else:
            result = state["rag_result"]
            claims = (
                financial_claims(result.facts)
                if result.facts
                else narrative_claims(result)
            )
            if result.workflow_status == WorkflowStatus.SYSTEM_ERROR:
                conclusion = (
                    "지금은 공시를 확인할 수 없습니다. 잠시 후 같은 질문으로 다시 시도해 주세요. "
                    "확인하지 못한 숫자는 추측해서 답하지 않았습니다."
                )
            elif result.analysis_report is not None:
                conclusion = self._analysis_conclusion(result)
            elif result.verdict == Verdict.NO_OFFICIAL_EVIDENCE:
                conclusion = (
                    "현재 저장된 공식 공시에서는 이 내용을 확인할 근거를 찾지 못했습니다. "
                    "근거가 없다는 뜻이지, 이 말이 거짓이라고 확정한 것은 아닙니다."
                )
            elif result.verdict == Verdict.INSUFFICIENT_INFO:
                conclusion = (
                    "지금 가지고 있는 공시 자료만으로는 답을 확정할 수 없습니다. "
                    "회사와 기간이 같은 자료가 더 필요합니다."
                )
            else:
                conclusion = self._grounded_conclusion(result)
            self._evidence_gate.validate_claims(
                claims,
                {item.evidence_id for item in result.evidence},
            )
            answer = FinalAnswer(
                workflow_status=result.workflow_status,
                verdict=result.verdict,
                conclusion=conclusion,
                facts=result.facts,
                comparison_basis=result.comparison_basis,
                claims=claims,
                evidence=result.evidence,
                limitations=result.limitations,
                unavailable_reason=result.unavailable_reason,
                stale_data_warning=result.stale_data_warning,
                analysis_report=result.analysis_report,
                as_of=result.searched_until or result.normalized_query.as_of,
                route=Route.RAG,
            )
            await self._conversations.delete(state["request"].conversation_id)

        completed = _event(
            state,
            EventType.ANSWER_COMPLETED,
            Actor.SUPERVISOR,
            {
                "answer": answer.model_dump(mode="json"),
                "final_actor": Actor.SUPERVISOR,
                "model_provider": getattr(self._language_model, "provider_name", "unknown"),
            },
        )
        return {"answer": answer, "events": [completed]}

    @staticmethod
    def _analysis_conclusion(result: RagResult) -> str:
        report = result.analysis_report
        assert report is not None
        if report.methodology == "FINANCIAL_RATIO_CALCULATION_V1":
            company_labels = {
                "SAMSUNG_ELECTRONICS": "삼성전자",
                "SK_HYNIX": "SK하이닉스",
            }
            company = company_labels[result.facts[0].company.value]
            period = format_period_label(result.facts[0].period)
            findings = " ".join(finding.summary for finding in report.findings)
            return (
                f"{company}의 {period} 연결 기준 {report.lens_label}을 계산했습니다. "
                f"{findings} 아래에서 이 숫자가 사업과 현금흐름에 무엇을 뜻하는지, "
                "다음 공시에서 어떤 항목을 이어 봐야 하는지 설명합니다."
            )
        categories = ", ".join(finding.category_label for finding in report.findings)
        return (
            f"관련 공식 공시 {report.source_count}건을 {categories} 영역으로 나눠 검토했습니다. "
            "아래에서 확인된 수치와 문맥, 투자자가 이해해야 할 의미, 다음 공시에서 "
            "이어 볼 항목을 원문 근거와 함께 설명합니다."
        )

    @staticmethod
    def _grounded_conclusion(result: RagResult) -> str:
        """Render only values that already passed the evidence gate.

        The language model participates in routing and slot extraction, but its
        free-form draft is never a public-answer trust boundary.
        """

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
        metric_explanations = {
            "revenue": "'매출액'은 제품이나 서비스를 팔아 번 전체 금액입니다.",
            "operating_profit": "'영업이익'은 회사가 본업으로 번 이익입니다.",
            "net_income": "'당기순이익'은 비용과 세금까지 빼고 최종적으로 남은 이익입니다.",
            "total_assets": "'자산총계'는 회사가 가진 현금, 설비, 권리 등을 모두 합친 금액입니다.",
            "total_liabilities": "'부채총계'는 회사가 갚아야 할 차입금과 영업상 의무를 합한 금액입니다.",
            "total_equity": "'자본총계'는 자산에서 부채를 뺀 주주 몫의 장부가치입니다.",
            "operating_cash_flow": "'영업활동현금흐름'은 본업에서 실제로 들어오고 나간 현금의 순액입니다.",
        }
        if len(result.facts) == 1:
            fact = result.facts[0]
            amount = format_krw_with_approximation(fact.value)
            period_label = (
                f"{fact.period}년"
                if len(fact.period) == 4 and fact.period.isdigit()
                else format_period_label(fact.period)
            )
            return (
                f"{company_labels[fact.company.value]}의 {period_label} "
                f"{metric_labels[fact.metric.value]}은 {amount}입니다. "
                "쉽게 말해 '연결'은 회사와 자회사 실적을 합친 기준이고, "
                f"{metric_explanations[fact.metric.value]}"
            )
        if len(result.facts) == 2:
            left, right = result.facts
            relation = "두 회사가 같습니다"
            if left.value < right.value:
                relation = f"{company_labels[right.company.value]}가 더 큽니다"
            elif left.value > right.value:
                relation = f"{company_labels[left.company.value]}가 더 큽니다"
            metric_label = metric_labels[left.metric.value]
            difference = format_krw_with_approximation(abs(left.value - right.value))
            period_label = format_period_label(left.period)
            return (
                f"결론부터 말하면 {relation}. {period_label} 연결 누적 {metric_label}은 "
                f"{company_labels[left.company.value]} {format_krw_with_approximation(left.value)}, "
                f"{company_labels[right.company.value]} {format_krw_with_approximation(right.value)}입니다. "
                f"두 금액의 차이는 {difference}입니다. 같은 보고기간과 같은 연결 누적 기준으로 비교했습니다."
            )
        evidence_count = len(result.evidence)
        if result.verdict == Verdict.CONTRADICTED:
            return (
                "아니요. 입력한 내용은 공식 공시와 다릅니다. "
                f"서로 확인할 수 있는 공시 근거 {evidence_count}건을 아래에 표시했습니다."
            )
        if result.verdict in {
            Verdict.PARTIALLY_CONFIRMED,
            Verdict.INSUFFICIENT_INFO,
        } and evidence_count:
            topic = result.normalized_query.topic_or_claim or "질문"
            if "재무건전" in topic or "재무 건전" in topic:
                return (
                    f"관련 공식 공시 근거 {evidence_count}건을 확인했습니다. "
                    "다만 재무건전성은 한 문장이나 단일 지표로 단정할 수 없습니다. "
                    "유동성, 부채 부담, 현금창출력과 우발채무를 함께 확인해야 하므로 "
                    "아래에는 판단에 직접 도움이 되는 핵심 근거만 정리했습니다."
                )
            return (
                f"관련 공식 공시 근거 {evidence_count}건을 확인했습니다. "
                "다만 질문 범위가 넓어 하나의 결론으로 단정하지 않았습니다. "
                "아래 핵심 근거와 판단 조건을 함께 확인해 주세요."
            )
        if evidence_count == 0:
            return (
                "선택한 회사, 기간과 공시 범위에서 질문을 직접 뒷받침하거나 "
                "반박할 공식 근거를 찾지 못했습니다."
            )
        return (
            f"관련 공식 공시 근거 {evidence_count}건을 확인했습니다. "
            "아래에는 판단에 직접 연결되는 문서와 위치를 정리했습니다."
        )
