import { parseJsonlStream, StreamProtocolError } from "./stream";
import type { UiEvent } from "../types";

function runStarted(overrides: Partial<UiEvent> = {}): UiEvent {
  return {
    schema_version: "1.0",
    event_id: "event-1",
    request_id: "request-1",
    conversation_id: "conversation-1",
    seq: 0,
    emitted_at: "2026-07-19T00:00:00+09:00",
    type: "run.started",
    data: { message: "삼성전자 공시 확인" },
    ...overrides,
  } as UiEvent;
}

function byteIndexOf(haystack: Uint8Array, needle: Uint8Array): number {
  outer: for (let index = 0; index <= haystack.length - needle.length; index += 1) {
    for (let offset = 0; offset < needle.length; offset += 1) {
      if (haystack[index + offset] !== needle[offset]) continue outer;
    }
    return index;
  }
  return -1;
}

describe("parseJsonlStream", () => {
  it("한글 UTF-8 문자가 byte chunk 사이에서 잘려도 이벤트를 복원한다", async () => {
    const event = runStarted();
    const encoder = new TextEncoder();
    const bytes = encoder.encode(`${JSON.stringify(event)}\n`);
    const hangul = encoder.encode("삼");
    const hangulIndex = byteIndexOf(bytes, hangul);
    expect(hangulIndex).toBeGreaterThan(0);

    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(bytes.slice(0, hangulIndex + 1));
        controller.enqueue(bytes.slice(hangulIndex + 1, hangulIndex + 2));
        controller.enqueue(bytes.slice(hangulIndex + 2));
        controller.close();
      },
    });
    const received: UiEvent[] = [];

    const last = await parseJsonlStream(stream, (item) => received.push(item));

    expect(received).toHaveLength(1);
    expect(received[0]).toEqual(event);
    expect(last).toEqual(event);
  });

  it("여러 줄과 마지막 개행 없는 이벤트를 순서대로 읽는다", async () => {
    const first = runStarted();
    const second: UiEvent = {
      ...first,
      event_id: "event-2",
      seq: 1,
      type: "workflow.progress",
      data: { stage: "SUPERVISOR_ROUTING" },
    };
    const bytes = new TextEncoder().encode(
      `${JSON.stringify(first)}\n${JSON.stringify(second)}`,
    );
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(bytes);
        controller.close();
      },
    });
    const received: UiEvent[] = [];

    await parseJsonlStream(stream, (item) => received.push(item));

    expect(received.map((item) => item.event_id)).toEqual(["event-1", "event-2"]);
  });

  it("allowlist 밖의 진행 단계는 거부한다", async () => {
    const unsafe = {
      ...runStarted(),
      type: "workflow.progress",
      data: { stage: "MODEL_CHAIN_OF_THOUGHT", label: "raw thought" },
    };
    const bytes = new TextEncoder().encode(`${JSON.stringify(unsafe)}\n`);
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(bytes);
        controller.close();
      },
    });

    await expect(parseJsonlStream(stream, () => undefined)).rejects.toBeInstanceOf(
      StreamProtocolError,
    );
  });

  it("동결된 공개 verdict 밖의 내부 상태값은 거부한다", async () => {
    const unsafe = {
      ...runStarted(),
      type: "answer.completed",
      data: {
        result: {
          kind: "no_evidence",
          verdict: "NO_OFFICIAL_EVIDENCE",
          title: "내부 상태",
          summary: "외부로 나가면 안 됩니다.",
          facts: [],
          evidence: [],
          interpretation: "",
          as_of: "2026-07-19",
        },
      },
    };
    const bytes = new TextEncoder().encode(`${JSON.stringify(unsafe)}\n`);
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(bytes);
        controller.close();
      },
    });

    await expect(parseJsonlStream(stream, () => undefined)).rejects.toBeInstanceOf(
      StreamProtocolError,
    );
  });

  it("판정 상태와 실패 사유가 추가된 공개 결과를 허용한다", async () => {
    const completed = {
      ...runStarted(),
      type: "answer.completed",
      data: {
        result: {
          kind: "no_evidence",
          status: "INSUFFICIENT_EVIDENCE",
          verdict: "INSUFFICIENT_EVIDENCE",
          workflow_status: "UNAVAILABLE",
          failure_reason: "NO_OFFICIAL_EVIDENCE",
          title: "근거 부족",
          summary: "공시 근거를 찾지 못했습니다.",
          facts: [],
          evidence: [
            {
              report_name: "삼성전자 사업보고서",
              rcept_no: "20260310002820",
              source_url: "https://dart.fss.or.kr/example",
              section: "연결재무제표",
            },
          ],
          interpretation: "거짓 판정이 아닙니다.",
          as_of: "2026-07-19",
        },
      },
    };
    const bytes = new TextEncoder().encode(`${JSON.stringify(completed)}\n`);
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(bytes);
        controller.close();
      },
    });

    const last = await parseJsonlStream(stream, () => undefined);

    expect(last?.type).toBe("answer.completed");
  });
});
