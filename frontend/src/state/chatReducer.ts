import {
  PROGRESS_LABELS,
  type FactCheckResult,
  type ProgressStage,
  type SlotValues,
  type UiEvent,
  type ProgressSummary,
} from "../types";

export type ChatPhase =
  | "idle"
  | "streaming"
  | "awaiting_user"
  | "complete"
  | "error"
  | "cancelled";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
}

export interface ProgressEntry {
  stage: ProgressStage;
  label: string;
  status: "routing" | "started" | "completed" | "passed" | "failed" | "checked" | "updated" | "finalizing";
  agent: string;
  tool: string;
  summary?: ProgressSummary;
}

export interface PublicRunError {
  code: string;
  message: string;
  retryable: boolean;
}

export interface ChatState {
  phase: ChatPhase;
  draft: string;
  conversationId: string | null;
  requestId: string | null;
  activeClientRequestId: string | null;
  lastSeq: number;
  seenEventIds: ReadonlySet<string>;
  messages: ChatMessage[];
  progress: ProgressEntry[];
  collectedSlots: SlotValues;
  missingSlots: string[];
  slotOverrides: SlotValues;
  result: FactCheckResult | null;
  error: PublicRunError | null;
  lastSubmittedMessage: string | null;
}

export const initialChatState: ChatState = {
  phase: "idle",
  draft: "",
  conversationId: null,
  requestId: null,
  activeClientRequestId: null,
  lastSeq: -1,
  seenEventIds: new Set(),
  messages: [],
  progress: [],
  collectedSlots: {},
  missingSlots: [],
  slotOverrides: {},
  result: null,
  error: null,
  lastSubmittedMessage: null,
};

export type ChatAction =
  | { type: "draft.changed"; value: string }
  | { type: "prompt.selected"; value: string }
  | { type: "slot.override"; slot: string; value: string }
  | { type: "turn.submitted"; message: string; clientRequestId: string }
  | { type: "turn.restarted"; clientRequestId: string }
  | { type: "event.received"; event: UiEvent }
  | { type: "stream.failed"; message: string }
  | { type: "run.cancelled" }
  | { type: "conversation.reset" };

function messageId(prefix: string, seed: string): string {
  return `${prefix}-${seed}`;
}

function markProgressComplete(progress: ProgressEntry[]): ProgressEntry[] {
  return progress;
}

function applyEvent(state: ChatState, event: UiEvent): ChatState {
  if (state.seenEventIds.has(event.event_id) || event.seq <= state.lastSeq) {
    return state;
  }
  if (state.requestId && event.request_id !== state.requestId) {
    return state;
  }

  const seenEventIds = new Set(state.seenEventIds);
  seenEventIds.add(event.event_id);
  const common = {
    ...state,
    seenEventIds,
    lastSeq: event.seq,
    requestId: state.requestId ?? event.request_id,
    conversationId: event.conversation_id,
  };

  switch (event.type) {
    case "run.started":
      return { ...common, phase: "streaming" };
    case "workflow.progress": {
      if (state.progress.some((entry) => entry.stage === event.data.stage)) {
        return common;
      }
      return {
        ...common,
        progress: [
          ...state.progress,
          {
            stage: event.data.stage,
            // Public labels are fixed here. Never render provider or model text as process trace.
            label: PROGRESS_LABELS[event.data.stage],
            status: event.data.status ?? "updated",
            agent: event.data.agent ?? "unknown",
            tool: event.data.tool ?? "unknown",
            summary: event.data.summary ?? null,
          },
        ],
      };
    }
    case "clarification.required":
      return {
        ...common,
        phase: "awaiting_user",
        progress: markProgressComplete(state.progress),
        collectedSlots: event.data.collected_slots,
        missingSlots: event.data.missing_slots,
        messages: [
          ...state.messages,
          {
            id: messageId("assistant", event.event_id),
            role: "assistant",
            content: event.data.question,
          },
        ],
      };
    case "answer.completed":
      return {
        ...common,
        phase: "complete",
        progress: markProgressComplete(state.progress),
        result: event.data.result,
        error: null,
        messages: [
          ...state.messages,
          {
            id: messageId("assistant", event.event_id),
            role: "assistant",
            content: event.data.result.summary,
          },
        ],
      };
    case "run.failed":
      return {
        ...common,
        phase: "error",
        error: event.data,
      };
  }
}

export function chatReducer(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    case "draft.changed":
    case "prompt.selected":
      return { ...state, draft: action.value };
    case "slot.override":
      return {
        ...state,
        slotOverrides: {
          ...state.slotOverrides,
          [action.slot]: action.value,
        },
      };
    case "turn.submitted":
      return {
        ...state,
        phase: "streaming",
        draft: "",
        activeClientRequestId: action.clientRequestId,
        requestId: null,
        lastSeq: -1,
        seenEventIds: new Set(),
        progress: [],
        result: null,
        error: null,
        slotOverrides: {},
        lastSubmittedMessage: action.message,
        messages: [
          ...state.messages,
          {
            id: messageId("user", action.clientRequestId),
            role: "user",
            content: action.message,
          },
        ],
      };
    case "turn.restarted":
      return {
        ...state,
        phase: "streaming",
        activeClientRequestId: action.clientRequestId,
        requestId: null,
        lastSeq: -1,
        seenEventIds: new Set(),
        progress: [],
        result: null,
        error: null,
      };
    case "event.received":
      return applyEvent(state, action.event);
    case "stream.failed":
      return {
        ...state,
        phase: "error",
        error: {
          code: "STREAM_INTERRUPTED",
          message: action.message,
          retryable: true,
        },
      };
    case "run.cancelled":
      return {
        ...state,
        phase: "cancelled",
        error: null,
      };
    case "conversation.reset":
      return initialChatState;
  }
}

export function buildMessageWithOverrides(
  message: string,
  overrides: SlotValues,
): string {
  const entries = Object.entries(overrides).filter(([, value]) => value.trim());
  if (entries.length === 0) return message.trim();
  const context = entries.map(([slot, value]) => `- ${slot}: ${value.trim()}`).join("\n");
  return `${message.trim()}\n\n[사용자 지정 조건]\n${context}`;
}
