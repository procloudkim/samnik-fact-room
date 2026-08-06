import { useEffect, useMemo, useReducer, useRef, useState, type FormEvent } from "react";
import { streamChatTurn } from "./api/stream";
import { EvidencePanel } from "./components/EvidencePanel";
import { FinancialExplorer } from "./components/FinancialExplorer";
import { QueryPanel } from "./components/QueryPanel";
import type { TimelinePeriod, TimelineResponse } from "./types";
import {
  buildMessageWithOverrides,
  chatReducer,
  initialChatState,
} from "./state/chatReducer";
import "./styles.css";

const REPORT_CODE_OPTIONS = [
  { code: "11013", label: "1분기보고서 · 1~3월 누적" },
  { code: "11012", label: "반기보고서 · 1~6월 누적" },
  { code: "11014", label: "3분기보고서 · 1~9월 누적" },
  { code: "11011", label: "사업보고서 · 연간·4분기 포함" },
] as const;

const COMPANY_SCOPE_OPTIONS = [
  { code: "SAMSUNG_ELECTRONICS", label: "삼성전자" },
  { code: "SK_HYNIX", label: "SK하이닉스" },
  { code: "COMPARE", label: "두 회사 비교" },
] as const;

const MIN_YEAR = 2015;

type CompanyScope = (typeof COMPANY_SCOPE_OPTIONS)[number]["code"];

function currentYear(): number {
  return new Date().getFullYear();
}

function rollingYearsFrom(anchorYear: number = currentYear()): number[] {
  const safeAnchor = Math.max(anchorYear, MIN_YEAR);
  const years: number[] = [];
  for (let year = safeAnchor; year >= MIN_YEAR; year -= 1) {
    years.push(year);
  }
  return years;
}

function parseLatestCommonPeriod(
  period: string | null,
): { year: number; report_code: string } | null {
  if (!period) return null;
  const match = /^(20\d{2})(Q1|H1|Q3)?$/.exec(period.trim());
  if (!match) return null;
  const year = Number(match[1]);
  const suffix = match[2] ?? "";
  const report_code = {
    "": "11011",
    Q1: "11013",
    H1: "11012",
    Q3: "11014",
  }[suffix] ?? "11011";
  return Number.isInteger(year) ? { year, report_code } : null;
}

function createRequestId(): string {
  return globalThis.crypto?.randomUUID?.() ?? `request-${Date.now()}`;
}

function phaseStatus(phase: typeof initialChatState.phase): string {
  switch (phase) {
    case "streaming":
      return "공시 근거 검증을 진행하고 있습니다.";
    case "awaiting_user":
      return "추가 확인이 필요한 질문이 도착했습니다.";
    case "complete":
      return "팩트체크가 완료되었습니다. 공시 판정 기록을 확인하세요.";
    case "error":
      return "팩트체크를 완료하지 못했습니다.";
    case "cancelled":
      return "팩트체크가 중단되었습니다.";
    default:
      return "질문을 입력할 준비가 되었습니다.";
  }
}

function summarizeCoverageStatus(
  periods: readonly TimelinePeriod[],
  year: number,
  reportCode: string,
  companyScope: CompanyScope,
) {
  const normalizedCompanies =
    companyScope === "COMPARE"
      ? ["SAMSUNG_ELECTRONICS", "SK_HYNIX"]
      : [companyScope];
  const period = periods.find((row) => row.business_year === year && row.report_code === reportCode);
  if (!period) {
    return [];
  }

  const matches = period.companies.filter((company) =>
    normalizedCompanies.includes(company.company),
  );
  return matches.map((company) => ({
    company: company.company,
    cfs: company.cfs,
    ofs: company.ofs,
    filing_date_estimate: company.filing_date_estimate,
    expected_by: period.expected_by,
  }));
}

export default function App() {
  const [state, dispatch] = useReducer(chatReducer, initialChatState);
  const abortRef = useRef<AbortController | null>(null);

  const [selectedScope, setSelectedScope] = useState<CompanyScope>(COMPANY_SCOPE_OPTIONS[0].code);
  const [selectedReportCode, setSelectedReportCode] = useState<string>(
    REPORT_CODE_OPTIONS[0].code,
  );
  const [selectedYear, setSelectedYear] = useState<number>(currentYear());
  const [financialYears, setFinancialYears] = useState<number[]>(rollingYearsFrom());
  const [timeline, setTimeline] = useState<TimelineResponse | null>(null);
  const [timelineLoaded, setTimelineLoaded] = useState(false);

  const selectedCompanies = useMemo(
    () =>
      selectedScope === "COMPARE"
        ? ["SAMSUNG_ELECTRONICS", "SK_HYNIX"]
        : [selectedScope],
    [selectedScope],
  );

  const selectedCoverage = useMemo(
    () =>
      timeline
        ? summarizeCoverageStatus(timeline.periods, selectedYear, selectedReportCode, selectedScope)
        : [],
    [selectedYear, selectedReportCode, selectedScope, timeline],
  );

  const companyLabel = useMemo(() => {
    if (selectedScope === "COMPARE") return "삼성전자 · SK하이닉스";
    return COMPANY_SCOPE_OPTIONS.find((option) => option.code === selectedScope)?.label ?? selectedScope;
  }, [selectedScope]);

  const reportLabel = useMemo(
    () =>
      REPORT_CODE_OPTIONS.find((option) => option.code === selectedReportCode)?.label ??
      selectedReportCode,
    [selectedReportCode],
  );

  const isScopeUnavailable = selectedCoverage.some(
    (entry) => entry.cfs.status !== "LOADED",
  );

  useEffect(() => () => abortRef.current?.abort(), []);

  useEffect(() => {
    let isCancelled = false;

    async function loadTimeline() {
      try {
        const response = await fetch("/api/v1/financials/timeline");
        if (!response.ok) {
          throw new Error(`timeline failed: ${response.status}`);
        }
        const payload = (await response.json()) as TimelineResponse | null;
        if (!payload?.periods || !Array.isArray(payload.periods)) {
          throw new Error("timeline contract mismatch");
        }

        const latestYears = Array.from(
          new Set(payload.periods.map((period) => period.business_year)),
        ).sort((a, b) => b - a);

        const years = latestYears.length > 0 ? latestYears : rollingYearsFrom();
        const parsedLatest = parseLatestCommonPeriod(payload.latest_common_period);
        const fallbackYear = years[0] ?? rollingYearsFrom()[0];
        const nextYear = parsedLatest?.year ?? fallbackYear;
        const nextReportCode = parsedLatest?.report_code ?? REPORT_CODE_OPTIONS[0].code;

        if (!isCancelled) {
          setTimeline({
            as_of: payload.as_of,
            start_year: payload.start_year,
            end_year: payload.end_year,
            completed_years: payload.completed_years,
            filing_estimate_window_years: payload.filing_estimate_window_years,
            latest_common_period: payload.latest_common_period,
            periods: payload.periods,
          });
          setFinancialYears(years);
          setSelectedYear((current) => (years.includes(current) ? current : nextYear));
          const resolvedReportCode =
            payload.periods.some((item) => item.business_year === nextYear && item.report_code === nextReportCode)
              ? nextReportCode
              : payload.periods.length > 0
                ? payload.periods[0].report_code
                : REPORT_CODE_OPTIONS[0].code;
          setSelectedReportCode(resolvedReportCode);
          setTimelineLoaded(true);
        }
      } catch (_error) {
        if (!isCancelled) {
          setFinancialYears(rollingYearsFrom());
          setSelectedYear((year) => (rollingYearsFrom().includes(year) ? year : rollingYearsFrom()[0]));
          setSelectedReportCode("11013");
          setTimeline(null);
        }
      } finally {
        if (!isCancelled) {
          setTimelineLoaded(true);
        }
      }
    }

    void loadTimeline();
    return () => {
      isCancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!financialYears.includes(selectedYear)) {
      setSelectedYear(financialYears[0] ?? MIN_YEAR);
    }
  }, [financialYears, selectedYear]);

  async function runTurn(message: string, retry = false) {
    const clientRequestId = createRequestId();
    const controller = new AbortController();
    abortRef.current = controller;

    dispatch(
      retry
        ? { type: "turn.restarted", clientRequestId }
        : { type: "turn.submitted", message, clientRequestId },
    );

    try {
      await streamChatTurn(
        {
          conversation_id: state.conversationId,
          client_request_id: clientRequestId,
          message,
          context: {
            companies: selectedCompanies,
            business_year: selectedYear,
            report_code: selectedReportCode,
            fs_div: "CFS",
          },
        },
        (event) => dispatch({ type: "event.received", event }),
        { signal: controller.signal },
      );
    } catch (error) {
      if (!controller.signal.aborted) {
        dispatch({
          type: "stream.failed",
          message:
            error instanceof Error
              ? error.message
              : "네트워크 응답을 확인하지 못했습니다.",
        });
      }
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
    }
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (state.phase === "streaming" || !state.draft.trim() || isScopeUnavailable) return;
    const message = buildMessageWithOverrides(state.draft, state.slotOverrides);
    void runTurn(message);
  }

  function handleCancel() {
    abortRef.current?.abort();
    dispatch({ type: "run.cancelled" });
  }

  function handleRetry() {
    if (!state.lastSubmittedMessage || state.phase === "streaming") return;
    void runTurn(state.lastSubmittedMessage, true);
  }

  function handleReset() {
    abortRef.current?.abort();
    dispatch({ type: "conversation.reset" });
  }

  return (
    <div className="app-shell">
      <header className="site-header">
        <div className="brand-lockup">
          <span className="brand-monogram" aria-hidden="true">
            S·H
          </span>
          <div>
            <p>OpenDART evidence service</p>
            <h1>삼전닉스 팩트룸</h1>
          </div>
        </div>
        <div className="header-meta">
          <span>Samsung Electronics</span>
          <span aria-hidden="true">×</span>
          <span>SK hynix</span>
        </div>
      </header>

      <main id="main-content" className="workspace">
      <QueryPanel
        state={state}
        dispatch={dispatch}
        onSubmit={handleSubmit}
        onCancel={handleCancel}
        onReset={handleReset}
        companyScope={selectedScope}
        companyLabel={companyLabel}
        companyOptions={COMPANY_SCOPE_OPTIONS}
        onCompanyScopeChange={setSelectedScope}
        reportCodes={REPORT_CODE_OPTIONS}
        reportCode={selectedReportCode}
        reportLabel={reportLabel}
        year={selectedYear}
        availableYears={financialYears}
        onYearChange={setSelectedYear}
        onReportCodeChange={setSelectedReportCode}
        timelineCoverage={selectedCoverage}
        progress={state.progress}
        isTimelineUnavailable={isScopeUnavailable}
        timelineReady={timelineLoaded}
      />
        <EvidencePanel
          state={state}
          onRetry={handleRetry}
          onUsePrompt={(prompt) => dispatch({ type: "prompt.selected", value: prompt })}
        />
      </main>

      <details className="financial-explorer-details">
        <summary>
          <span className="expert-summary-copy">
            <small>Professional filing workspace</small>
            <strong>전문가용 원문 재무제표</strong>
          </span>
          <span className="expert-summary-basis">Samsung blue · SK red · CFS/OFS</span>
        </summary>
        <FinancialExplorer
          defaultCompany={selectedCompanies[0] ?? COMPANY_SCOPE_OPTIONS[0].code}
          defaultYear={selectedYear}
          defaultReportCode={selectedReportCode}
          availableYears={financialYears}
        />
      </details>

      <footer className="site-footer">
        <p>공식 공시를 먼저 읽는 투자정보 습관</p>
        <p>실시간 시세·투자 추천·목표주가 제공 안 함</p>
      </footer>

      <div className="sr-only" role="status" aria-live="polite" aria-atomic="true">
        {phaseStatus(state.phase)}
      </div>
    </div>
  );
}
