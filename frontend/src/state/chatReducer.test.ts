import { chatReducer, initialChatState } from "./chatReducer";
import type { FactCheckResult, UiEvent } from "../types";

function event<T extends UiEvent["type"]>(
  type: T,
  seq: number,
  data: Extract<UiEvent, { type: T }>["data"],
  eventId = `event-${seq}`,
): Extract<UiEvent, { type: T }> {
  return {
    schema_version: "1.0",
    event_id: eventId,
    request_id: "request-server",
    conversation_id: "conversation-1",
    seq,
    emitted_at: "2026-07-19T00:00:00+09:00",
    type,
    data,
  } as Extract<UiEvent, { type: T }>;
}

const result: FactCheckResult = {
  kind: "single_fact",
  verdict: "CONFIRMED",
  title: "2025년 삼성전자 영업이익",
  summary: "공시 기준으로 확인되었습니다.",
  facts: [],
  evidence: [],
  interpretation: "연결·연간 누적 기준입니다.",
  as_of: "2026-07-19",
};

function submittedState() {
  return chatReducer(initialChatState, {
    type: "turn.submitted",
    message: "질문",
    clientRequestId: "request-client",
  });
}

describe("chatReducer", () => {
  it("중복 event_id와 역순 seq 이벤트를 무시한다", () => {
    let state = submittedState();
    state = chatReducer(state, {
      type: "event.received",
      event: event("run.started", 0, {}),
    });
    state = chatReducer(state, {
      type: "event.received",
      event: event("workflow.progress", 1, { stage: "SUPERVISOR_ROUTING" }),
    });

    const duplicate = chatReducer(state, {
      type: "event.received",
      event: event(
        "workflow.progress",
        2,
        { stage: "SQL_RETRIEVAL" },
        "event-1",
      ),
    });
    const outOfOrder = chatReducer(duplicate, {
      type: "event.received",
      event: event("workflow.progress", 0, { stage: "VECTOR_RETRIEVAL" }, "late"),
    });

    expect(outOfOrder.progress.map((item) => item.stage)).toEqual([
      "SUPERVISOR_ROUTING",
    ]);
    expect(outOfOrder.lastSeq).toBe(1);
  });

  it("보완 질문에서 같은 conversation을 유지하고 슬롯을 저장한다", () => {
    let state = submittedState();
    state = chatReducer(state, {
      type: "event.received",
      event: event("run.started", 0, {}),
    });
    state = chatReducer(state, {
      type: "event.received",
      event: event("clarification.required", 1, {
        question: "2025년 연간 기준으로 비교할까요?",
        collected_slots: { company: "삼성전자", metric: "영업이익" },
        missing_slots: ["period"],
      }),
    });

    expect(state.phase).toBe("awaiting_user");
    expect(state.conversationId).toBe("conversation-1");
    expect(state.collectedSlots).toEqual({ company: "삼성전자", metric: "영업이익" });
    expect(state.missingSlots).toEqual(["period"]);
    expect(state.messages.at(-1)?.content).toContain("2025년 연간");
  });

  it("answer.completed 전에는 결과를 만들지 않고 terminal에서 원자적으로 교체한다", () => {
    let state = submittedState();
    state = chatReducer(state, {
      type: "event.received",
      event: event("run.started", 0, {}),
    });
    state = chatReducer(state, {
      type: "event.received",
      event: event("workflow.progress", 1, { stage: "SQL_RETRIEVAL" }),
    });
    expect(state.result).toBeNull();

    state = chatReducer(state, {
      type: "event.received",
      event: event("answer.completed", 2, { result }),
    });

    expect(state.phase).toBe("complete");
    expect(state.result).toEqual(result);
  });

  it("사용자 중단은 부분 결과 없이 cancelled 상태가 된다", () => {
    const state = chatReducer(submittedState(), { type: "run.cancelled" });
    expect(state.phase).toBe("cancelled");
    expect(state.result).toBeNull();
  });

  it("백엔드 label 대신 공개 allowlist 문구만 표시한다", () => {
    let state = submittedState();
    state = chatReducer(state, {
      type: "event.received",
      event: event("run.started", 0, {}),
    });
    state = chatReducer(state, {
      type: "event.received",
      event: event("workflow.progress", 1, {
        stage: "SUPERVISOR_ROUTING",
        label: "내부 모델 추론 텍스트",
      }),
    });

    expect(state.progress[0]?.label).toBe(
      "Supervisor가 질문 의도와 조건을 확인합니다",
    );
    expect(state.progress[0]?.label).not.toContain("내부 모델");
  });
});
