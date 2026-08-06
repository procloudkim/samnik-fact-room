import type {
  EvidenceSource,
  FactCheckResult,
  FactRow,
  ProgressStage,
  Verdict,
} from "../types";
import type { ChatState, ProgressEntry } from "../state/chatReducer";

const VERDICT_COPY: Record<Verdict, { label: string; tone: string }> = {
  CONFIRMED: { label: "공시에서 확인", tone: "confirmed" },
  CONTRADICTED: { label: "공시와 다른 내용", tone: "contradicted" },
  REVIEW_REQUIRED: { label: "근거 확인 · 종합 판단 필요", tone: "partial" },
  INSUFFICIENT_EVIDENCE: { label: "범위 내 직접 근거 없음", tone: "no-evidence" },
  SYSTEM_UNAVAILABLE: { label: "잠시 후 다시 확인", tone: "system-error" },
  OUT_OF_SCOPE: { label: "실적 질문으로 바꿔 보기", tone: "out-of-scope" },
};

const STAGE_SHORT_LABEL: Record<ProgressStage, string> = {
  SUPERVISOR_ROUTING: "질문 분류",
  SCENARIO_SLOT_CHECK: "조건 확인",
  SQL_RETRIEVAL: "재무 수치 조회",
  VECTOR_RETRIEVAL: "공시 본문 검색",
  CONTEXT_ANALYSIS: "맥락 종합",
  EXPLANATION_SYNTHESIS: "쉬운 해설",
  EVIDENCE_VALIDATION: "근거 검증",
  SUPERVISOR_FINALIZING: "최종 답변",
};

function formatAsOf(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;

  return `${new Intl.DateTimeFormat("ko-KR", {
    year: "numeric",
    month: "long",
    day: "numeric",
    timeZone: "Asia/Seoul",
  }).format(parsed)} 기준`;
}

interface EvidencePanelProps {
  state: ChatState;
  onRetry: () => void;
  onUsePrompt: (prompt: string) => void;
}

function ProgressTimeline({
  progress,
  isStreaming,
}: {
  progress: ProgressEntry[];
  isStreaming: boolean;
}) {
  if (progress.length === 0 && !isStreaming) return null;

  return (
    <section className="workflow" aria-labelledby="workflow-title">
      <div className="workflow-heading">
        <h3 id="workflow-title">검증 경로</h3>
        <span>{isStreaming ? "진행 중" : "경로 기록"}</span>
      </div>
      <ol className="workflow-list">
        {progress.map((entry, index) => {
          const active = isStreaming && index === progress.length - 1;
          return (
            <li className={active ? "is-active" : "is-complete"} key={entry.stage}>
              <span className="workflow-marker" aria-hidden="true">
                {active ? "" : "✓"}
              </span>
              <div>
                <strong>{STAGE_SHORT_LABEL[entry.stage]}</strong>
                <p>{entry.label}</p>
              </div>
            </li>
          );
        })}
        {isStreaming && progress.length === 0 ? (
          <li className="is-active">
            <span className="workflow-marker" aria-hidden="true" />
            <div>
              <strong>요청 접수</strong>
              <p>검증 Workflow를 시작하고 있습니다</p>
            </div>
          </li>
        ) : null}
      </ol>
    </section>
  );
}

function sourceIndexById(evidence: EvidenceSource[]): Map<string, number> {
  const index = new Map<string, number>();
  evidence.forEach((source, sourceIndex) => {
    if (source.evidence_id) index.set(source.evidence_id, sourceIndex + 1);
  });
  return index;
}

function InlineCitations({
  fact,
  evidence,
}: {
  fact: FactRow;
  evidence: EvidenceSource[];
}) {
  const indexes = sourceIndexById(evidence);
  const cited = (fact.evidence_ids ?? [])
    .map((id) => indexes.get(id))
    .filter((index): index is number => index !== undefined);
  const visible = cited.length > 0 ? cited : evidence.length === 1 ? [1] : [];

  return visible.map((index) => (
    <a
      className="citation-marker"
      href={`#source-${index}`}
      aria-label={`근거 ${index}로 이동`}
      key={index}
    >
      [{index}]
    </a>
  ));
}

function FactsTable({ result }: { result: FactCheckResult }) {
  if (result.facts.length === 0) return null;

  return (
    <section className="facts-section" aria-labelledby="facts-title">
      <div className="section-rule-heading">
        <h3 id="facts-title">
          {result.kind === "comparison" ? "동일 기준 비교" : "확인된 공시 수치"}
        </h3>
        {result.kind === "comparison" && result.difference_display ? (
          <span>{result.difference_display}</span>
        ) : null}
      </div>
      <div className="table-scroll" tabIndex={0} aria-label="재무 수치 표, 가로 스크롤 가능">
        <table>
          <caption className="sr-only">
            회사·지표·값·기간·재무 기준과 연결된 공시 근거
          </caption>
          <thead>
            <tr>
              <th scope="col">회사</th>
              <th scope="col">지표</th>
              <th scope="col">공시 수치</th>
              <th scope="col">기간</th>
              <th scope="col">기준</th>
            </tr>
          </thead>
          <tbody>
            {result.facts.map((fact, index) => (
              <tr key={`${fact.company}-${fact.metric}-${index}`}>
                <th scope="row">{fact.company_label ?? fact.company}</th>
                <td>{fact.metric_label ?? fact.metric}</td>
                <td className="numeric-cell">
                  <strong>{fact.value_trillion_krw_display ?? fact.value_display}</strong>{" "}
                  <InlineCitations fact={fact} evidence={result.evidence} />
                  {fact.value_trillion_krw_display ? (
                    <small>원 단위 {fact.raw_value_display ?? fact.value_display}</small>
                  ) : null}
                </td>
                <td>{fact.period_label ?? fact.period}</td>
                <td>
                  {fact.period_type_label && fact.fs_div_label && fact.value_scope_label
                    ? `${fact.period_type_label}·${fact.fs_div_label}·${fact.value_scope_label}`
                    : fact.basis}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function AnalysisReport({ result }: { result: FactCheckResult }) {
  const report = result.analysis_report;
  if (!report) return null;
  const indexes = sourceIndexById(result.evidence);

  return (
    <section className="analysis-report" aria-labelledby="analysis-report-title">
      <div className="section-rule-heading">
        <h3 id="analysis-report-title">
          {report.methodology === "FINANCIAL_RATIO_CALCULATION_V1"
            ? "숫자가 뜻하는 것"
            : "공시가 말해 주는 것"}
        </h3>
        <span>{report.findings.length}개 분석 영역</span>
      </div>
      <ol>
        {report.findings.map((finding, index) => {
          const citations = finding.evidence_ids
            .map((evidenceId) => indexes.get(evidenceId))
            .filter((sourceIndex): sourceIndex is number => sourceIndex !== undefined);
          return (
            <li key={finding.category_key}>
              <span>{String(index + 1).padStart(2, "0")}</span>
              <div>
                <strong>{finding.category_label}</strong>
                <p className="analysis-fact">
                  <span>확인된 사실</span>
                  {finding.summary}
                </p>
                {finding.plain_language ? (
                  <p className="analysis-meaning">
                    <span>쉽게 말하면</span>
                    {finding.plain_language}
                  </p>
                ) : null}
                {finding.next_check ? (
                  <p className="analysis-next">
                    <span>다음에 볼 것</span>
                    {finding.next_check}
                  </p>
                ) : null}
                {citations.map((sourceIndex) => (
                  <a
                    className="citation-marker"
                    href={`#source-${sourceIndex}`}
                    aria-label={`${finding.category_label} 근거 ${sourceIndex}로 이동`}
                    key={sourceIndex}
                  >
                    [{sourceIndex}]
                  </a>
                ))}
              </div>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

function Sources({
  evidence,
  totalCount,
  collapsed = false,
}: {
  evidence: EvidenceSource[];
  totalCount?: number;
  collapsed?: boolean;
}) {
  if (evidence.length === 0) return null;

  const confirmedCount = Math.max(totalCount ?? evidence.length, evidence.length);

  return (
    <details className="sources" open={!collapsed}>
      <summary className="section-rule-heading">
        <h3 id="sources-title">핵심 공시 근거</h3>
        <span>
          {confirmedCount > evidence.length
            ? `${evidence.length}건 표시 · 총 ${confirmedCount}건 확인`
            : `${evidence.length}건`}
        </span>
      </summary>
      <ol>
        {evidence.map((source, index) => (
          <li id={`source-${index + 1}`} key={source.evidence_id ?? source.rcept_no}>
            <div className="source-number" aria-hidden="true">
              {String(index + 1).padStart(2, "0")}
            </div>
            <div className="source-body">
              <div className="source-title-row">
                <strong>{source.report_name}</strong>
                <a href={source.source_url} target="_blank" rel="noreferrer">
                  원문 열기<span className="sr-only">, 새 창</span>
                </a>
              </div>
              <dl>
                <div>
                  <dt>접수번호</dt>
                  <dd>{source.rcept_no}</dd>
                </div>
                {source.section ? (
                  <div>
                    <dt>근거 위치</dt>
                    <dd>{source.section}</dd>
                  </div>
                ) : null}
                {source.rcept_dt ? (
                  <div>
                    <dt>접수일</dt>
                    <dd>{source.rcept_dt}</dd>
                  </div>
                ) : null}
              </dl>
              {source.excerpt ? (
                <blockquote>
                  {source.excerpt}
                  {source.excerpt_truncated ? (
                    <small>긴 원문 중 핵심 구간만 표시했습니다.</small>
                  ) : null}
                </blockquote>
              ) : null}
            </div>
          </li>
        ))}
      </ol>
    </details>
  );
}

function TraceSummary({ result }: { result: FactCheckResult }) {
  const trace = result.trace_summary;
  if (!trace) return null;

  return (
    <details className="trace-summary">
      <summary>모델 호출 기록</summary>
      <dl>
        {trace.route ? (
          <div>
            <dt>route</dt>
            <dd>{trace.route}</dd>
          </div>
        ) : null}
        {trace.agent_calls ? (
          <div>
            <dt>Agent</dt>
            <dd>{trace.agent_calls.join(" → ")}</dd>
          </div>
        ) : null}
        {trace.final_actor ? (
          <div>
            <dt>최종 답변</dt>
            <dd>{trace.final_actor}</dd>
          </div>
        ) : null}
        <div>
          <dt>MLAPI</dt>
          <dd>{trace.elice_mlapi_called ? "호출됨" : "호출 기록 없음"}</dd>
        </div>
        <div>
          <dt>Vector</dt>
          <dd>{trace.vector_search_called ? "검색됨" : "미호출"}</dd>
        </div>
      </dl>
      {trace.state_transitions?.length ? (
        <ol aria-label="상태 전이">
          {trace.state_transitions.map((transition) => (
            <li key={`${transition.sequence}-${transition.event}`}>
              {transition.sequence}. {transition.actor} · {transition.event}
            </li>
          ))}
        </ol>
      ) : null}
    </details>
  );
}

export function ResultCard({
  result,
  onRetry,
  onUsePrompt,
}: {
  result: FactCheckResult;
  onRetry: () => void;
  onUsePrompt: (prompt: string) => void;
}) {
  const verdict = VERDICT_COPY[result.verdict] ?? VERDICT_COPY.SYSTEM_UNAVAILABLE;
  const isNoEvidence = result.kind === "no_evidence";
  const isOutOfScope = result.kind === "out_of_scope";
  const isSystemError = result.kind === "system_error";

  if (isSystemError) {
    return (
      <article className="state-card state-card--error" aria-labelledby="system-error-title">
        <p className="state-kicker">잠시 문제가 생겼습니다</p>
        <h3 id="system-error-title">{result.title}</h3>
        <p>{result.summary}</p>
        {result.retryable ? (
          <button className="secondary-button" type="button" onClick={onRetry}>
            같은 질문 다시 확인
          </button>
        ) : null}
      </article>
    );
  }

  if (isOutOfScope) {
    return (
      <article className="state-card state-card--scope" aria-labelledby="scope-result-title">
        <p className="state-kicker">이 질문은 답하기 어렵습니다</p>
        <h3 id="scope-result-title">{result.title}</h3>
        <p>{result.summary}</p>
        {result.suggested_questions?.length ? (
          <div className="suggested-questions" aria-label="대신 확인할 수 있는 질문">
            {result.suggested_questions.map((question) => (
              <button type="button" key={question} onClick={() => onUsePrompt(question)}>
                {question}
              </button>
            ))}
          </div>
        ) : null}
      </article>
    );
  }

  return (
    <article className={`verdict-card verdict-card--${verdict.tone}`} aria-labelledby="verdict-title">
      <header className="verdict-header">
        <div>
          <p className="verdict-label">{verdict.label}</p>
          <h3 id="verdict-title">{result.title}</h3>
        </div>
        <div className="as-of">
          <span>공시 기준일</span>
          <strong>{formatAsOf(result.as_of)}</strong>
        </div>
      </header>

      {result.data_status === "STALE_CACHE" ? (
        <p className="freshness-warning" role="status">
          최신 동기화에 실패해 표시된 기준일의 저장 데이터로 확인했습니다.
        </p>
      ) : null}

      <div className="verdict-summary">
        <span aria-hidden="true">“</span>
        <p>{result.summary}</p>
      </div>

      {isNoEvidence ? (
        <section className="no-evidence-notice" aria-labelledby="no-evidence-title">
          <h4 id="no-evidence-title">근거 미확인은 거짓 판정이 아닙니다</h4>
          <p>
            {result.searched_scope ??
              "표시된 기준일과 적재된 OpenDART 공시 범위에서는 직접 근거를 찾지 못했습니다."}
          </p>
        </section>
      ) : null}

      <FactsTable result={result} />

      <AnalysisReport result={result} />

      {result.interpretation ? (
        <section className="interpretation" aria-labelledby="interpretation-title">
          <h3 id="interpretation-title">해석</h3>
          <p>{result.interpretation}</p>
        </section>
      ) : null}

      {result.interpretation_notes?.length ? (
        <section className="limitations" aria-labelledby="interpretation-notes-title">
          <h3 id="interpretation-notes-title">해석상 유의점</h3>
          <ul>
            {result.interpretation_notes.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        </section>
      ) : null}

      <Sources
        evidence={result.evidence}
        totalCount={result.evidence_total_count}
        collapsed={result.kind === "narrative_evidence"}
      />

      {result.limitations?.length ? (
        <section className="limitations" aria-labelledby="limitations-title">
          <h3 id="limitations-title">확인 한계</h3>
          <ul>
            {result.limitations.map((limitation) => (
              <li key={limitation}>{limitation}</li>
            ))}
          </ul>
        </section>
      ) : null}

      <TraceSummary result={result} />
    </article>
  );
}

export function EvidencePanel({ state, onRetry, onUsePrompt }: EvidencePanelProps) {
  const isStreaming = state.phase === "streaming";

  return (
    <section className="evidence-panel" aria-labelledby="evidence-title" aria-busy={isStreaming}>
      <div className="panel-heading evidence-heading">
        <div>
          <p className="eyebrow">Evidence ledger</p>
          <h2 id="evidence-title">공시 판정 기록</h2>
        </div>
        <span className="corpus-mark">OpenDART</span>
      </div>

      <ProgressTimeline progress={state.progress} isStreaming={isStreaming} />

      {state.phase === "idle" ? (
        <div className="empty-ledger">
          <p className="folio-number">01</p>
          <h3>판정은 근거가 준비된 뒤 한 번에 표시됩니다.</h3>
          <p>
            진행 중에는 Agent 호출 단계만 보여주고, 검증되지 않은 수치나 생성 중인
            문장은 노출하지 않습니다.
          </p>
          <ol>
            <li>
              <span>01</span>
              <div>
                <strong>질문 조건</strong>
                <p>회사·기간·지표를 같은 비교 기준으로 정리합니다.</p>
              </div>
            </li>
            <li>
              <span>02</span>
              <div>
                <strong>공시 근거</strong>
                <p>SQL 수치와 공시 본문을 원 접수번호에 연결합니다.</p>
              </div>
            </li>
            <li>
              <span>03</span>
              <div>
                <strong>최종 판정</strong>
                <p>Supervisor가 근거·기준일·한계를 함께 전달합니다.</p>
              </div>
            </li>
          </ol>
        </div>
      ) : null}

      {isStreaming ? (
        <div className="pending-ledger" role="status">
          <span className="pending-line" aria-hidden="true" />
          <div>
            <strong>공시 근거를 대조하고 있습니다</strong>
            <p>검증 완료 전에는 판정과 수치를 표시하지 않습니다.</p>
          </div>
        </div>
      ) : null}

      {state.phase === "awaiting_user" ? (
        <div className="clarification-card">
          <p className="state-kicker">Awaiting context</p>
          <h3>비교 조건 한 가지를 더 확인해 주세요.</h3>
          <p>왼쪽 대화의 Supervisor 질문에 답하면 같은 대화를 이어서 조회합니다.</p>
          {state.missingSlots.length ? (
            <div className="missing-slots">
              {state.missingSlots.map((slot) => (
                <span className="missing-chip" key={slot}>
                  {slot.replaceAll("_", " ")}
                </span>
              ))}
            </div>
          ) : null}
        </div>
      ) : null}

      {state.phase === "error" && state.error ? (
        <article className="state-card state-card--error" role="alert">
          <p className="state-kicker">Request interrupted</p>
          <h3>검증을 완료하지 못했습니다.</h3>
          <p>{state.error.message}</p>
          <code>{state.error.code}</code>
          {state.error.retryable ? (
            <button className="secondary-button" type="button" onClick={onRetry}>
              같은 질문 다시 확인
            </button>
          ) : null}
        </article>
      ) : null}

      {state.phase === "cancelled" ? (
        <article className="state-card state-card--cancelled">
          <p className="state-kicker">Request cancelled</p>
          <h3>사용자가 검증을 중단했습니다.</h3>
          <p>부분 결과는 판정으로 사용하지 않았습니다.</p>
          <button className="secondary-button" type="button" onClick={onRetry}>
            같은 질문 다시 확인
          </button>
        </article>
      ) : null}

      {state.phase === "complete" && state.result ? (
        <ResultCard result={state.result} onRetry={onRetry} onUsePrompt={onUsePrompt} />
      ) : null}
    </section>
  );
}
