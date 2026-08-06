export const PROGRESS_STAGES = [
  "SUPERVISOR_ROUTING",
  "SCENARIO_SLOT_CHECK",
  "SQL_RETRIEVAL",
  "VECTOR_RETRIEVAL",
  "CONTEXT_ANALYSIS",
  "EXPLANATION_SYNTHESIS",
  "EVIDENCE_VALIDATION",
  "SUPERVISOR_FINALIZING",
] as const;

export type ProgressStage = (typeof PROGRESS_STAGES)[number];

export const PROGRESS_LABELS: Record<ProgressStage, string> = {
  SUPERVISOR_ROUTING: "Supervisor가 질문 의도와 조건을 확인합니다",
  SCENARIO_SLOT_CHECK: "멀티턴 Agent가 부족한 조건을 확인합니다",
  SQL_RETRIEVAL: "RAG Agent가 정형 재무 수치를 조회합니다",
  VECTOR_RETRIEVAL: "RAG Agent가 공시 본문을 검색합니다",
  CONTEXT_ANALYSIS: "질문별 분석 Agent가 공시 맥락과 직접 근거를 종합합니다",
  EXPLANATION_SYNTHESIS: "재무 해설 Tutor가 숫자와 공시의 의미를 쉽게 풉니다",
  EVIDENCE_VALIDATION: "기간·단위·공시 근거를 검증합니다",
  SUPERVISOR_FINALIZING: "Supervisor가 근거 범위 안에서 답변을 구성합니다",
};

export const EVENT_TYPES = [
  "run.started",
  "workflow.progress",
  "clarification.required",
  "answer.completed",
  "run.failed",
] as const;

export type EventType = (typeof EVENT_TYPES)[number];

export interface EventEnvelope<TType extends EventType, TData> {
  schema_version: "1.0";
  event_id: string;
  request_id: string;
  conversation_id: string;
  seq: number;
  emitted_at: string;
  type: TType;
  data: TData;
}

export type SlotValues = Record<string, string>;

export interface RunStartedData {
  message?: string;
}

export interface WorkflowProgressData {
  stage: ProgressStage;
  label?: string;
  status?: "routing" | "started" | "completed" | "passed" | "failed" | "checked" | "updated" | "finalizing";
  agent?: string;
  tool?: string;
  summary?: ProgressSummary;
}

export type ProgressSummary =
  | { [key: string]: ProgressSummaryValue }
  | null;

export type ProgressSummaryValue =
  | string
  | number
  | boolean
  | null
  | ProgressSummary
  | ProgressSummaryValue[];

export interface ClarificationRequiredData {
  question: string;
  collected_slots: SlotValues;
  missing_slots: string[];
}

export interface EvidenceSource {
  evidence_id?: string;
  report_name: string;
  rcept_no: string;
  source_url: string;
  section?: string;
  rcept_dt?: string;
  excerpt?: string;
  excerpt_truncated?: boolean;
}

export interface AnalysisFinding {
  category_key: string;
  category_label: string;
  summary: string;
  plain_language?: string | null;
  next_check?: string | null;
  evidence_ids: string[];
}

export interface AnalysisReport {
  lens_key: string;
  lens_label: string;
  methodology: "CONTEXTUAL_DISCLOSURE_ANALYSIS_V2" | "FINANCIAL_RATIO_CALCULATION_V1";
  findings: AnalysisFinding[];
  source_count: number;
  limitation: string;
}

export interface FactRow {
  company: string;
  company_label?: string;
  metric: string;
  metric_label?: string;
  value?: number | string;
  raw_value?: string;
  raw_value_display?: string;
  value_display: string;
  value_trillion_krw?: string;
  value_trillion_krw_display?: string;
  unit?: string;
  period: string;
  period_type?: "ANNUAL" | "INTERIM";
  period_type_label?: string;
  period_label?: string;
  fs_div?: string;
  fs_div_label?: string;
  value_scope?: string;
  value_scope_label?: string;
  basis: string;
  evidence_ids?: string[];
}

export type Verdict =
  | "CONFIRMED"
  | "CONTRADICTED"
  | "REVIEW_REQUIRED"
  | "INSUFFICIENT_EVIDENCE"
  | "SYSTEM_UNAVAILABLE"
  | "OUT_OF_SCOPE";

export type DataStatus = "FRESH" | "STALE_CACHE" | "NO_DATA";

export interface TraceSummary {
  route?: string;
  agent_calls?: string[];
  state_transitions?: Array<{
    sequence: number;
    actor: string;
    event: string;
    route?: string;
    workflow_status?: string;
    agent?: string;
    retrieval_mode?: string;
    returned_to?: string;
    returned_from?: string;
  }>;
  final_actor?: string;
  vector_search_called?: boolean;
  elice_mlapi_called?: boolean;
  model_usage?: {
    called: boolean;
    provider: string;
    model: string;
    input_tokens: number;
    output_tokens: number;
    cached_tokens: number;
    total_tokens: number;
    usage_status: "REPORTED" | "UNREPORTED";
  } | null;
}

interface ResultBase {
  kind:
    | "single_fact"
    | "comparison"
    | "narrative_evidence"
    | "no_evidence"
    | "out_of_scope"
    | "system_error";
  verdict: Verdict;
  status?: Verdict;
  workflow_status?: string;
  failure_reason?: string | null;
  title: string;
  summary: string;
  facts: FactRow[];
  evidence: EvidenceSource[];
  evidence_total_count?: number;
  analysis_report?: AnalysisReport;
  interpretation: string;
  as_of: string;
  data_status?: DataStatus;
  limitations?: string[];
  interpretation_notes?: string[];
  trace_summary?: TraceSummary;
}

export interface SingleFactResult extends ResultBase {
  kind: "single_fact";
}

export interface ComparisonResult extends ResultBase {
  kind: "comparison";
  difference_display?: string;
  comparison_basis?: Record<string, boolean>;
}

export interface NarrativeEvidenceResult extends ResultBase {
  kind: "narrative_evidence";
}

export interface NoEvidenceResult extends ResultBase {
  kind: "no_evidence";
  searched_scope?: string;
}

export interface OutOfScopeResult extends ResultBase {
  kind: "out_of_scope";
  reason?: string;
  suggested_questions?: string[];
}

export interface SystemErrorResult extends ResultBase {
  kind: "system_error";
  code?: string;
  retryable?: boolean;
}

export type FactCheckResult =
  | SingleFactResult
  | ComparisonResult
  | NarrativeEvidenceResult
  | NoEvidenceResult
  | OutOfScopeResult
  | SystemErrorResult;

export interface AnswerCompletedData {
  result: FactCheckResult;
}

export interface RunFailedData {
  code: string;
  message: string;
  retryable: boolean;
}

export type CoverageStatusValue =
  | "LOADED"
  | "NOT_YET_FILED"
  | "NOT_FILED"
  | "SOURCE_NOT_AVAILABLE"
  | "FAILED";

export interface TurnContext {
  companies?: string[];
  business_year?: number;
  report_code?: string;
  metric_key?: string;
  fs_div?: "CFS" | "OFS";
}

export interface TimelineCellStatus {
  status: CoverageStatusValue;
  report_code: string;
  fs_div: "CFS" | "OFS";
}

export interface FilingDateEstimate {
  estimated_date: string;
  sample_size: number;
  history_start_year: number;
  history_end_year: number;
  average_lag_days: number;
  methodology: "MEAN_FIRST_FILING_LAG";
}

export interface TimelineCell {
  company: string;
  filing_date_estimate?: FilingDateEstimate | null;
  cfs: {
    status: CoverageStatusValue;
    fs_div: string;
    reason?: string | null;
    checked_at?: string | null;
  };
  ofs: {
    status: CoverageStatusValue;
    fs_div: string;
    reason?: string | null;
    checked_at?: string | null;
  };
}

export interface TimelinePeriod {
  business_year: number;
  report_code: string;
  period: string;
  label: string;
  period_end: string;
  expected_by: string;
  flow_value_basis: string;
  stock_value_basis: string;
  companies: TimelineCell[];
}

export interface TimelineResponse {
  as_of: string;
  start_year: number;
  end_year: number;
  completed_years: number;
  filing_estimate_window_years: number;
  latest_common_period: string | null;
  periods: TimelinePeriod[];
}

export type UiEvent =
  | EventEnvelope<"run.started", RunStartedData>
  | EventEnvelope<"workflow.progress", WorkflowProgressData>
  | EventEnvelope<"clarification.required", ClarificationRequiredData>
  | EventEnvelope<"answer.completed", AnswerCompletedData>
  | EventEnvelope<"run.failed", RunFailedData>;

export interface TurnRequest {
  conversation_id: string | null;
  client_request_id: string;
  message: string;
  context?: TurnContext;
}
