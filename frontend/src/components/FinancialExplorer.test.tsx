import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { FinancialExplorer } from "./FinancialExplorer";

const QUERY_MODES = ["FACT", "TIME_SERIES", "COMPARE", "YOY", "CFS_OFS_DIFF", "STATEMENT"] as const;

describe("FinancialExplorer", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("supports all 6 financial query modes", async () => {
    const user = userEvent.setup();
    const years = [2023, 2024, 2025, 2026];
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ facts: [], warnings: [] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    render(
      <FinancialExplorer
        defaultCompany="SAMSUNG_ELECTRONICS"
        defaultYear={2025}
        defaultReportCode="11011"
        availableYears={years}
      />,
    );

    const modeSelect = screen.getByLabelText("조회 방식");
    const optionValues = within(modeSelect).getAllByRole("option");
    const renderedModes = optionValues.map((option) => option.getAttribute("value"));
    expect(renderedModes).toEqual(QUERY_MODES as readonly (string | null)[]);

    for (const mode of QUERY_MODES) {
      await user.selectOptions(modeSelect, mode);
      await user.click(screen.getByRole("button", { name: "재무제표 조회" }));

      const queryCall = [...fetchMock.mock.calls]
        .reverse()
        .find(([request]) => String(request).includes("/api/v1/financials/query"));
      const request = queryCall?.[1];
      expect(request).toBeTruthy();
      const requestBody = typeof request?.body === "string" ? request.body : "{}";
      const payload = JSON.parse(requestBody);
      expect(payload.mode).toBe(mode);
    }
  });

  it("STATEMENT 모드에서도 0원 행을 그대로 표시한다", async () => {
    const user = userEvent.setup();
    const years = [2024, 2025, 2026];
    const fetchMock = vi.spyOn(globalThis, "fetch");
    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          facts: [
            {
              company: "SAMSUNG_ELECTRONICS",
              company_label: "삼성전자",
              period: "2025",
              fs_div: "CFS",
              account_id: "A1",
              account_nm: "현금및현금성자산",
              statement: "BS",
              raw_amount: "0",
              value_display: "0원",
              value_basis: "ANNUAL",
              rcept_no: "2025001",
              source_url: "https://dart.fss.or.kr/report",
              metric_key: null,
            },
            {
              company: "SAMSUNG_ELECTRONICS",
              company_label: "삼성전자",
              period: "2025",
              fs_div: "CFS",
              account_id: "A2",
              account_nm: "매출액",
              statement: "IS",
              raw_amount: "100",
              value_display: "100원",
              value_basis: "ANNUAL",
              rcept_no: "2025002",
              source_url: "https://dart.fss.or.kr/report2",
              metric_key: null,
            },
          ],
          warnings: [],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ accounts: [] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    render(
      <FinancialExplorer
        defaultCompany="SAMSUNG_ELECTRONICS"
        defaultYear={2025}
        defaultReportCode="11011"
        availableYears={years}
      />,
    );

    await user.selectOptions(screen.getByLabelText("조회 방식"), "STATEMENT");
    await user.click(screen.getByRole("button", { name: "재무제표 조회" }));

    expect(screen.getByText("100원", { selector: "td.numeric-cell strong" })).toBeInTheDocument();
    expect(screen.getAllByText("0원")).toHaveLength(2);
    expect(screen.queryByText(/기본은 0값을 감추어/)).not.toBeInTheDocument();
  });

  it("전체 재무제표는 핵심 안내 뒤 원문을 24개씩 단계적으로 보여준다", async () => {
    const user = userEvent.setup();
    const facts = Array.from({ length: 30 }, (_, index) => ({
      company: "SK_HYNIX",
      company_label: "SK하이닉스",
      period: "2023Q1",
      fs_div: "CFS",
      account_id: `A${index}`,
      account_nm: index === 0 ? "영업이익" : `상세계정 ${index}`,
      statement: "IS",
      raw_amount: String(index),
      value_display: `${index}원`,
      value_basis: "YTD",
      rcept_no: `2023${String(index).padStart(10, "0")}`,
      source_url: "https://dart.fss.or.kr/report",
      metric_key: index === 0 ? "operating_profit" : null,
    }));
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ facts, warnings: [] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    render(
      <FinancialExplorer
        defaultCompany="SK_HYNIX"
        defaultYear={2023}
        defaultReportCode="11013"
        availableYears={[2023]}
      />,
    );

    await user.selectOptions(screen.getByLabelText("조회 방식"), "STATEMENT");
    await user.click(screen.getByRole("button", { name: "재무제표 조회" }));

    expect(screen.getByRole("heading", { name: "핵심 항목부터 확인하세요" })).toBeInTheDocument();
    expect(
      screen.getByText((_, element) =>
        element?.textContent === "전체 30개 계정 중 자주 확인하는 항목을 먼저 정리했습니다.",
      ),
    ).toBeInTheDocument();

    await user.click(screen.getByText("전체 계정 원문"));
    expect(screen.getAllByRole("row")).toHaveLength(25);
    await user.click(screen.getByRole("button", { name: "다음 6개 더 보기" }));
    expect(screen.getAllByRole("row")).toHaveLength(31);
  });

  it("핵심 요약은 자본변동표의 0원 하위행보다 손익계산서 대표행을 우선한다", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          facts: [
            {
              company: "SAMSUNG_ELECTRONICS",
              company_label: "삼성전자",
              period: "2025",
              fs_div: "CFS",
              account_id: "ifrs-full_ProfitLossFromOperatingActivities",
              account_nm: "영업이익",
              account_detail: "자본|기타자본항목",
              statement: "SCE",
              raw_amount: "0",
              value_display: "0원",
              value_basis: "ANNUAL",
              rcept_no: "20260000000001",
              source_url: "https://dart.fss.or.kr/report",
              metric_key: "operating_profit",
            },
            {
              company: "SAMSUNG_ELECTRONICS",
              company_label: "삼성전자",
              period: "2025",
              fs_div: "CFS",
              account_id: "ifrs-full_ProfitLossFromOperatingActivities",
              account_nm: "영업이익",
              account_detail: "-",
              statement: "IS",
              raw_amount: "1000000000000",
              value_display: "1조 원",
              value_basis: "ANNUAL",
              rcept_no: "20260000000001",
              source_url: "https://dart.fss.or.kr/report",
              metric_key: "operating_profit",
            },
          ],
          warnings: [],
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );

    render(
      <FinancialExplorer
        defaultCompany="SAMSUNG_ELECTRONICS"
        defaultYear={2025}
        defaultReportCode="11011"
        availableYears={[2025]}
      />,
    );
    await user.selectOptions(screen.getByLabelText("조회 방식"), "STATEMENT");
    await user.click(screen.getByRole("button", { name: "재무제표 조회" }));

    const summary = screen.getByRole("heading", { name: "핵심 항목부터 확인하세요" })
      .closest("section");
    expect(summary).not.toBeNull();
    expect(within(summary as HTMLElement).getByText("1조 원")).toBeVisible();
  });

  it("CORPUS_INCOMPLETE 코드를 공시 일정에 맞는 사용자 안내로 바꾼다", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          facts: [],
          warnings: ["INSUFFICIENT_EVIDENCE", "CORPUS_INCOMPLETE"],
          coverage: [
            {
              company: "SAMSUNG_ELECTRONICS",
              business_year: 2026,
              reprt_code: "11011",
              fs_div: "CFS",
              status: "NOT_YET_FILED",
            },
          ],
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );

    render(
      <FinancialExplorer
        defaultCompany="SAMSUNG_ELECTRONICS"
        defaultYear={2026}
        defaultReportCode="11011"
        availableYears={[2026]}
      />,
    );
    await user.click(screen.getByRole("button", { name: "재무제표 조회" }));

    expect(screen.getByText(/2026년 사업보고서는 아직 공시 전/)).toBeVisible();
    expect(screen.queryByText(/CORPUS_INCOMPLETE/)).not.toBeInTheDocument();
  });
});
