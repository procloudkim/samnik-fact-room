import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "./App";
import type { UiEvent } from "./types";

function makeStream(events: UiEvent[]): ReadableStream<Uint8Array> {
  const payload = events.map((event) => JSON.stringify(event)).join("\n") + "\n";
  const bytes = new TextEncoder().encode(payload);
  return new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(bytes.slice(0, Math.floor(bytes.length / 2)));
      controller.enqueue(bytes.slice(Math.floor(bytes.length / 2)));
      controller.close();
    },
  });
}

function envelope<T extends UiEvent["type"]>(
  type: T,
  seq: number,
  data: Extract<UiEvent, { type: T }>["data"],
): Extract<UiEvent, { type: T }> {
  return {
    schema_version: "1.0",
    event_id: `event-${seq}`,
    request_id: "request-server",
    conversation_id: "conversation-1",
    seq,
    emitted_at: "2026-07-19T00:00:00+09:00",
    type,
    data,
  } as Extract<UiEvent, { type: T }>;
}

describe("App", () => {
  it("POST JSONL 진행 이벤트 뒤에만 원자적 판정 카드를 표시한다", async () => {
    const user = userEvent.setup();
    const currentYear = new Date().getFullYear();
    const timelinePayload = {
      as_of: "2026-07-22T00:00:00+09:00",
      start_year: currentYear - 3,
      end_year: currentYear,
      latest_common_period: null,
      periods: [
        {
          business_year: currentYear,
          report_code: "11011",
          period: `${currentYear}`,
          label: "연간·4분기 포함",
          period_end: `${currentYear}-12-31`,
          expected_by: `${currentYear + 1}-03-01`,
          flow_value_basis: "ANNUAL",
          stock_value_basis: "POINT_IN_TIME",
          companies: [
            {
              company: "SAMSUNG_ELECTRONICS",
              cfs: { status: "LOADED", report_code: "11011", fs_div: "CFS" },
              ofs: { status: "LOADED", report_code: "11011", fs_div: "OFS" },
            },
            {
              company: "SK_HYNIX",
              cfs: { status: "LOADED", report_code: "11011", fs_div: "CFS" },
              ofs: { status: "LOADED", report_code: "11011", fs_div: "OFS" },
            },
          ],
        },
      ],
    };

    const events: UiEvent[] = [
      envelope("run.started", 0, {}),
      envelope("workflow.progress", 1, { stage: "SUPERVISOR_ROUTING" }),
      envelope("workflow.progress", 2, { stage: "SQL_RETRIEVAL" }),
      envelope("workflow.progress", 3, { stage: "EVIDENCE_VALIDATION" }),
      envelope("workflow.progress", 4, { stage: "SUPERVISOR_FINALIZING" }),
      envelope("answer.completed", 5, {
        result: {
          kind: "single_fact",
          verdict: "CONFIRMED",
          title: "2025년 삼성전자 영업이익",
          summary: "2025년 연결 영업이익은 43,601,051백만원입니다.",
          facts: [
            {
              company: "삼성전자",
              metric: "영업이익",
              value_display: "43,601,051백만원",
              period: "2025년",
              basis: "연결·연간 누적",
              evidence_ids: ["ev-1"],
            },
          ],
          evidence: [
            {
              evidence_id: "ev-1",
              report_name: "삼성전자 사업보고서 (2025.12)",
              rcept_no: "20260310002820",
              source_url: "https://dart.fss.or.kr/report",
              section: "연결 손익계산서",
            },
          ],
          interpretation: "연결·연간 누적 기준입니다.",
          as_of: "2026-07-19",
          trace_summary: {
            model_usage: {
              called: false,
              provider: "elice_mlapi",
              model: "openai/gpt-5-mini",
              input_tokens: 0,
              output_tokens: 0,
              cached_tokens: 0,
              total_tokens: 0,
              usage_status: "UNREPORTED",
            },
          },
        },
      }),
    ];
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/api/v1/financials/timeline") {
        return new Response(JSON.stringify(timelinePayload), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response(makeStream(events), {
        status: 200,
        headers: { "Content-Type": "application/jsonl" },
      });
    });

    render(<App />);
    await screen.findByRole("region", { name: /사업보고서.*적재 상태/ });
    await user.click(
      screen.getByRole("button", { name: /본업 수익성.*매출액과 영업이익/ }),
    );
    expect(screen.queryByText("43,601,051백만원")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "공시 근거 확인" }));

    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "2025년 삼성전자 영업이익" }),
      ).toBeVisible(),
    );
    expect(screen.getByText("43,601,051백만원")).toBeVisible();
    expect(screen.getByText("20260310002820")).toBeVisible();
    expect(screen.getByText(/외부 LLM 호출 0회/)).toBeVisible();
    const streamCall = fetchMock.mock.calls.find(
      ([request]) => String(request) === "/api/v1/chat/turns/stream",
    );
    expect(streamCall).toBeTruthy();
    const body = JSON.parse((streamCall?.[1] as RequestInit).body as string);
    expect(body.context).toMatchObject({
      companies: ["SAMSUNG_ELECTRONICS"],
      business_year: currentYear,
      report_code: "11011",
      fs_div: "CFS",
    });
    fetchMock.mockRestore();
  });

  it("모바일과 키보드에서 사용할 수 있는 이름 있는 입력·버튼을 제공한다", async () => {
    const currentYear = new Date().getFullYear();
    const timelinePayload = {
      as_of: "2026-07-22T00:00:00+09:00",
      start_year: currentYear - 3,
      end_year: currentYear,
      latest_common_period: null,
      periods: [
        {
          business_year: currentYear,
          report_code: "11011",
          period: `${currentYear}`,
          label: "연간·4분기 포함",
          period_end: `${currentYear}-12-31`,
          expected_by: `${currentYear + 1}-03-01`,
          flow_value_basis: "ANNUAL",
          stock_value_basis: "POINT_IN_TIME",
          companies: [
            {
              company: "SAMSUNG_ELECTRONICS",
              cfs: { status: "LOADED", report_code: "11011", fs_div: "CFS" },
              ofs: { status: "LOADED", report_code: "11011", fs_div: "OFS" },
            },
            {
              company: "SK_HYNIX",
              cfs: { status: "LOADED", report_code: "11011", fs_div: "CFS" },
              ofs: { status: "LOADED", report_code: "11011", fs_div: "OFS" },
            },
          ],
        },
      ],
    };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(timelinePayload), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    render(<App />);
    await screen.findByRole("region", { name: /사업보고서.*적재 상태/ });
    expect(screen.getByRole("textbox", { name: "공시 기반 질의" })).toBeVisible();
    expect(screen.getByRole("button", { name: "공시 근거 확인" })).toBeDisabled();
    expect(screen.getByRole("main")).toHaveAttribute("id", "main-content");
    expect(screen.getByText(/매수·매도 추천과 주가예측은 제공하지 않습니다/)).toBeVisible();
    expect(screen.getByRole("button", { name: /현금 전환력.*영업활동현금흐름/ })).toBeVisible();
    expect(screen.getByRole("button", { name: /재무건전성.*자산·부채·자본/ })).toBeVisible();
    expect(screen.getByText("심화 공시 인사이트")).toBeVisible();
    fetchMock.mockRestore();
  });

  it("미공시 반기보고서는 회사별 최근 10개년 평균 접수일을 안내한다", async () => {
    const user = userEvent.setup();
    const timelinePayload = {
      as_of: "2026-07-22T00:00:00+09:00",
      start_year: 2015,
      end_year: 2026,
      completed_years: 11,
      filing_estimate_window_years: 10,
      latest_common_period: "2026Q1",
      periods: [
        {
          business_year: 2026,
          report_code: "11012",
          period: "2026H1",
          label: "반기 누적",
          period_end: "2026-06-30",
          expected_by: "2026-08-14",
          flow_value_basis: "YTD",
          stock_value_basis: "POINT_IN_TIME",
          companies: [
            {
              company: "SAMSUNG_ELECTRONICS",
              filing_date_estimate: {
                estimated_date: "2026-08-15",
                sample_size: 10,
                history_start_year: 2016,
                history_end_year: 2025,
                average_lag_days: 45.7,
                methodology: "MEAN_FIRST_FILING_LAG",
              },
              cfs: { status: "NOT_YET_FILED", report_code: "11012", fs_div: "CFS" },
              ofs: { status: "NOT_YET_FILED", report_code: "11012", fs_div: "OFS" },
            },
            {
              company: "SK_HYNIX",
              filing_date_estimate: {
                estimated_date: "2026-08-15",
                sample_size: 10,
                history_start_year: 2016,
                history_end_year: 2025,
                average_lag_days: 45.7,
                methodology: "MEAN_FIRST_FILING_LAG",
              },
              cfs: { status: "NOT_YET_FILED", report_code: "11012", fs_div: "CFS" },
              ofs: { status: "NOT_YET_FILED", report_code: "11012", fs_div: "OFS" },
            },
          ],
        },
      ],
    };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(timelinePayload), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    render(<App />);
    await user.selectOptions(screen.getByLabelText("대상"), "COMPARE");

    const rail = await screen.findByRole("region", { name: /2026년 반기보고서/ });
    expect(within(rail).getByText("삼성전자")).toBeVisible();
    expect(within(rail).getByText("SK하이닉스")).toBeVisible();
    expect(within(rail).getAllByText(/최근 10개년 최초 접수 흐름상/)).toHaveLength(2);
    expect(within(rail).getAllByText(/8월 15일 전후/)).toHaveLength(2);
    expect(within(rail).getByText(/법정 제출기한은 8월 14일/)).toBeVisible();
    fetchMock.mockRestore();
  });
});
