import { useEffect, useMemo, useState, type FormEvent } from "react";

type QueryMode = "FACT" | "TIME_SERIES" | "COMPARE" | "YOY" | "CFS_OFS_DIFF" | "STATEMENT";

interface FinancialExplorerProps {
  defaultCompany: string;
  defaultYear: number;
  defaultReportCode: string;
  availableYears: number[];
}

interface FinancialRow {
  company: string;
  company_label: string;
  period: string;
  fs_div: string;
  metric_key?: string | null;
  account_id: string;
  account_nm: string;
  account_detail?: string | null;
  statement: string;
  raw_amount: string;
  value_display: string;
  value_basis: string;
  rcept_no: string;
  source_url: string;
}

interface AccountCandidate {
  company: string;
  account_id: string;
  account_nm: string;
  sj_div: string;
  fs_div: string;
  metric_key?: string | null;
}

interface ReportCodeOption {
  code: string;
  label: string;
}

interface CompanyOption {
  code: string;
  label: string;
}

interface CoverageRow {
  company: string;
  business_year: number;
  reprt_code: string;
  fs_div: string;
  status: "LOADED" | "NOT_FILED" | "NOT_YET_FILED" | "SOURCE_NOT_AVAILABLE" | "FAILED";
}

const METRICS = [
  ["revenue", "매출액"],
  ["operating_profit", "영업이익"],
  ["net_income", "당기순이익"],
  ["total_assets", "자산총계"],
  ["total_liabilities", "부채총계"],
  ["total_equity", "자본총계"],
  ["operating_cash_flow", "영업활동현금흐름"],
] as const;

const REPORT_CODE_OPTIONS: readonly ReportCodeOption[] = [
  { code: "11013", label: "1분기보고서 · 1~3월 누적" },
  { code: "11012", label: "반기보고서 · 1~6월 누적" },
  { code: "11014", label: "3분기보고서 · 1~9월 누적" },
  { code: "11011", label: "사업보고서 · 연간·4분기 포함" },
];

const COMPANY_OPTIONS: readonly CompanyOption[] = [
  { code: "SAMSUNG_ELECTRONICS", label: "삼성전자" },
  { code: "SK_HYNIX", label: "SK하이닉스" },
];

const STATEMENT_PAGE_SIZE = 24;

const SUMMARY_ACCOUNTS = [
  { metric: "revenue", label: "매출액", pattern: /매출액|영업수익|수익\(매출액\)/ },
  { metric: "operating_profit", label: "영업이익", pattern: /^영업이익$/ },
  { metric: "net_income", label: "당기순이익", pattern: /당기순이익|분기순이익|반기순이익/ },
  { metric: "total_assets", label: "자산총계", pattern: /^자산총계$/ },
  { metric: "total_liabilities", label: "부채총계", pattern: /^부채총계$/ },
  { metric: "total_equity", label: "자본총계", pattern: /^자본총계$/ },
  { metric: "operating_cash_flow", label: "영업현금흐름", pattern: /영업활동.*현금흐름/ },
] as const;

const PREFERRED_STATEMENTS: Record<string, readonly string[]> = {
  revenue: ["IS", "CIS"],
  operating_profit: ["IS", "CIS"],
  net_income: ["IS", "CIS"],
  total_assets: ["BS"],
  total_liabilities: ["BS"],
  total_equity: ["BS"],
  operating_cash_flow: ["CF"],
};

function summaryRowScore(row: FinancialRow, metricKey: string, pattern: RegExp): number {
  const preferred = PREFERRED_STATEMENTS[metricKey] ?? [];
  const statementRank = preferred.indexOf(row.statement);
  let score = row.metric_key === metricKey ? 100 : 0;
  if (pattern.test(row.account_nm.trim())) score += 30;
  if (statementRank >= 0) score += 60 - statementRank * 10;
  if (!row.account_detail || row.account_detail === "-") score += 20;
  return score;
}

function financialWarningMessages(
  warningCodes: string[],
  coverage: CoverageRow[],
  year: string,
  reportCode: string,
): string[] {
  const reportLabel = REPORT_CODE_OPTIONS.find((item) => item.code === reportCode)?.label
    .split(" · ")[0] ?? "선택한 보고서";
  const statuses = new Set(coverage.map((item) => item.status));
  const messages: string[] = [];
  if (statuses.has("NOT_YET_FILED")) {
    messages.push(`${year}년 ${reportLabel}는 아직 공시 전입니다. 직전 공시 기간을 선택해 주세요.`);
  } else if (statuses.has("NOT_FILED")) {
    messages.push(`${year}년 ${reportLabel} 제출 내역을 OpenDART에서 확인하지 못했습니다.`);
  } else if (statuses.has("SOURCE_NOT_AVAILABLE")) {
    messages.push(
      `${year}년 ${reportLabel}는 공시는 존재하지만 OpenDART 구조화 재무제표 원본이 제공되지 않습니다. 사업보고서 또는 공시 원문을 확인해 주세요.`,
    );
  } else if (statuses.has("FAILED")) {
    messages.push("선택한 재무제표의 적재 검증을 완료하지 못했습니다. 다른 기간을 조회해 주세요.");
  }
  if (warningCodes.includes("INSUFFICIENT_EVIDENCE") && messages.length === 0) {
    messages.push("선택한 범위에서 조회 가능한 구조화 재무제표 행을 찾지 못했습니다.");
  }
  if (warningCodes.includes("CORPUS_INCOMPLETE") && messages.length === 0) {
    messages.push("선택한 범위의 일부 공시가 아직 적재되지 않아, 확인된 행만 표시합니다.");
  }
  return messages;
}

function formatRawAmount(value: string): string {
  try {
    return `${BigInt(value).toLocaleString("ko-KR")}원`;
  } catch {
    return `${value}원`;
  }
}

function clampYearOptions(years: number[]): number[] {
  const unique = [...new Set(years)].filter(Number.isFinite);
  if (unique.length > 0) return [...unique].sort((a, b) => b - a);
  const currentYear = new Date().getFullYear();
  const fallback: number[] = [];
  for (let year = currentYear; year >= 2015; year -= 1) fallback.push(year);
  return fallback;
}

export function FinancialExplorer({
  defaultCompany,
  defaultYear,
  defaultReportCode,
  availableYears,
}: FinancialExplorerProps) {
  const [mode, setMode] = useState<QueryMode>("FACT");
  const [company, setCompany] = useState(defaultCompany);
  const [metric, setMetric] = useState("operating_profit");
  const [reportCode, setReportCode] = useState(defaultReportCode);
  const [startYear, setStartYear] = useState(String(defaultYear));
  const [endYear, setEndYear] = useState(String(defaultYear));
  const [rows, setRows] = useState<FinancialRow[]>([]);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [visibleLimit, setVisibleLimit] = useState(STATEMENT_PAGE_SIZE);
  const [accountQuery, setAccountQuery] = useState("");
  const [accounts, setAccounts] = useState<AccountCandidate[]>([]);

  const years = useMemo(() => clampYearOptions(availableYears), [availableYears]);

  useEffect(() => {
    setCompany(defaultCompany);
  }, [defaultCompany]);

  useEffect(() => {
    const normalizedYear = String(defaultYear);
    setStartYear(normalizedYear);
    setEndYear(normalizedYear);
  }, [defaultYear]);

  useEffect(() => {
    setReportCode(defaultReportCode);
  }, [defaultReportCode]);

  const visibleRows = mode === "STATEMENT" ? rows.slice(0, visibleLimit) : rows;

  const summaryRows = useMemo(
    () =>
      SUMMARY_ACCOUNTS.flatMap(({ metric: metricKey, label, pattern }) => {
        const row = rows
          .filter(
            (candidate) =>
              candidate.metric_key === metricKey || pattern.test(candidate.account_nm.trim()),
          )
          .sort(
            (left, right) =>
              summaryRowScore(right, metricKey, pattern)
              - summaryRowScore(left, metricKey, pattern),
          )[0];
        return row ? [{ label, row }] : [];
      }),
    [rows],
  );

  const valueBasis = useMemo(() => {
    const stockMetrics = new Set(["total_assets", "total_liabilities", "total_equity"]);
    if (stockMetrics.has(metric)) return "POINT_IN_TIME";
    return reportCode === "11011" ? "ANNUAL" : "YTD";
  }, [metric, reportCode]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setLoading(true);
    setWarnings([]);
    setVisibleLimit(STATEMENT_PAGE_SIZE);
    try {
      const companies = mode === "COMPARE"
        ? COMPANY_OPTIONS.map((item) => item.code)
        : [company];
      const fsDivs = mode === "CFS_OFS_DIFF" ? ["CFS", "OFS"] : ["CFS"];
      const response = await fetch("/api/v1/financials/query", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          mode,
          companies,
          measures: [{ metric_key: metric }],
          start_period: startYear,
          end_period: endYear,
          report_codes: [reportCode],
          fs_divs: fsDivs,
          value_basis: valueBasis,
        }),
      });
      if (!response.ok) throw new Error("재무제표 저장소를 사용할 수 없습니다.");
      const payload = await response.json() as {
        facts: FinancialRow[];
        warnings: string[];
        coverage?: CoverageRow[];
      };
      setRows(payload.facts);
      setWarnings(
        financialWarningMessages(
          payload.warnings ?? [],
          payload.coverage ?? [],
          startYear,
          reportCode,
        ),
      );
    } catch (error) {
      setRows([]);
      setWarnings([error instanceof Error ? error.message : "조회에 실패했습니다."]);
    } finally {
      setLoading(false);
    }
  }

  function changeMode(nextMode: QueryMode) {
    setMode(nextMode);
    setRows([]);
    setWarnings([]);
    setVisibleLimit(STATEMENT_PAGE_SIZE);
  }

  async function searchAccounts(event: FormEvent) {
    event.preventDefault();
    if (!accountQuery.trim()) return;
    const params = new URLSearchParams({ q: accountQuery, company });
    const response = await fetch(`/api/v1/financials/accounts?${params}`);
    if (!response.ok) {
      setAccounts([]);
      return;
    }
    const payload = await response.json() as { accounts: AccountCandidate[] };
    setAccounts(payload.accounts);
  }

  return (
    <section className="financial-explorer" aria-labelledby="financial-explorer-title">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">Professional filing ledger</p>
          <h2 id="financial-explorer-title">원문 재무제표 워크벤치</h2>
          <p className="financial-explorer-intro">
            요약 수치에서 전체 계정과 접수 원문까지 같은 공시 기준으로 추적합니다.
          </p>
        </div>
        <div className="filing-brand-key" aria-label="회사 브랜드 기준">
          <span className="is-samsung">Samsung Electronics</span>
          <span className="is-hynix">SK hynix</span>
          <span>CFS · OFS</span>
        </div>
      </div>

      <form className="financial-controls" onSubmit={submit}>
        <label className="financial-control">
          조회 방식
          <select value={mode} onChange={(event) => changeMode(event.target.value as QueryMode)}>
            <option value="FACT">단일 재무지표</option>
            <option value="TIME_SERIES">시계열</option>
            <option value="COMPARE">회사 비교</option>
            <option value="YOY">전년 대비</option>
            <option value="CFS_OFS_DIFF">연결·별도 비교</option>
            <option value="STATEMENT">전체 재무제표</option>
          </select>
        </label>
        <label className="financial-control">
          회사
          <select value={company} onChange={(event) => setCompany(event.target.value)}>
            {COMPANY_OPTIONS.map((option) => (
              <option value={option.code} key={option.code}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <label className="financial-control">
          지표
          <select value={metric} onChange={(event) => setMetric(event.target.value)}>
            {METRICS.map(([value, label]) => (
              <option value={value} key={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label className="financial-control financial-control--report">
          보고서
          <select value={reportCode} onChange={(event) => setReportCode(event.target.value)}>
            {REPORT_CODE_OPTIONS.map((option) => (
              <option value={option.code} key={option.code}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <label className="financial-control">
          시작 연도
          <select value={startYear} onChange={(event) => setStartYear(event.target.value)}>
            {years.map((year) => (
              <option value={year} key={`s-${year}`}>
                {year}년
              </option>
            ))}
          </select>
        </label>
        <label className="financial-control">
          종료 연도
          <select value={endYear} onChange={(event) => setEndYear(event.target.value)}>
            {years.map((year) => (
              <option value={year} key={`e-${year}`}>
                {year}년
              </option>
            ))}
          </select>
        </label>
        <button className="primary-button" type="submit" disabled={loading}>
          {loading ? "조회 중" : "재무제표 조회"}
        </button>
      </form>

      {warnings.length ? <p className="financial-warning" role="status">{warnings.join(" · ")}</p> : null}
      {rows.length ? (
        <>
          {mode === "STATEMENT" ? (
            <section className="statement-summary" aria-labelledby="statement-summary-title">
              <div>
                <p className="eyebrow">Key accounts first</p>
                <h3 id="statement-summary-title">핵심 항목부터 확인하세요</h3>
                <p>전체 {rows.length.toLocaleString("ko-KR")}개 계정 중 자주 확인하는 항목을 먼저 정리했습니다.</p>
              </div>
              {summaryRows.length ? (
                <ul>
                  {summaryRows.map(({ label, row }) => (
                    <li key={`${label}-${row.account_id}`}>
                      <span>{label}</span>
                      <strong>{row.value_display}</strong>
                      <small>{row.fs_div} · {row.statement}</small>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="statement-summary-empty">표준 핵심 계정을 찾지 못해 원문 계정을 제공합니다.</p>
              )}
            </section>
          ) : null}

          <details
            className="statement-ledger"
            open={mode !== "STATEMENT" ? true : undefined}
          >
            <summary>
              <span>{mode === "STATEMENT" ? "전체 계정 원문" : "조회 결과"}</span>
              <small>
                {mode === "STATEMENT"
                  ? `${visibleRows.length.toLocaleString("ko-KR")} / ${rows.length.toLocaleString("ko-KR")}개 표시`
                  : `${rows.length.toLocaleString("ko-KR")}개`}
              </small>
            </summary>
            <div className="table-scroll" tabIndex={0} aria-label="재무제표 결과, 가로 스크롤 가능">
              <table>
                <thead>
                  <tr>
                    <th>회사</th>
                    <th>기간</th>
                    <th>기준</th>
                    <th>계정</th>
                    <th>금액</th>
                    <th>근거</th>
                  </tr>
                </thead>
                <tbody>
                  {visibleRows.map((row) => (
                    <tr key={`${row.company}-${row.period}-${row.fs_div}-${row.account_id}-${row.raw_amount}`}>
                      <th scope="row">{row.company_label}</th>
                      <td>{row.period}</td>
                      <td>
                        {row.fs_div}·{row.value_basis}
                      </td>
                      <td>
                        <strong>{row.account_nm}</strong>
                        <small>{row.account_id} · {row.statement}</small>
                      </td>
                      <td className="numeric-cell">
                        <strong>{row.value_display}</strong>
                        <small>{formatRawAmount(row.raw_amount)}</small>
                      </td>
                      <td>
                        <a href={row.source_url} target="_blank" rel="noreferrer">
                          {row.rcept_no}
                        </a>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {mode === "STATEMENT" && visibleRows.length < rows.length ? (
              <button
                className="ledger-more-button"
                type="button"
                onClick={() => setVisibleLimit((current) => current + STATEMENT_PAGE_SIZE)}
              >
                다음 {Math.min(STATEMENT_PAGE_SIZE, rows.length - visibleRows.length)}개 더 보기
              </button>
            ) : null}
          </details>
        </>
      ) : null}

      <details className="account-tools">
        <summary>전문 계정명·계정 ID 검색</summary>
        <form className="account-search" onSubmit={searchAccounts}>
          <label htmlFor="account-query">찾을 계정</label>
          <div>
            <input
              id="account-query"
              value={accountQuery}
              onChange={(event) => setAccountQuery(event.target.value)}
              placeholder="예: 유형자산"
            />
            <button className="secondary-button" type="submit">
              계정 찾기
            </button>
          </div>
        </form>
        {accounts.length ? (
          <div className="account-candidates" aria-label="계정 후보">
            {accounts.map((account) => (
              <button
                type="button"
                key={`${account.company}-${account.account_id}-${account.sj_div}`}
                onClick={() => setMetric(account.metric_key ?? metric)}
              >
                <strong>{account.account_nm}</strong>
                <span>
                  {account.account_id} · {account.sj_div} · {account.fs_div}
                </span>
              </button>
            ))}
          </div>
        ) : null}
      </details>
    </section>
  );
}
