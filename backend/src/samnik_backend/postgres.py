from __future__ import annotations

import json
import re
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from typing import AsyncIterator, Sequence

import asyncpg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from .domain import (
    Company,
    ConversationRecord,
    Evidence,
    EvidenceStatus,
    EvidenceHit,
    EvidenceSearchResult,
    FinancialFact,
    FsDiv,
    Metric,
    ValueScope,
)
from .errors import RepositoryUnavailable
from .formatting import period_sort_key


CORP_CODES = {
    Company.SAMSUNG_ELECTRONICS: "00126380",
    Company.SK_HYNIX: "00164779",
}
COMPANIES_BY_CORP_CODE = {value: key for key, value in CORP_CODES.items()}
REPORT_CODE_SUFFIX = {"11011": "", "11013": "Q1", "11012": "H1", "11014": "Q3"}
PERIOD_RE = re.compile(r"^(?P<year>20\d{2})(?P<suffix>Q1|H1|Q3)?$")
CANONICAL_EMBEDDING_MODEL = "intfloat/multilingual-e5-small"
POINT_METRICS = frozenset({Metric.TOTAL_ASSETS, Metric.TOTAL_LIABILITIES, Metric.TOTAL_EQUITY})


def _vector_literal(values: Sequence[float]) -> str:
    return "[" + ",".join(f"{value:.10f}" for value in values) + "]"


def _parse_period(period: str) -> tuple[int, str]:
    match = PERIOD_RE.fullmatch(period)
    if not match:
        raise ValueError(f"unsupported M0 period key: {period}")
    report_codes = {"": "11011", "Q1": "11013", "H1": "11012", "Q3": "11014"}
    return int(match.group("year")), report_codes[match.group("suffix") or ""]


def _period_key(business_year: int, report_code: str) -> str:
    return f"{business_year}{REPORT_CODE_SUFFIX.get(report_code, ':' + report_code)}"


def _domain_scope(metric: Metric, requested_value_scope: ValueScope) -> ValueScope:
    if metric in POINT_METRICS:
        return ValueScope.POINT_IN_TIME
    return requested_value_scope


def _preferred_statement_rank_sql(metric_param: str, statement_param: str) -> str:
    return f"""
        CASE
            WHEN {metric_param} IN ('total_assets', 'total_liabilities', 'total_equity')
                THEN CASE WHEN {statement_param} = 'BS' THEN 0 ELSE 1 END
            WHEN {metric_param} = 'operating_cash_flow'
                THEN CASE WHEN {statement_param} = 'CF' THEN 0 ELSE 1 END
            WHEN {metric_param} IN ('revenue', 'operating_profit', 'net_income')
                THEN CASE
                    WHEN {statement_param} = 'IS' THEN 0
                    WHEN {statement_param} = 'CIS' THEN 1
                    ELSE 2
                END
            ELSE 99
        END
    """


def _amount_expression(value_scope_param: str, *, raw: bool = False) -> str:
    suffix = "_raw" if raw else ""
    return f"""
        CASE
            WHEN {value_scope_param} IN ('YTD', 'CUMULATIVE')
                 AND fsl.thstrm_add_amount IS NOT NULL
                THEN fsl.thstrm_add_amount{suffix}
            WHEN {value_scope_param} IN ('POINT_IN_TIME', 'QUARTER_ONLY', 'ANNUAL')
                THEN fsl.thstrm_amount{suffix}
            ELSE fsl.thstrm_amount{suffix}
        END
    """


def _fact_from_row(
    row: asyncpg.Record,
    *,
    requested_value_scope: ValueScope,
) -> FinancialFact:
    metric = Metric(row["metric_key"])
    evidence_id = row.get("row_hash")
    if evidence_id is None:
        evidence_id = row.get("evidence_id")
    if evidence_id is None:
        raise KeyError("financial_statement_line row is missing evidence identifier")
    return FinancialFact(
        company=COMPANIES_BY_CORP_CODE[row["corp_code"].strip()],
        metric=metric,
        value=Decimal(row["amount_numeric"]),
        currency=row["currency"].strip(),
        unit="KRW",
        period=_period_key(row["business_year"], row["reprt_code"].strip()),
        value_scope=_domain_scope(metric, requested_value_scope),
        fs_div=FsDiv(row["fs_div"]),
        evidence_id=evidence_id.strip(),
        source_raw_amount=row.get("raw_amount"),
        source_unit_scale=1,
        period_start=row.get("period_start"),
        period_end=row.get("period_end"),
        report_code=row.get("reprt_code"),
        section=row.get("sj_div"),
    )


def _evidence_from_row(row: asyncpg.Record) -> Evidence:
    return Evidence(
        evidence_id=row["evidence_id"],
        company=COMPANIES_BY_CORP_CODE[row["corp_code"].strip()],
        business_year=row["business_year"],
        rcept_no=row["rcept_no"].strip(),
        filing_title=row["report_name"],
        filed_at=row["filing_date"],
        url=row["source_url"],
        excerpt=row["excerpt"],
        section_path=row["section_path"],
        source_kind=row["source_kind"],
        evidence_status=EvidenceStatus.OFFICIAL_API_VERIFIED,
    )


class PostgresFinancialRepository:
    """Parameterized queries against the canonical infra/migrations schema."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def get_fact(
        self,
        *,
        company: Company,
        metric: Metric,
        period: str,
        value_scope: ValueScope,
        fs_div: FsDiv,
        as_of: datetime,
    ) -> FinancialFact | None:
        try:
            business_year, report_code = _parse_period(period)
        except ValueError:
            return None
        sql = f"""
            WITH candidates AS (
                SELECT
                    fsl.*, f.period_start, f.period_end, f.report_name, f.source_url,
                    fr.filing_date, fr.selected_latest, si.acquired_at,
                    alias.metric_key,
                    {_amount_expression("$7")} AS amount_numeric,
                    {_amount_expression("$7", raw=True)} AS raw_amount,
                    {_preferred_statement_rank_sql("$2", "fsl.sj_div")} AS statement_rank
                FROM financial_statement_line fsl
                JOIN filing_revision fr ON fr.filing_revision_id = fsl.filing_revision_id
                JOIN snapshot_import si ON si.snapshot_id = fsl.snapshot_id
                JOIN filing f ON f.snapshot_id = fsl.snapshot_id AND f.rcept_no = fsl.rcept_no
                LEFT JOIN LATERAL (
                    SELECT ma.metric_key, ma.metric_alias_id, ma.comparison_grade
                    FROM metric_alias ma
                    WHERE (ma.corp_code IS NULL OR ma.corp_code = fsl.corp_code)
                      AND fsl.business_year BETWEEN ma.valid_from_year AND COALESCE(ma.valid_to_year, 2100)
                      AND (ma.account_id = fsl.account_id OR ma.exact_account_nm = fsl.account_nm)
                    ORDER BY (ma.corp_code IS NOT NULL) DESC, ma.priority, ma.metric_alias_id
                    LIMIT 1
                ) alias ON true
                WHERE fsl.corp_code = $1 AND alias.metric_key = $2
                  AND fsl.business_year = $3 AND f.reprt_code = $4
                  AND fsl.fs_div = $5
                  AND fr.filing_date <= ($6::timestamptz)::date
                  AND si.acquired_at <= $6::timestamptz
                  AND si.evidence_status = 'OFFICIAL_API_VERIFIED'
                  AND f.evidence_status = 'OFFICIAL_API_VERIFIED'
            )
            SELECT *
            FROM candidates
            WHERE amount_numeric IS NOT NULL
            ORDER BY CASE WHEN selected_latest THEN 1 ELSE 0 END DESC,
                     filing_date DESC, rcept_no DESC, acquired_at DESC,
                     snapshot_id DESC, statement_rank ASC,
                     filing_revision_id DESC, financial_statement_line_id DESC
            LIMIT 1
        """
        try:
            async with self._pool.acquire() as connection:
                row = await connection.fetchrow(
                    sql,
                    CORP_CODES[company],
                    metric.value,
                    business_year,
                    report_code,
                    fs_div.value,
                    as_of,
                    value_scope.value,
                )
            fact = _fact_from_row(row, requested_value_scope=value_scope) if row else None
            return fact if fact is None or fact.value_scope == value_scope else None
        except (
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            OSError,
            KeyError,
            ValueError,
        ) as exc:
            raise RepositoryUnavailable("financial fact query failed") from exc

    async def get_latest_common_facts(
        self,
        *,
        companies: tuple[Company, Company],
        metric: Metric,
        value_scope: ValueScope,
        fs_div: FsDiv,
        as_of: datetime,
        interim_only: bool = False,
    ) -> tuple[FinancialFact, FinancialFact] | None:
        sql = f"""
            WITH candidates AS (
                SELECT
                    fsl.*, f.period_start, f.period_end, f.report_name, f.source_url,
                    fr.filing_date, fr.selected_latest, si.acquired_at,
                    alias.metric_key,
                    {_amount_expression("$6")} AS amount_numeric,
                    {_amount_expression("$6", raw=True)} AS raw_amount,
                    {_preferred_statement_rank_sql("$2", "fsl.sj_div")} AS statement_rank,
                    ROW_NUMBER() OVER (
                        PARTITION BY fsl.corp_code, fsl.business_year, fsl.reprt_code
                        ORDER BY
                            CASE WHEN fr.selected_latest THEN 1 ELSE 0 END DESC,
                            fr.filing_date DESC,
                            fr.rcept_no DESC,
                            si.acquired_at DESC,
                            si.snapshot_id DESC,
                            {_preferred_statement_rank_sql("$2", "fsl.sj_div")} ASC,
                            fr.filing_revision_id DESC,
                            fsl.financial_statement_line_id DESC
                    ) AS row_number
                FROM financial_statement_line fsl
                JOIN filing_revision fr ON fr.filing_revision_id = fsl.filing_revision_id
                JOIN snapshot_import si ON si.snapshot_id = fsl.snapshot_id
                JOIN filing f ON f.snapshot_id = fsl.snapshot_id AND f.rcept_no = fsl.rcept_no
                LEFT JOIN LATERAL (
                    SELECT ma.metric_key, ma.metric_alias_id, ma.comparison_grade
                    FROM metric_alias ma
                    WHERE (ma.corp_code IS NULL OR ma.corp_code = fsl.corp_code)
                      AND fsl.business_year BETWEEN ma.valid_from_year AND COALESCE(ma.valid_to_year, 2100)
                      AND (ma.account_id = fsl.account_id OR ma.exact_account_nm = fsl.account_nm)
                    ORDER BY (ma.corp_code IS NOT NULL) DESC, ma.priority, ma.metric_alias_id
                    LIMIT 1
                ) alias ON true
                WHERE fsl.corp_code = ANY($1::bpchar[]) AND alias.metric_key = $2
                  AND fsl.fs_div = $3
                  AND fr.filing_date <= ($4::timestamptz)::date
                  AND si.acquired_at <= $4::timestamptz
                  AND (NOT $5::boolean OR f.reprt_code <> '11011')
                  AND si.evidence_status = 'OFFICIAL_API_VERIFIED'
                  AND f.evidence_status = 'OFFICIAL_API_VERIFIED'
                  AND ({_amount_expression("$6")} IS NOT NULL)
            )
            SELECT *
            FROM candidates
            WHERE row_number = 1
            ORDER BY corp_code, business_year, reprt_code
        """
        try:
            async with self._pool.acquire() as connection:
                rows = await connection.fetch(
                    sql,
                    [CORP_CODES[company] for company in companies],
                    metric.value,
                    fs_div.value,
                    as_of,
                    interim_only,
                    value_scope.value,
                )
        except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError) as exc:
            raise RepositoryUnavailable("latest common period query failed") from exc

        grouped: dict[Company, dict[str, FinancialFact]] = {company: {} for company in companies}
        for row in rows:
            fact = _fact_from_row(row, requested_value_scope=value_scope)
            if fact.value_scope == value_scope:
                grouped[fact.company][fact.period] = fact
        common = set(grouped[companies[0]]) & set(grouped[companies[1]])
        if not common:
            return None
        period = max(common, key=period_sort_key)
        return grouped[companies[0]][period], grouped[companies[1]][period]


class PostgresEvidenceRepository:
    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        embedding_model: str = CANONICAL_EMBEDDING_MODEL,
    ) -> None:
        self._pool = pool
        self._embedding_model = embedding_model

    async def get_by_ids(self, evidence_ids: Sequence[str]) -> list[Evidence]:
        if not evidence_ids:
            return []
        sql = """
            WITH financial_evidence AS (
                SELECT DISTINCT ON (fsl.row_hash)
                       fsl.row_hash::text AS evidence_id, fsl.corp_code,
                       fsl.business_year, f.rcept_no, f.report_name,
                       fr.filing_date, f.source_url,
                       NULL::text AS excerpt, fsl.sj_div AS section_path,
                       'FINANCIAL_FACT'::text AS source_kind
                FROM financial_statement_line fsl
                JOIN filing_revision fr
                  ON fr.filing_revision_id = fsl.filing_revision_id
                JOIN filing f
                  ON f.snapshot_id = fsl.snapshot_id AND f.rcept_no = fsl.rcept_no
                JOIN snapshot_import si ON si.snapshot_id = fsl.snapshot_id
                WHERE fsl.row_hash = ANY($1::text[])
                  AND si.evidence_status = 'OFFICIAL_API_VERIFIED'
                  AND f.evidence_status = 'OFFICIAL_API_VERIFIED'
                ORDER BY fsl.row_hash,
                         CASE WHEN fr.selected_latest THEN 1 ELSE 0 END DESC,
                         fr.filing_date DESC, si.acquired_at DESC, si.snapshot_id DESC
            ), narrative_evidence AS (
                SELECT DISTINCT ON (sc.chunk_id)
                       sc.chunk_id::text AS evidence_id, sc.corp_code,
                       sc.business_year, f.rcept_no, f.report_name,
                       f.filing_date, sc.source_url,
                       sc.content AS excerpt, sc.section_path,
                       'FILING_CHUNK'::text AS source_kind
                FROM source_chunk sc
                JOIN filing f
                  ON f.rcept_no = sc.rcept_no AND f.snapshot_id = sc.snapshot_id
                JOIN snapshot_import si ON si.snapshot_id = sc.snapshot_id
                WHERE sc.chunk_id::text = ANY($1::text[])
                  AND si.evidence_status = 'OFFICIAL_API_VERIFIED'
                  AND sc.evidence_status = 'OFFICIAL_API_VERIFIED'
                  AND f.evidence_status = 'OFFICIAL_API_VERIFIED'
                ORDER BY sc.chunk_id, si.acquired_at DESC, si.snapshot_id DESC
            )
            SELECT * FROM financial_evidence
            UNION ALL
            SELECT * FROM narrative_evidence
        """
        try:
            async with self._pool.acquire() as connection:
                rows = await connection.fetch(sql, list(evidence_ids))
            return [_evidence_from_row(row) for row in rows]
        except (
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            OSError,
            KeyError,
            ValueError,
        ) as exc:
            raise RepositoryUnavailable("evidence lookup failed") from exc

    async def search(
        self,
        *,
        company: Company,
        period: str | None = None,
        query_text: str,
        embedding: Sequence[float],
        as_of: datetime,
        limit: int,
    ) -> EvidenceSearchResult:
        business_year: int | None = None
        report_code: str | None = None
        if period is not None:
            try:
                business_year, report_code = _parse_period(period)
            except ValueError:
                return EvidenceSearchResult(
                    hits=[],
                    searched_filing_ids=[],
                    corpus_scope=f"unsupported M0 period: {period}",
                    corpus_complete=False,
                    searched_until=as_of,
                )
        snapshot_sql = """
            SELECT si.snapshot_id, si.acquired_at,
                   si.narrative_corpus_status,
                   si.narrative_covered_files,
                   si.narrative_total_files,
                   f.rcept_no, f.business_year, f.reprt_code
            FROM snapshot_import si
            JOIN filing f ON f.snapshot_id = si.snapshot_id
            WHERE si.evidence_status = 'OFFICIAL_API_VERIFIED'
              AND si.acquired_at <= $1
              AND f.corp_code = $2
              AND f.evidence_status = 'OFFICIAL_API_VERIFIED'
              AND f.filing_date <= $1::date
              AND ($3::smallint IS NULL OR f.business_year = $3)
              AND ($4::bpchar IS NULL OR f.reprt_code = $4)
            ORDER BY
                CASE si.narrative_corpus_status
                    WHEN 'FULL_TEXT' THEN 0
                    WHEN 'PARTIAL_TEXT' THEN 1
                    ELSE 2
                END,
                si.acquired_at DESC,
                f.filing_date DESC,
                f.rcept_no DESC,
                si.snapshot_id DESC
            LIMIT 1
        """
        search_sql = """
            SELECT sc.chunk_id::text AS evidence_id, sc.corp_code,
                   sc.business_year, f.rcept_no,
                   f.report_name, f.filing_date, sc.source_url,
                   sc.content AS excerpt, sc.section_path,
                   'FILING_CHUNK'::text AS source_kind,
                   1 - (sc.embedding <=> $4::vector) AS score
            FROM source_chunk sc
            JOIN filing f ON f.rcept_no = sc.rcept_no AND f.snapshot_id = sc.snapshot_id
            WHERE sc.snapshot_id = $1 AND sc.corp_code = $2
              AND sc.rcept_no = $3 AND f.rcept_no = $3
              AND sc.evidence_status = 'OFFICIAL_API_VERIFIED'
              AND f.evidence_status = 'OFFICIAL_API_VERIFIED'
              AND f.filing_date <= $5::date
              AND sc.embedding_model = $6
            ORDER BY sc.embedding <=> $4::vector
            LIMIT $7
        """
        try:
            async with self._pool.acquire() as connection:
                snapshot = await connection.fetchrow(
                    snapshot_sql,
                    as_of,
                    CORP_CODES[company],
                    business_year,
                    report_code,
                )
                if not snapshot:
                    return EvidenceSearchResult(
                        hits=[],
                        searched_filing_ids=[],
                        corpus_scope="no OFFICIAL_API_VERIFIED snapshot",
                        corpus_complete=False,
                        searched_until=as_of,
                    )
                rows = await connection.fetch(
                    search_sql,
                    snapshot["snapshot_id"],
                    CORP_CODES[company],
                    snapshot["rcept_no"],
                    _vector_literal(embedding),
                    as_of,
                    self._embedding_model,
                    limit,
                )
        except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError) as exc:
            raise RepositoryUnavailable("exact pgvector evidence search failed") from exc
        hits = [
            EvidenceHit(
                evidence=_evidence_from_row(row),
                score=max(0.0, min(1.0, float(row["score"]))),
            )
            for row in rows
        ]
        corpus_complete = (
            snapshot["narrative_corpus_status"] == "FULL_TEXT"
            and snapshot["narrative_total_files"] > 0
            and snapshot["narrative_covered_files"]
            == snapshot["narrative_total_files"]
        )
        return EvidenceSearchResult(
            hits=hits,
            searched_filing_ids=[snapshot["rcept_no"].strip()],
            corpus_scope=(
                f"{company.value} {snapshot['business_year']} "
                f"{snapshot['reprt_code'].strip()} receipt={snapshot['rcept_no'].strip()} "
                f"coverage={snapshot['narrative_corpus_status']}"
            ),
            corpus_complete=corpus_complete,
            searched_until=min(as_of, snapshot["acquired_at"]),
        )


class PostgresConversationRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def get(self, conversation_id: str) -> ConversationRecord | None:
        try:
            async with self._pool.acquire() as connection:
                row = await connection.fetchrow(
                    "SELECT state FROM conversation_state WHERE conversation_id = $1",
                    conversation_id,
                )
            if not row:
                return None
            state = row["state"]
            if isinstance(state, str):
                state = json.loads(state)
            return ConversationRecord.model_validate(state)
        except (
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            OSError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
        ) as exc:
            raise RepositoryUnavailable("conversation state read failed") from exc

    async def put(self, record: ConversationRecord) -> None:
        try:
            async with self._pool.acquire() as connection:
                await connection.execute(
                    """
                    INSERT INTO conversation_state(conversation_id, state, updated_at)
                    VALUES ($1, $2::jsonb, now())
                    ON CONFLICT (conversation_id) DO UPDATE
                    SET state = EXCLUDED.state, updated_at = now()
                    """,
                    record.conversation_id,
                    record.model_dump_json(),
                )
        except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError) as exc:
            raise RepositoryUnavailable("conversation state write failed") from exc

    async def delete(self, conversation_id: str) -> None:
        try:
            async with self._pool.acquire() as connection:
                await connection.execute(
                    "DELETE FROM conversation_state WHERE conversation_id = $1",
                    conversation_id,
                )
        except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError) as exc:
            raise RepositoryUnavailable("conversation state delete failed") from exc


@asynccontextmanager
async def postgres_checkpointer(dsn: str) -> AsyncIterator[AsyncPostgresSaver]:
    """Initialize LangGraph's separate canonical Postgres checkpoint tables."""

    async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
        await saver.setup()
        yield saver
