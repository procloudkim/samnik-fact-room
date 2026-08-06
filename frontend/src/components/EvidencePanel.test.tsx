import { render, screen } from "@testing-library/react";
import { ResultCard } from "./EvidencePanel";
import type { FactCheckResult } from "../types";

const noop = () => undefined;

const comparison: FactCheckResult = {
  kind: "comparison",
  verdict: "CONFIRMED",
  status: "CONFIRMED",
  workflow_status: "ANSWER_READY",
  title: "2025년 연간 영업이익 비교",
  summary: "SK하이닉스의 영업이익이 삼성전자보다 3.61조원 많습니다.",
  facts: [
    {
      company: "삼성전자",
      metric: "영업이익",
      value_display: "43,601,051백만원",
      raw_value: "43601051000000",
      raw_value_display: "43,601,051,000,000원",
      value_trillion_krw: "43.601051",
      value_trillion_krw_display: "약 43.60조 원",
      period: "2025년",
      period_label: "2025 사업연도",
      period_type: "ANNUAL",
      period_type_label: "연간",
      fs_div: "CFS",
      fs_div_label: "연결",
      value_scope: "CUMULATIVE",
      value_scope_label: "누적",
      basis: "연결·연간 누적",
      evidence_ids: ["samsung"],
    },
    {
      company: "SK하이닉스",
      metric: "영업이익",
      value_display: "47,206,319백만원",
      period: "2025년",
      basis: "연결·연간 누적",
      evidence_ids: ["hynix"],
    },
  ],
  evidence: [
    {
      evidence_id: "samsung",
      report_name: "삼성전자 사업보고서 (2025.12)",
      rcept_no: "20260310002820",
      source_url: "https://dart.fss.or.kr/samsung",
      section: "연결 손익계산서",
    },
    {
      evidence_id: "hynix",
      report_name: "SK하이닉스 사업보고서 (2025.12)",
      rcept_no: "20260317000635",
      source_url: "https://dart.fss.or.kr/hynix",
      section: "연결 포괄손익계산서",
    },
  ],
  interpretation: "동일 기간·통화·연결 기준으로 비교했습니다.",
  interpretation_notes: [
    "2025 사업연도 연결 누적 수치이며, 별도재무제표 또는 분기 단독 수치와 비교할 때 기준을 맞춰야 합니다.",
  ],
  trace_summary: {
    agent_calls: ["RAG_FAQ"],
    final_actor: "SUPERVISOR",
    state_transitions: [
      { sequence: 1, actor: "SYSTEM", event: "turn.started" },
      {
        sequence: 2,
        actor: "SUPERVISOR",
        event: "supervisor.route.selected",
      },
    ],
  },
  as_of: "2026-07-19",
};

function renderResult(result: FactCheckResult) {
  render(<ResultCard result={result} onRetry={noop} onUsePrompt={noop} />);
}

describe("ResultCard", () => {
  it("정확한 비교표와 수치별 inline citation, 공시 상세를 함께 표시한다", () => {
    renderResult(comparison);

    expect(screen.getByRole("heading", { name: "2025년 연간 영업이익 비교" })).toBeVisible();
    expect(screen.getByRole("table")).toBeVisible();
    expect(screen.getByText("약 43.60조 원")).toBeVisible();
    expect(screen.getByText(/원 단위 43,601,051,000,000원/)).toBeVisible();
    expect(screen.getByText("연간·연결·누적")).toBeVisible();
    expect(screen.getByText("2026년 7월 19일 기준")).toBeVisible();
    expect(screen.getByRole("heading", { name: "해석상 유의점" })).toBeVisible();
    expect(screen.getByRole("list", { name: "상태 전이" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "근거 1로 이동" })).toHaveAttribute(
      "href",
      "#source-1",
    );
    expect(screen.getByText("20260317000635")).toBeVisible();
    expect(screen.getAllByRole("link", { name: /원문 열기/ })).toHaveLength(2);
  });

  it("공시 근거 없음은 거짓 판정과 구분해 검색 범위를 표시한다", () => {
    renderResult({
      ...comparison,
      kind: "no_evidence",
      verdict: "INSUFFICIENT_EVIDENCE",
      status: "INSUFFICIENT_EVIDENCE",
      workflow_status: "UNAVAILABLE",
      failure_reason: "NO_OFFICIAL_EVIDENCE",
      title: "공시에서 직접 확인되지 않음",
      summary: "조회 범위에서 해당 고객사 승인 내용을 찾지 못했습니다.",
      facts: [],
      evidence: [],
      searched_scope: "2026-07-19까지 적재된 SK하이닉스 정기공시",
    });

    expect(screen.getByText("근거 미확인은 거짓 판정이 아닙니다")).toBeVisible();
    expect(screen.getByText(/SK하이닉스 정기공시/)).toBeVisible();
    expect(screen.queryByText(/NO_OFFICIAL_EVIDENCE/)).not.toBeInTheDocument();
  });

  it("확인된 근거와 종합 판단 필요 상태를 자료 없음으로 표현하지 않는다", () => {
    renderResult({
      ...comparison,
      kind: "narrative_evidence",
      verdict: "REVIEW_REQUIRED",
      status: "REVIEW_REQUIRED",
      title: "관련 공시는 확인됐으며 종합 판단이 필요합니다",
      summary:
        "관련 공식 공시 근거 8건을 확인했습니다. 재무건전성은 여러 지표를 함께 봐야 합니다.",
      facts: [],
      evidence: comparison.evidence.map((item) => ({
        ...item,
        excerpt: "유동성, 부채 부담과 현금창출력을 함께 검토해야 합니다.",
        excerpt_truncated: true,
      })),
      evidence_total_count: 8,
      analysis_report: {
        lens_key: "FINANCIAL_RESILIENCE",
        lens_label: "재무건전성",
        methodology: "CONTEXTUAL_DISCLOSURE_ANALYSIS_V2",
        source_count: 8,
        limitation: "직접 공시 근거와 다음 확인 항목을 함께 정리했습니다.",
        findings: [
          {
            category_key: "LIQUIDITY",
            category_label: "유동성·자금조달",
            summary: "유동성 공급원과 자금수지 예측 근거를 확인했습니다.",
            plain_language: "가까운 시일 안에 갚을 돈을 마련할 수 있는지 보는 항목입니다.",
            next_check: "현금성자산과 1년 내 차입금 만기를 비교하세요.",
            evidence_ids: ["samsung"],
          },
          {
            category_key: "CREDIT",
            category_label: "부채·신용위험",
            summary: "부채와 신용위험 관리 근거를 확인했습니다.",
            evidence_ids: ["hynix"],
          },
        ],
      },
      interpretation:
        "공시 근거는 확인됐으며, 결론에 필요한 추가 확인 항목을 함께 표시했습니다.",
    });

    expect(screen.getByText("근거 확인 · 종합 판단 필요")).toBeVisible();
    expect(
      screen.getByRole("heading", {
        name: "관련 공시는 확인됐으며 종합 판단이 필요합니다",
      }),
    ).toBeVisible();
    expect(screen.getByText("2건 표시 · 총 8건 확인")).toBeVisible();
    expect(screen.getByRole("heading", { name: "공시가 말해 주는 것" })).toBeVisible();
    expect(screen.getByText("쉽게 말하면")).toBeVisible();
    expect(screen.getByText("다음에 볼 것")).toBeVisible();
    expect(screen.getByText("유동성·자금조달")).toBeVisible();
    expect(screen.getByText("부채·신용위험")).toBeVisible();
    expect(screen.getByText("2건 표시 · 총 8건 확인").closest("details")).not.toHaveAttribute(
      "open",
    );
    expect(screen.getAllByText("긴 원문 중 핵심 구간만 표시했습니다.")).toHaveLength(2);
    expect(screen.queryByText("근거 미확인은 거짓 판정이 아닙니다")).not.toBeInTheDocument();
    expect(screen.queryByText(/확인할 자료 부족/)).not.toBeInTheDocument();
  });

  it("범위 밖과 시스템 오류를 서로 다른 상태 카드로 렌더링한다", () => {
    const { unmount } = render(
      <ResultCard
        result={{
          ...comparison,
          kind: "out_of_scope",
          verdict: "OUT_OF_SCOPE",
          title: "투자 판단은 제공하지 않습니다",
          summary: "공시 사실 확인 질문으로 바꿔 주세요.",
          facts: [],
          evidence: [],
          suggested_questions: ["2025년 삼성전자 영업이익은?"],
        }}
        onRetry={noop}
        onUsePrompt={noop}
      />,
    );
    expect(screen.getByText("이 질문은 답하기 어렵습니다")).toBeVisible();
    expect(screen.getByRole("button", { name: "2025년 삼성전자 영업이익은?" })).toBeVisible();
    unmount();

    renderResult({
      ...comparison,
      kind: "system_error",
      verdict: "SYSTEM_UNAVAILABLE",
      status: "SYSTEM_UNAVAILABLE",
      workflow_status: "SYSTEM_ERROR",
      failure_reason: "REPOSITORY_UNAVAILABLE",
      title: "공시 저장소에 연결하지 못했습니다",
      summary: "잠시 후 다시 확인해 주세요.",
      facts: [],
      evidence: [],
      code: "DATABASE_UNAVAILABLE",
      retryable: true,
    });
    expect(screen.getByText("잠시 문제가 생겼습니다")).toBeVisible();
    expect(screen.queryByText("DATABASE_UNAVAILABLE")).not.toBeInTheDocument();
    expect(screen.queryByText(/REPOSITORY_UNAVAILABLE/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "같은 질문 다시 확인" })).toBeVisible();
  });
});
