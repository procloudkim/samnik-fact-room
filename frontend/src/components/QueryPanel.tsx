import type { FormEvent, KeyboardEvent } from "react";
import type { ChatAction, ChatState, ProgressEntry } from "../state/chatReducer";
import type { CoverageStatusValue, FilingDateEstimate } from "../types";

interface CompanyOption {
  code: string;
  label: string;
}

type CompanyScopeCode = "SAMSUNG_ELECTRONICS" | "SK_HYNIX" | "COMPARE";

interface CoverageEntry {
  company: string;
  cfs: { status: CoverageStatusValue };
  ofs: { status: CoverageStatusValue };
  filing_date_estimate?: FilingDateEstimate | null;
  expected_by: string;
}

interface QueryPanelProps {
  state: ChatState;
  dispatch: (action: ChatAction) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  onCancel: () => void;
  onReset: () => void;
  companyScope: CompanyScopeCode;
  companyOptions: readonly CompanyOption[];
  companyLabel: string;
  onCompanyScopeChange: (companyScope: CompanyScopeCode) => void;
  reportCodes: readonly CompanyOption[];
  reportCode: string;
  reportLabel: string;
  year: number;
  availableYears: number[];
  onYearChange: (year: number) => void;
  onReportCodeChange: (reportCode: string) => void;
  timelineCoverage: readonly CoverageEntry[];
  progress: ProgressEntry[];
  isTimelineUnavailable: boolean;
  timelineReady: boolean;
}

const METRIC_LABELS: Record<string, string> = {
  operating_profit: "영업이익",
  revenue: "매출액",
  net_income: "당기순이익",
  total_assets: "자산총계",
};

const REPORT_NOTE = {
  "11013": "1분기보고서(1~3월 누적)",
  "11012": "반기보고서(1~6월 누적)",
  "11014": "3분기보고서(1~9월 누적)",
  "11011": "사업보고서(연간·4분기 포함)",
} as const;

const COMPANY_LABELS: Record<string, string> = {
  SAMSUNG_ELECTRONICS: "삼성전자",
  SK_HYNIX: "SK하이닉스",
};

const COVERAGE_STATUS_LABELS: Record<CoverageStatusValue, string> = {
  LOADED: "공시 확인",
  NOT_YET_FILED: "공시 전",
  NOT_FILED: "미공시",
  SOURCE_NOT_AVAILABLE: "원문 미제공",
  FAILED: "확인 필요",
};

const STAGE_SHORT_LABEL: Record<string, string> = {
  SUPERVISOR_ROUTING: "질문 분류",
  SCENARIO_SLOT_CHECK: "조건 확인",
  SQL_RETRIEVAL: "재무 수치 조회",
  VECTOR_RETRIEVAL: "공시 본문 검색",
  CONTEXT_ANALYSIS: "맥락 종합",
  EXPLANATION_SYNTHESIS: "쉬운 해설",
  EVIDENCE_VALIDATION: "근거 검증",
  SUPERVISOR_FINALIZING: "최종 답변",
};

function buildPromptTemplates({
  company,
  compareCompany,
  year,
  reportCode,
}: {
  company: string;
  compareCompany: string | null;
  year: number;
  reportCode: string;
}) {
  const report = REPORT_NOTE[reportCode as keyof typeof REPORT_NOTE];
  const core = [
    {
      lens: "본업 수익성",
      prompt: `${year}년 ${company} ${report} 매출액과 영업이익으로 본업 수익성을 진단해줘.`,
    },
    {
      lens: "최종 수익성",
      prompt: `${year}년 ${company} ${report} 매출액과 당기순이익으로 최종 수익성을 진단해줘.`,
    },
    {
      lens: "현금 전환력",
      prompt: `${year}년 ${company} ${report} 영업이익과 영업활동현금흐름으로 현금 전환력을 진단해줘.`,
    },
    {
      lens: "재무건전성",
      prompt: `${year}년 ${company} ${report} 자산·부채·자본 구조로 재무건전성을 진단해줘.`,
    },
  ];
  if (compareCompany) {
    core.push({
      lens: "동일 기준 상대비교",
      prompt: `${company}와 ${compareCompany}의 ${year}년 ${report} 영업이익을 같은 연결·누적 기준으로 비교해줘.`,
    });
  }
  const advanced = [
    {
      lens: "운전자본 리스크",
      prompt: `${company} ${year}년 ${report}에서 재고자산과 매출채권의 위험 신호를 공시 근거로 점검해줘.`,
    },
    {
      lens: "이익의 질",
      prompt: `${company} ${year}년 ${report}에서 일회성 손익과 손상차손 등 이익의 지속성을 판단할 근거를 찾아줘.`,
    },
    {
      lens: "희석 리스크",
      prompt: `${company} ${year}년 ${report}에서 전환사채·스톡옵션·유상증자 등 주식 희석 가능성을 확인해줘.`,
    },
    {
      lens: "자본배분 정책",
      prompt: `${company} ${year}년 ${report}에서 설비투자·배당·자사주 관련 자본배분 근거를 정리해줘.`,
    },
    {
      lens: reportCode === "11011" ? "감사·회계 쟁점" : "사업 집중도",
      prompt:
        reportCode === "11011"
          ? `${company} ${year}년 사업보고서의 핵심감사사항과 주요 회계 위험을 근거와 함께 확인해줘.`
          : `${company} ${year}년 ${report}에서 사업부문·주요 고객 집중도와 관련된 위험 근거를 찾아줘.`,
    },
  ];
  return { core, advanced };
}

function formatMonthDay(value: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) return value;
  return `${Number(match[2])}월 ${Number(match[3])}일`;
}

function ContextSelector({
  companyOptions,
  companyScope,
  reportCodes,
  reportCode,
  year,
  availableYears,
  onCompanyScopeChange,
  onYearChange,
  onReportCodeChange,
}: {
  companyOptions: readonly CompanyOption[];
  companyScope: CompanyScopeCode;
  reportCodes: readonly CompanyOption[];
  reportCode: string;
  year: number;
  availableYears: number[];
  onCompanyScopeChange: (companyScope: CompanyScopeCode) => void;
  onYearChange: (year: number) => void;
  onReportCodeChange: (reportCode: string) => void;
}) {
  return (
    <section className="context-selector" aria-label="조회 컨텍스트 선택">
      <label className="context-field">
        대상
        <select
          value={companyScope}
          onChange={(event) =>
            onCompanyScopeChange(event.target.value as CompanyScopeCode)
          }
        >
          {companyOptions.map((option) => (
            <option value={option.code} key={option.code}>
              {option.label}
            </option>
          ))}
        </select>
      </label>

      <label className="context-field">
        기준 연도
        <select value={String(year)} onChange={(event) => onYearChange(Number(event.target.value))}>
          {availableYears.map((option) => (
            <option value={option} key={option}>
              {option}년
            </option>
          ))}
        </select>
      </label>

      <label className="context-field context-field--report">
        보고서
        <select value={reportCode} onChange={(event) => onReportCodeChange(event.target.value)}>
          {reportCodes.map((option) => (
            <option value={option.code} key={option.code}>
              {option.label}
            </option>
          ))}
        </select>
      </label>
    </section>
  );
}

function PromptChips({
  company,
  compareCompany,
  year,
  reportCode,
  onPromptSelect,
  disabled,
}: {
  company: string;
  compareCompany: string | null;
  year: number;
  reportCode: string;
  disabled: boolean;
  onPromptSelect: (prompt: string) => void;
}) {
  const prompts = buildPromptTemplates({ company, compareCompany, year, reportCode });

  function renderPrompt({ lens, prompt }: { lens: string; prompt: string }) {
    return (
      <button
        className="prompt-chip"
        type="button"
        key={prompt}
        disabled={disabled}
        onClick={() => onPromptSelect(prompt)}
      >
        <span>{lens}</span>
        <strong>{prompt}</strong>
      </button>
    );
  }

  return (
    <section className="research-prompts" aria-label="리서치 질문 카드">
      <div className="research-prompts-heading">
        <div>
          <span>Financial signals, explained</span>
          <h3>숫자로 읽는 회사의 체력</h3>
        </div>
        <p>공시 수치로 바로 확인하는 투자 판단의 기초 체력</p>
      </div>
      <div className="prompt-list" aria-label="숫자로 읽는 회사의 체력 질문">
        {prompts.core.map(renderPrompt)}
      </div>
      <details className="advanced-prompts">
        <summary>
          <div>
            <span>Advanced disclosure lens</span>
            <strong>심화 공시 인사이트</strong>
          </div>
          <small>근거를 찾고, 숫자의 의미와 다음 확인 항목까지 쉽게 해설</small>
        </summary>
        <div className="prompt-list prompt-list--advanced" aria-label="심화 공시 인사이트 질문">
          {prompts.advanced.map(renderPrompt)}
        </div>
      </details>
    </section>
  );
}

function MissingSlots({ slots }: { slots: string[] }) {
  if (!slots.length) return null;

  return (
    <section className="missing-slot-banner" aria-label="추가로 필요한 조건">
      <strong>추가로 필요한 조건</strong>
      <div className="missing-chips">
        {slots.map((slot) => (
          <span className="missing-chip" key={slot}>
            {slot.replaceAll("_", " ")}
          </span>
        ))}
      </div>
    </section>
  );
}

function CoverageStatusRail({
  coverage,
  reportLabel,
  year,
  isUnavailable,
}: {
  coverage: readonly CoverageEntry[];
  reportLabel: string;
  year: number;
  isUnavailable: boolean;
}) {
  if (coverage.length === 0) {
    return null;
  }

  return (
    <section className="timeline-status" aria-label={`${year}년 ${reportLabel} 적재 상태`}>
      <p className="timeline-status-title">
        {year}년 {reportLabel}
      </p>
      <ul className="timeline-status-list">
        {coverage.map((entry) => {
          const estimate = entry.filing_date_estimate;
          const isAwaitingFiling = entry.cfs.status === "NOT_YET_FILED";
          return (
            <li key={`${entry.company}-${entry.cfs.status}-${entry.ofs.status}`}>
              <div className="timeline-status-row">
                <strong className="timeline-status-company">
                  {COMPANY_LABELS[entry.company] ?? entry.company}
                </strong>
                <span>연결 · {COVERAGE_STATUS_LABELS[entry.cfs.status]}</span>
                <span>별도 · {COVERAGE_STATUS_LABELS[entry.ofs.status]}</span>
              </div>
              {isAwaitingFiling && estimate ? (
                <p className="filing-date-forecast">
                  최근 {estimate.sample_size}개년 최초 접수 흐름상
                  <strong> {formatMonthDay(estimate.estimated_date)} 전후</strong> 공시가 예상됩니다.
                </p>
              ) : null}
            </li>
          );
        })}
      </ul>
      {isUnavailable ? (
        <p className="filing-date-methodology">
          예상일은 과거 최초 공시 접수일과 각 보고기간 말일의 평균 시차를 적용한 참고치입니다.
          {coverage[0]?.expected_by
            ? ` 법정 제출기한은 ${formatMonthDay(coverage[0].expected_by)}이며 실제 공시일은 달라질 수 있습니다.`
            : " 실제 공시일은 달라질 수 있습니다."}
        </p>
      ) : null}
    </section>
  );
}

function ProgressRail({
  isStreaming,
  progress,
  phaseLabel,
}: {
  isStreaming: boolean;
  progress: ProgressEntry[];
  phaseLabel: string;
}) {
  if (progress.length === 0 && !isStreaming) return null;
  return (
    <section className="workflow" aria-label="진행 상태">
      <div className="workflow-heading">
        <h3>검증 진행</h3>
        <span>{phaseLabel}</span>
      </div>
      <ol className="workflow-list">
        {progress.map((entry, index) => {
          const active = isStreaming && index === progress.length - 1;
          return (
            <li className={active ? "is-active" : "is-complete"} key={`${entry.stage}-${index}`}>
              <span className="workflow-marker" aria-hidden="true">
                {active ? "" : "✓"}
              </span>
              <div>
                <strong>{STAGE_SHORT_LABEL[entry.stage] ?? entry.stage}</strong>
                <p>
                  {entry.agent} · {entry.tool}
                </p>
              </div>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

export function QueryPanel({
  state,
  dispatch,
  onSubmit,
  onCancel,
  onReset,
  companyScope,
  companyOptions,
  companyLabel,
  onCompanyScopeChange,
  reportCodes,
  reportCode,
  reportLabel,
  year,
  availableYears,
  onYearChange,
  onReportCodeChange,
  timelineCoverage,
  progress,
  isTimelineUnavailable,
  timelineReady,
}: QueryPanelProps) {
  const isStreaming = state.phase === "streaming";
  const hasContext = state.messages.length > 0;
  const compareCompany =
    companyScope === "COMPARE"
      ? companyOptions.find((option) => option.code === "SK_HYNIX")?.label ?? null
      : null;
  const promptCompany =
    companyScope === "COMPARE"
      ? companyOptions.find((option) => option.code === "SAMSUNG_ELECTRONICS")?.label ??
        "삼성전자"
      : companyLabel;
  const phaseLabel = isStreaming
    ? "검증 진행 중"
    : timelineReady
      ? isTimelineUnavailable
        ? "요청 불가"
        : "대기"
      : "초기화 중";
  const modelUsage = state.result?.trace_summary?.model_usage;

  function handleComposerKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      event.currentTarget.form?.requestSubmit();
    }
  }

  return (
    <section className="query-panel" aria-labelledby="query-title">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">Disclosure research desk</p>
          <h2 id="query-title">공시 리서치</h2>
        </div>
        {hasContext && !isStreaming ? (
          <button className="text-button" type="button" onClick={onReset}>
            새 대화
          </button>
        ) : null}
      </div>

      <p className="panel-intro">
        공식 공시를 기준으로 핵심 재무지표와 주요 위험 신호를 검증합니다. 회사·사업연도·보고서
        유형을 지정하면 동일한 기준으로 조회합니다.
      </p>

      <ContextSelector
        companyOptions={companyOptions}
        companyScope={companyScope}
        reportCodes={reportCodes}
        reportCode={reportCode}
        year={year}
        availableYears={availableYears}
        onCompanyScopeChange={onCompanyScopeChange}
        onYearChange={onYearChange}
        onReportCodeChange={onReportCodeChange}
      />

      <div className="query-context-summary" aria-live="polite" aria-atomic="true">
        <span className="context-chip">회사: {companyLabel}</span>
        <span className="context-chip">연도: {year}년</span>
        <span className="context-chip">보고서: {reportLabel}</span>
      </div>

      <p className="context-scope-hint" aria-label="전용 조회 가이드">
        {companyLabel}의 {year}년 {reportLabel}을 투자 체크리스트 관점으로 검증해 보세요.
      </p>

      <PromptChips
        company={promptCompany}
        compareCompany={compareCompany}
        year={year}
        reportCode={reportCode}
        disabled={isStreaming}
        onPromptSelect={(prompt) => dispatch({ type: "prompt.selected", value: prompt })}
      />

      <ProgressRail isStreaming={isStreaming} progress={progress} phaseLabel={phaseLabel} />

      {modelUsage ? (
        <p className="model-usage-note" aria-label="이번 답변 모델 사용량">
          {modelUsage.called
            ? `외부 LLM ${modelUsage.total_tokens.toLocaleString("ko-KR")} tokens · ${modelUsage.usage_status === "REPORTED" ? "공급자 집계" : "집계 미제공"}`
            : "외부 LLM 호출 0회 · 규칙 분류와 검증 도구로 처리"}
        </p>
      ) : null}

      {state.messages.length > 0 ? (
        <ol
          className="conversation-log"
          role="log"
          aria-live="polite"
          aria-relevant="additions"
          aria-label="대화 기록"
        >
          {state.messages.map((message) => (
            <li className={`message message--${message.role}`} key={message.id}>
              <span className="message-role">
                {message.role === "user" ? "사용자" : "팩트체크 에이전트"}
              </span>
              <p>{message.content}</p>
            </li>
          ))}
        </ol>
      ) : null}

      <MissingSlots slots={state.missingSlots} />

      <CoverageStatusRail
        coverage={timelineCoverage}
        year={year}
        reportLabel={reportLabel}
        isUnavailable={isTimelineUnavailable}
      />

      <form className="query-form" onSubmit={onSubmit}>
        <label htmlFor="fact-query">공시 기반 질의</label>
        <textarea
          id="fact-query"
          rows={4}
          maxLength={1200}
          placeholder={`예: ${year}년 ${companyLabel} ${reportLabel} ${METRIC_LABELS.operating_profit}은 얼마야?`}
          value={state.draft}
          disabled={isStreaming}
          onKeyDown={handleComposerKeyDown}
          onChange={(event) =>
            dispatch({ type: "draft.changed", value: event.target.value })
          }
        />
        <p className="composer-security-note">
          공시 문서는 근거 데이터로만 읽습니다. 문서 안의 명령은 실행하지 않으며 비밀값도
          공개하지 않습니다.
        </p>
        <div className="form-footer">
          <p>Enter로 확인 · Shift+Enter로 줄바꿈</p>
          <div className="form-actions">
            {isStreaming ? (
              <button className="secondary-button" type="button" onClick={onCancel}>
                중단
              </button>
            ) : null}
            <button
              className="primary-button"
              type="submit"
              disabled={isStreaming || !state.draft.trim() || isTimelineUnavailable}
            >
              {state.phase === "awaiting_user" ? "조건 보내기" : "공시 근거 확인"}
            </button>
          </div>
          {isTimelineUnavailable ? (
            <p className="scope-note" role="status">
              현재 공식 수치가 확인되지 않아 질문 전송을 잠시 막았습니다. 위 공시 예상일 이후
              OpenDART 수집 상태를 다시 확인해 주세요.
            </p>
          ) : null}
        </div>
      </form>

      <p className="scope-note">
        매수·매도 추천과 주가예측은 제공하지 않습니다. 답변은 OpenDART에 적재된
        공시 범위와 표시된 기준일 안에서만 유효합니다.
      </p>
    </section>
  );
}
