import {
  EVENT_TYPES,
  PROGRESS_STAGES,
  type TurnRequest,
  type UiEvent,
} from "../types";

const TERMINAL_EVENTS = new Set<UiEvent["type"]>([
  "clarification.required",
  "answer.completed",
  "run.failed",
]);

const RESULT_KINDS = new Set([
  "single_fact",
  "comparison",
  "narrative_evidence",
  "no_evidence",
  "out_of_scope",
  "system_error",
]);

const PUBLIC_VERDICTS = new Set([
  "CONFIRMED",
  "CONTRADICTED",
  "REVIEW_REQUIRED",
  "INSUFFICIENT_EVIDENCE",
  "SYSTEM_UNAVAILABLE",
  "OUT_OF_SCOPE",
]);

export class StreamProtocolError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "StreamProtocolError";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function isUiEvent(value: unknown): value is UiEvent {
  if (!isRecord(value)) return false;

  const type = value.type;
  const data = value.data;
  const commonIsValid =
    value.schema_version === "1.0" &&
    typeof value.event_id === "string" &&
    typeof value.request_id === "string" &&
    typeof value.conversation_id === "string" &&
    Number.isInteger(value.seq) &&
    (value.seq as number) >= 0 &&
    typeof value.emitted_at === "string" &&
    typeof type === "string" &&
    EVENT_TYPES.includes(type as UiEvent["type"]) &&
    isRecord(data);

  if (!commonIsValid) return false;

  switch (type) {
    case "run.started":
      return data.message === undefined || typeof data.message === "string";
    case "workflow.progress":
      return (
        typeof data.stage === "string" &&
        PROGRESS_STAGES.includes(data.stage as (typeof PROGRESS_STAGES)[number]) &&
        (data.label === undefined || typeof data.label === "string") &&
        (data.status === undefined ||
          [
            "routing",
            "started",
            "completed",
            "passed",
            "failed",
            "checked",
            "updated",
            "finalizing",
          ].includes(data.status as string)) &&
        (data.agent === undefined || typeof data.agent === "string") &&
        (data.tool === undefined || typeof data.tool === "string") &&
        (data.summary === undefined || data.summary === null || isRecord(data.summary))
      );
    case "clarification.required":
      return (
        typeof data.question === "string" &&
        isRecord(data.collected_slots) &&
        Object.values(data.collected_slots).every((slot) => typeof slot === "string") &&
        Array.isArray(data.missing_slots) &&
        data.missing_slots.every((slot) => typeof slot === "string")
      );
    case "answer.completed":
      return (
        isRecord(data.result) &&
        typeof data.result.kind === "string" &&
        RESULT_KINDS.has(data.result.kind) &&
        typeof data.result.verdict === "string" &&
        PUBLIC_VERDICTS.has(data.result.verdict) &&
        (data.result.status === undefined ||
          (typeof data.result.status === "string" &&
            PUBLIC_VERDICTS.has(data.result.status))) &&
        (data.result.workflow_status === undefined ||
          typeof data.result.workflow_status === "string") &&
        (data.result.failure_reason === undefined ||
          data.result.failure_reason === null ||
          typeof data.result.failure_reason === "string") &&
        typeof data.result.title === "string" &&
        typeof data.result.summary === "string" &&
        Array.isArray(data.result.facts) &&
        Array.isArray(data.result.evidence) &&
        typeof data.result.interpretation === "string" &&
        typeof data.result.as_of === "string"
      );
    case "run.failed":
      return (
        typeof data.code === "string" &&
        typeof data.message === "string" &&
        typeof data.retryable === "boolean"
      );
    default:
      return false;
  }
}

function parseLine(line: string): UiEvent {
  let parsed: unknown;
  try {
    parsed = JSON.parse(line);
  } catch {
    throw new StreamProtocolError("스트림에서 올바르지 않은 JSON을 받았습니다.");
  }

  if (!isUiEvent(parsed)) {
    throw new StreamProtocolError("지원하지 않는 스트림 이벤트를 받았습니다.");
  }
  return parsed;
}

export async function parseJsonlStream(
  stream: ReadableStream<Uint8Array>,
  onEvent: (event: UiEvent) => void,
): Promise<UiEvent | null> {
  const reader = stream.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  let lastEvent: UiEvent | null = null;

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split(/\r?\n/);
      buffer = lines.pop() ?? "";

      for (const candidate of lines) {
        const line = candidate.trim();
        if (!line) continue;
        lastEvent = parseLine(line);
        onEvent(lastEvent);
      }
    }

    buffer += decoder.decode();
    const tail = buffer.trim();
    if (tail) {
      lastEvent = parseLine(tail);
      onEvent(lastEvent);
    }
  } finally {
    reader.releaseLock();
  }

  return lastEvent;
}

async function readErrorMessage(response: Response): Promise<string> {
  try {
    const payload = (await response.json()) as { detail?: unknown; message?: unknown };
    if (typeof payload.message === "string") return payload.message;
    if (typeof payload.detail === "string") return payload.detail;
  } catch {
    // The public fallback below intentionally hides arbitrary upstream bodies.
  }
  return `요청을 처리하지 못했습니다. (HTTP ${response.status})`;
}

export async function streamChatTurn(
  request: TurnRequest,
  onEvent: (event: UiEvent) => void,
  options: {
    signal?: AbortSignal;
    fetchImpl?: typeof fetch;
  } = {},
): Promise<UiEvent> {
  const fetchImpl = options.fetchImpl ?? fetch;
  const response = await fetchImpl("/api/v1/chat/turns/stream", {
    method: "POST",
    headers: {
      Accept: "application/jsonl",
      "Content-Type": "application/json",
    },
    body: JSON.stringify(request),
    signal: options.signal,
  });

  if (!response.ok) {
    throw new Error(await readErrorMessage(response));
  }
  if (!response.body) {
    throw new StreamProtocolError("이 브라우저에서는 스트림 응답을 읽을 수 없습니다.");
  }

  const lastEvent = await parseJsonlStream(response.body, onEvent);
  if (!lastEvent || !TERMINAL_EVENTS.has(lastEvent.type)) {
    throw new StreamProtocolError("응답이 완료되기 전에 연결이 종료되었습니다.");
  }
  return lastEvent;
}
