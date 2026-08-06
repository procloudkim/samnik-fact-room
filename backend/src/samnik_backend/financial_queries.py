from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import asyncpg
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .domain import Company
from .errors import RepositoryUnavailable
from .financial_contract import (
    FinancialQueryMode,
    FinancialValueBasis,
    STANDARD_METRICS,
    SUPPORTED_FS_DIVS,
    SUPPORTED_REPORT_CODES,
)
from .formatting import format_korean_krw
from .postgres import CORP_CODES

SUPPORTED_YOY_START_YEAR = 2015
_PERIOD_ORDER = {"": 0, "Q1": 1, "H1": 2, "Q3": 3}


def _preferred_statement_rank(metric_key: str | None, statement: str) -> int:
    if metric_key in {"total_assets", "total_liabilities", "total_equity"}:
        if statement == "BS":
            return 0
        return 1
    if metric_key == "operating_cash_flow":
        if statement == "CF":
            return 0
        return 1
    if metric_key in {"revenue", "operating_profit", "net_income"}:
        if statement == "IS":
            return 0
        if statement == "CIS":
            return 1
        return 2
    return 99


def _canonical_group_key(
    row: asyncpg.Record,
    *,
    measure_metric: str | None,
) -> tuple[Any, ...]:
    if measure_metric is not None:
        return (
            row["corp_code"],
            row["business_year"],
            row["reprt_code"],
            row["fs_div"],
            row["metric_key"],
        )
    return (
        row["corp_code"],
        row["business_year"],
        row["reprt_code"],
        row["fs_div"],
        row["account_id"],
        row["account_nm"],
        row["account_detail"] or "",
        row["ord"],
    )


def _canonical_sort_key(
    row: asyncpg.Record,
    *,
    measure_metric: str | None,
) -> tuple[Any, ...]:
    statement_rank = (
        _preferred_statement_rank(measure_metric, row["sj_div"]) if measure_metric is not None else 0
    )
    return (
        1 if row["selected_latest"] else 0,
        row["filing_date"],
        row["rcept_no"],
        row["acquired_at"],
        row["snapshot_id"],
        -statement_rank,
        -int(row.get("metric_alias_priority") or 32767),
        row["filing_revision_id"],
        row["financial_statement_line_id"],
    )


def _select_canonical_rows(
    rows: list[asyncpg.Record],
    *,
    measure_metric: str | None,
) -> list[asyncpg.Record]:
    grouped: dict[tuple[Any, ...], list[asyncpg.Record]] = {}
    for row in rows:
        grouped.setdefault(_canonical_group_key(row, measure_metric=measure_metric), []).append(row)
    selected: list[asyncpg.Record] = []
    for items in grouped.values():
        selected.append(max(items, key=lambda row: _canonical_sort_key(row, measure_metric=measure_metric)))
    return selected


class FinancialMeasure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric_key: str | None = None
    account_id: str | None = None

    @model_validator(mode="after")
    def exactly_one_identifier(self) -> "FinancialMeasure":
        if (self.metric_key is None) == (self.account_id is None):
            raise ValueError("exactly one of metric_key or account_id is required")
        if self.metric_key is not None and self.metric_key not in STANDARD_METRICS:
            raise ValueError("unsupported standard metric")
        if self.metric_key is None and self.account_id is not None and not self.account_id:
            raise ValueError("account_id must be non-empty")
        if self.metric_key is not None and not self.metric_key:
            raise ValueError("metric_key must be non-empty")
        return self


def _parse_period_rank(period: str) -> tuple[int, int]:
    year = int(period[:4])
    suffix = period[4:]
    return year, _PERIOD_ORDER[suffix]


class FinancialQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: FinancialQueryMode
    companies: list[Company] = Field(min_length=1, max_length=2)
    measures: list[FinancialMeasure] = Field(min_length=1, max_length=20)
    start_period: str = Field(pattern=r"^20\d{2}(?:Q1|H1|Q3)?$")
    end_period: str = Field(pattern=r"^20\d{2}(?:Q1|H1|Q3)?$")
    report_codes: list[str] = Field(default_factory=lambda: ["11011"])
    fs_divs: list[str] = Field(default_factory=lambda: ["CFS"])
    value_basis: FinancialValueBasis
    as_of: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_scope(self) -> "FinancialQueryRequest":
        if len(self.companies) != len(set(self.companies)):
            raise ValueError("companies must be unique")
        if not self.companies:
            raise ValueError("at least one company is required")
        if len(self.companies) > 2:
            raise ValueError("at most two companies are supported")
        if not set(self.report_codes).issubset(SUPPORTED_REPORT_CODES):
            raise ValueError("unsupported report code")
        if not self.report_codes:
            raise ValueError("at least one report code is required")
        if not set(self.fs_divs).issubset(SUPPORTED_FS_DIVS):
            raise ValueError("unsupported financial statement division")
        if not self.fs_divs:
            raise ValueError("at least one statement division is required")
        if self.mode == FinancialQueryMode.COMPARE and len(self.companies) != 2:
            raise ValueError("COMPARE requires exactly two companies")
        if self.mode == FinancialQueryMode.CFS_OFS_DIFF:
            if len(self.companies) != 1:
                raise ValueError("CFS_OFS_DIFF requires one company")
            if set(self.fs_divs) != SUPPORTED_FS_DIVS:
                raise ValueError("CFS_OFS_DIFF requires both CFS and OFS")
        if self.mode == FinancialQueryMode.STATEMENT and len(self.measures) != 1:
            raise ValueError("STATEMENT requires exactly one measure")
        if self.value_basis not in {
            FinancialValueBasis.POINT_IN_TIME,
            FinancialValueBasis.QUARTER_ONLY,
            FinancialValueBasis.YTD,
            FinancialValueBasis.ANNUAL,
        }:
            raise ValueError("unsupported value basis")
        start_year, start_suffix_rank = _parse_period_rank(self.start_period)
        end_year, end_suffix_rank = _parse_period_rank(self.end_period)
        if start_year < SUPPORTED_YOY_START_YEAR or end_year < SUPPORTED_YOY_START_YEAR:
            raise ValueError("periods before 2015 are not supported")
        if (start_year, start_suffix_rank) > (end_year, end_suffix_rank):
            raise ValueError("start_period must not be after end_period")
        return self


def _year(period: str) -> int:
    return int(period[:4])


def _period_key(year: int, report_code: str) -> str:
    return f"{year}{ {'11011': '', '11013': 'Q1', '11012': 'H1', '11014': 'Q3'}[report_code] }"


def _row_dict(row: asyncpg.Record) -> dict[str, Any]:
    value = Decimal(row["amount_numeric"])
    return {
        "company": row["company"],
        "company_label": row["company_label"],
        "business_year": row["business_year"],
        "period": _period_key(row["business_year"], row["reprt_code"].strip()),
        "report_code": row["reprt_code"].strip(),
        "fs_div": row["fs_div"],
        "value_basis": row["value_basis"],
        "metric_key": row["metric_key"],
        "comparison_grade": row["comparison_grade"],
        "account_id": row["account_id"],
        "account_nm": row["account_nm"],
        "account_detail": row["account_detail"],
        "statement": row["sj_div"],
        "source_field": row["source_field"],
        "raw_amount": row["raw_amount"],
        "value": format(value, "f"),
        "value_display": format_korean_krw(value),
        "currency": row["currency"].strip(),
        "rcept_no": row["rcept_no"].strip(),
        "filing_date": row["filing_date"].isoformat(),
        "correction_kind": row["correction_kind"],
        "source_url": row["source_url"],
        "section": row["sj_div"],
        "row_hash": row["row_hash"].strip(),
    }


class PostgresFinancialQueryRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @staticmethod
    def _expected_coverage_keys(
        *,
        companies: list[Company],
        start_year: int,
        end_year: int,
        report_codes: list[str],
        fs_divs: list[str],
    ) -> set[tuple[str, int, str, str]]:
        keys: set[tuple[str, int, str, str]] = set()
        for company in companies:
            for year in range(start_year, end_year + 1):
                for report_code in report_codes:
                    for fs_div in fs_divs:
                        keys.add((company.value, year, report_code, fs_div))
        return keys

    async def _rows(
        self,
        request: FinancialQueryRequest,
        measure: FinancialMeasure,
        *,
        statement_only: bool = False,
    ) -> list[dict[str, Any]]:
        sql = """
            WITH candidates AS (
                SELECT fsl.*, fr.filing_date, fr.correction_kind, fr.source_url, fr.selected_latest,
                       c.corp_name AS company_label,
                       CASE fsl.corp_code
                           WHEN '00126380' THEN 'SAMSUNG_ELECTRONICS'
                           WHEN '00164779' THEN 'SK_HYNIX'
                       END AS company,
                       alias.metric_key, alias.comparison_grade,
                       alias.metric_alias_priority,
                       CASE
                           WHEN $8 = 'POINT_IN_TIME' THEN fsl.thstrm_amount
                           WHEN $8 = 'QUARTER_ONLY' THEN fsl.thstrm_amount
                           WHEN $8 = 'YTD' THEN COALESCE(fsl.thstrm_add_amount, fsl.thstrm_amount)
                           WHEN $8 = 'ANNUAL' THEN fsl.thstrm_amount
                       END AS amount_numeric,
                       CASE
                           WHEN $8 = 'POINT_IN_TIME' THEN fsl.thstrm_amount_raw
                           WHEN $8 = 'QUARTER_ONLY' THEN fsl.thstrm_amount_raw
                           WHEN $8 = 'YTD' AND fsl.thstrm_add_amount IS NOT NULL THEN fsl.thstrm_add_amount_raw
                           ELSE fsl.thstrm_amount_raw
                       END AS raw_amount,
                       CASE
                           WHEN $8 = 'YTD' AND fsl.thstrm_add_amount IS NOT NULL THEN 'thstrm_add_amount'
                           ELSE 'thstrm_amount'
                       END AS source_field,
                       $8::text AS value_basis,
                       si.acquired_at
                FROM financial_statement_line fsl
                JOIN filing_revision fr ON fr.filing_revision_id = fsl.filing_revision_id
                JOIN snapshot_import si ON si.snapshot_id = fsl.snapshot_id
                JOIN company c ON c.corp_code = fsl.corp_code
                LEFT JOIN LATERAL (
                    SELECT ma.metric_key, ma.comparison_grade,
                           ma.priority AS metric_alias_priority
                    FROM metric_alias ma
                    WHERE (ma.corp_code IS NULL OR ma.corp_code = fsl.corp_code)
                      AND fsl.business_year BETWEEN ma.valid_from_year AND COALESCE(ma.valid_to_year, 2100)
                      AND (ma.account_id = fsl.account_id OR ma.exact_account_nm = fsl.account_nm)
                    ORDER BY (ma.corp_code IS NOT NULL) DESC, ma.priority, ma.metric_alias_id
                    LIMIT 1
                ) alias ON true
                WHERE fsl.corp_code = ANY($1::bpchar[])
                  AND fsl.business_year BETWEEN $2 AND $3
                  AND fsl.reprt_code = ANY($4::bpchar[])
                  AND fsl.fs_div = ANY($5::text[])
                  AND fr.filing_date <= ($6::timestamptz)::date
                  AND si.acquired_at <= $6::timestamptz
                  AND ($10::boolean OR $7::text IS NULL OR alias.metric_key = $7)
                  AND ($10::boolean OR $9::text IS NULL OR fsl.account_id = $9)
                  AND ($10::boolean OR fsl.sj_div <> 'BS' OR $8 = 'POINT_IN_TIME')
                  AND ($10::boolean OR fsl.sj_div = 'BS' OR $8 <> 'POINT_IN_TIME')
                  AND ($8 <> 'ANNUAL' OR fsl.reprt_code = '11011')
            )
            SELECT *
            FROM candidates
            WHERE amount_numeric IS NOT NULL
        """
        args = (
            [CORP_CODES[item] for item in request.companies],
            _year(request.start_period),
            _year(request.end_period),
            request.report_codes,
            request.fs_divs,
            request.as_of,
            measure.metric_key,
            request.value_basis.value,
            measure.account_id,
            statement_only,
        )
        try:
            async with self._pool.acquire() as connection:
                rows = await connection.fetch(sql, *args)
            canonical_rows = _select_canonical_rows(
                rows,
                measure_metric=None if statement_only else measure.metric_key,
            )
            return [_row_dict(row) for row in canonical_rows]
        except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, KeyError, ValueError) as exc:
            raise RepositoryUnavailable("financial statement query failed") from exc

    async def get_fact(self, request: FinancialQueryRequest) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for measure in request.measures:
            rows.extend(await self._rows(request, measure))
        return rows

    async def get_time_series(self, request: FinancialQueryRequest) -> list[dict[str, Any]]:
        return await self.get_fact(request)

    async def compare_companies(self, request: FinancialQueryRequest) -> list[dict[str, Any]]:
        return await self.get_fact(request)

    async def calculate_yoy(self, request: FinancialQueryRequest) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        rows = await self.get_fact(request)
        grouped: dict[tuple[str, str, str, str, str, str], list[dict[str, Any]]] = {}
        for row in rows:
            key = (
                row["company"], row["account_id"], row["report_code"], row["fs_div"],
                row["value_basis"], row["currency"],
            )
            grouped.setdefault(key, []).append(row)
        calculations: list[dict[str, Any]] = []
        for values in grouped.values():
            values.sort(key=lambda item: item["business_year"])
            for previous, current in zip(values, values[1:]):
                if current["business_year"] - previous["business_year"] != 1:
                    continue
                previous_value = Decimal(previous["value"])
                current_value = Decimal(current["value"])
                change = current_value - previous_value
                if previous_value == 0:
                    rate = None
                    reason = "PRIOR_VALUE_ZERO"
                else:
                    rate = format(change / abs(previous_value) * Decimal(100), ".6f")
                    reason = None
                calculations.append(
                    {
                        "kind": "YOY",
                        "from_period": previous["period"],
                        "to_period": current["period"],
                        "change": format(change, "f"),
                        "rate_percent": rate,
                        "reason": reason,
                        "account_id": current["account_id"],
                        "company": current["company"],
                    }
                )
        return rows, calculations

    async def compare_cfs_ofs(self, request: FinancialQueryRequest) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        rows = await self.get_fact(request)
        grouped: dict[tuple[str, str, str, str, str], dict[str, dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(
                (
                    row["company"],
                    row["period"],
                    row["report_code"],
                    row["currency"],
                    row["account_id"],
                ),
                {},
            )[row["fs_div"]] = row
        calculations = []
        for (company, period, report_code, currency, account_id), pair in grouped.items():
            if {"CFS", "OFS"}.issubset(pair):
                difference = Decimal(pair["CFS"]["value"]) - Decimal(pair["OFS"]["value"])
                calculations.append(
                    {
                        "kind": "CFS_OFS_DIFF",
                        "company": company,
                        "period": period,
                        "report_code": report_code,
                        "currency": currency,
                        "account_id": account_id,
                        "difference": format(difference, "f"),
                    }
                )
        return rows, calculations

    async def get_statement(self, request: FinancialQueryRequest) -> list[dict[str, Any]]:
        return await self._rows(request, request.measures[0], statement_only=True)

    async def search_accounts(
        self, *, company: Company | None, query: str, limit: int, as_of: datetime
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT DISTINCT ON (fsl.corp_code, fsl.account_id, fsl.account_nm, fsl.sj_div)
                   CASE fsl.corp_code WHEN '00126380' THEN 'SAMSUNG_ELECTRONICS'
                                      WHEN '00164779' THEN 'SK_HYNIX' END AS company,
                   fsl.account_id, fsl.account_nm, fsl.sj_div, fsl.fs_div,
                   fsl.business_year, alias.metric_key, alias.comparison_grade
            FROM financial_statement_line fsl
            JOIN filing_revision fr ON fr.filing_revision_id = fsl.filing_revision_id
            JOIN snapshot_import si ON si.snapshot_id = fsl.snapshot_id
            LEFT JOIN LATERAL (
                SELECT metric_key, comparison_grade FROM metric_alias ma
                WHERE (ma.corp_code IS NULL OR ma.corp_code = fsl.corp_code)
                  AND (ma.account_id = fsl.account_id OR ma.exact_account_nm = fsl.account_nm)
                ORDER BY (ma.corp_code IS NOT NULL) DESC, priority LIMIT 1
            ) alias ON true
            WHERE ($1::bpchar IS NULL OR fsl.corp_code = $1)
              AND (fsl.account_nm ILIKE '%' || $2 || '%' OR fsl.account_id ILIKE '%' || $2 || '%')
              AND fr.filing_date <= ($3::timestamptz)::date
              AND si.acquired_at <= $3::timestamptz
            ORDER BY fsl.corp_code, fsl.account_id, fsl.account_nm, fsl.sj_div,
                     fsl.business_year DESC, fr.filing_date DESC
            LIMIT $4
        """
        try:
            async with self._pool.acquire() as connection:
                rows = await connection.fetch(
                    sql, CORP_CODES[company] if company else None, query, as_of, limit
                )
            return [dict(row) for row in rows]
        except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError) as exc:
            raise RepositoryUnavailable("account search failed") from exc

    async def get_coverage(
        self,
        *,
        companies: list[Company] | None,
        start_year: int,
        end_year: int,
        report_codes: list[str] | None = None,
        fs_divs: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT DISTINCT ON (dc.corp_code, dc.business_year, dc.reprt_code, dc.fs_div)
                   CASE dc.corp_code WHEN '00126380' THEN 'SAMSUNG_ELECTRONICS'
                                     WHEN '00164779' THEN 'SK_HYNIX' END AS company,
                   dc.business_year, dc.reprt_code, dc.fs_div, dc.status,
                   dc.api_status_code, dc.rcept_no, dc.source_row_count,
                   dc.loaded_row_count, dc.checked_at,
                   (
                       SELECT MIN(fr.filing_date)
                       FROM filing_revision fr
                       WHERE fr.corp_code = dc.corp_code
                         AND fr.business_year = dc.business_year
                         AND fr.reprt_code = dc.reprt_code
                   ) AS first_filing_date
            FROM data_coverage dc
            WHERE dc.corp_code = ANY($1::bpchar[])
              AND dc.reprt_code = ANY($2::bpchar[])
              AND dc.fs_div = ANY($3::text[])
              AND dc.business_year BETWEEN $4 AND $5
            ORDER BY dc.corp_code, dc.business_year, dc.reprt_code, dc.fs_div,
                     dc.checked_at DESC, dc.data_coverage_id DESC
        """
        if companies is None:
            companies = list(CORP_CODES.keys())
        effective_report_codes = report_codes or SUPPORTED_REPORT_CODES
        effective_fs_divs = fs_divs or SUPPORTED_FS_DIVS
        try:
            async with self._pool.acquire() as connection:
                rows = await connection.fetch(
                    sql,
                    [CORP_CODES[item] for item in companies],
                    list(effective_report_codes),
                    list(effective_fs_divs),
                    start_year,
                    end_year,
                )
            return [dict(row) for row in rows]
        except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError) as exc:
            raise RepositoryUnavailable("coverage query failed") from exc

    async def query(self, request: FinancialQueryRequest) -> dict[str, Any]:
        calculations: list[dict[str, Any]] = []
        if request.mode == FinancialQueryMode.YOY:
            rows, calculations = await self.calculate_yoy(request)
        elif request.mode == FinancialQueryMode.CFS_OFS_DIFF:
            rows, calculations = await self.compare_cfs_ofs(request)
        elif request.mode == FinancialQueryMode.STATEMENT:
            rows = await self.get_statement(request)
        else:
            rows = await self.get_fact(request)
        coverage = await self.get_coverage(
            companies=request.companies,
            start_year=_year(request.start_period), end_year=_year(request.end_period),
            report_codes=request.report_codes,
            fs_divs=request.fs_divs,
        )
        warnings: list[str] = []
        if not rows:
            warnings.append("INSUFFICIENT_EVIDENCE")
        covered = {
            (
                row["company"],
                row["business_year"],
                row["reprt_code"],
                row["fs_div"],
            )
            for row in coverage
        }
        missing_coverage = self._expected_coverage_keys(
            companies=request.companies,
            start_year=_year(request.start_period),
            end_year=_year(request.end_period),
            report_codes=request.report_codes,
            fs_divs=request.fs_divs,
        ) - covered
        if any(item["status"] != "LOADED" for item in coverage) or missing_coverage:
            warnings.append("CORPUS_INCOMPLETE")
        return {"facts": rows, "evidence": rows, "calculations": calculations, "coverage": coverage, "warnings": warnings}
