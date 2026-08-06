from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import asyncpg
import httpx
import pytest
from pydantic import ValidationError

from samnik_backend.api import create_app
from samnik_backend.domain import Company
from samnik_backend.financial_contract import (
    FinancialQueryMode,
    FinancialValueBasis,
    STANDARD_METRICS,
    SUPPORTED_FS_DIVS,
    SUPPORTED_REPORT_CODES,
    SUPPORTED_STATEMENT_DIVS,
)
from samnik_backend.financial_queries import (
    FinancialMeasure,
    FinancialQueryRequest,
    PostgresFinancialQueryRepository,
)


def test_full_financial_scope_contract_is_fixed() -> None:
    assert SUPPORTED_REPORT_CODES == {"11013", "11012", "11014", "11011"}
    assert SUPPORTED_FS_DIVS == {"CFS", "OFS"}
    assert SUPPORTED_STATEMENT_DIVS == {"BS", "IS", "CIS", "CF", "SCE"}
    assert STANDARD_METRICS == {
        "revenue", "operating_profit", "net_income", "total_assets",
        "total_liabilities", "total_equity", "operating_cash_flow",
    }


def test_measure_requires_exactly_one_identifier() -> None:
    with pytest.raises(ValidationError):
        FinancialMeasure()
    with pytest.raises(ValidationError):
        FinancialMeasure(metric_key="revenue", account_id="ifrs-full_Revenue")


def _request(
    mode: FinancialQueryMode = FinancialQueryMode.FACT,
    *,
    companies: list[Company] = [Company.SAMSUNG_ELECTRONICS],
    start_period: str = "2025",
    end_period: str = "2025",
    report_codes: list[str] = ["11011"],
    fs_divs: list[str] = ["CFS", "OFS"],
    value_basis: FinancialValueBasis = FinancialValueBasis.ANNUAL,
    measures: list[FinancialMeasure] | None = None,
) -> FinancialQueryRequest:
    return FinancialQueryRequest(
        mode=mode,
        companies=companies,
        measures=measures or [FinancialMeasure(metric_key="operating_profit")],
        start_period=start_period,
        end_period=end_period,
        report_codes=report_codes,
        fs_divs=fs_divs,
        value_basis=value_basis,
    )


def _request_for_mode(mode: FinancialQueryMode) -> FinancialQueryRequest:
    if mode == FinancialQueryMode.COMPARE:
        return _request(
            mode=mode,
            companies=[Company.SAMSUNG_ELECTRONICS, Company.SK_HYNIX],
            fs_divs=["CFS", "OFS"],
            start_period="2024",
            end_period="2024",
        )
    if mode == FinancialQueryMode.CFS_OFS_DIFF:
        return _request(
            mode=mode,
            fs_divs=["CFS", "OFS"],
            start_period="2024",
            end_period="2024",
        )
    return _request(mode=mode)


def test_query_model_rejects_invalid_contract_input() -> None:
    with pytest.raises(ValidationError):
        _request(mode=FinancialQueryMode.COMPARE)
    with pytest.raises(ValidationError):
        _request(
            mode=FinancialQueryMode.CFS_OFS_DIFF,
            companies=[Company.SAMSUNG_ELECTRONICS, Company.SK_HYNIX],
        )
    with pytest.raises(ValidationError):
        _request(mode=FinancialQueryMode.CFS_OFS_DIFF, fs_divs=["CFS"])
    with pytest.raises(ValidationError):
        _request(mode=FinancialQueryMode.STATEMENT, measures=[
            FinancialMeasure(metric_key="operating_profit"),
            FinancialMeasure(metric_key="net_income"),
        ])
    with pytest.raises(ValidationError):
        _request(start_period="2014", end_period="2014")
    with pytest.raises(ValidationError):
        _request(start_period="2026", end_period="2025")
    with pytest.raises(ValidationError):
        FinancialQueryRequest(
            mode=FinancialQueryMode.FACT,
            companies=[Company.SAMSUNG_ELECTRONICS],
            measures=[FinancialMeasure(metric_key="operating_profit")],
            start_period="2025",
            end_period="2025",
            report_codes=["11011", "99999"],
            fs_divs=["CFS"],
            value_basis=FinancialValueBasis.ANNUAL,
        )


def test_measure_requires_account_or_metric_exactly_one() -> None:
    with pytest.raises(ValidationError):
        FinancialMeasure()
    with pytest.raises(ValidationError):
        FinancialMeasure(metric_key="revenue", account_id="ifrs-full_Revenue")


@pytest.mark.asyncio
async def test_live_financial_api_returns_real_cfs_and_ofs_rows() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for the live financial API test")
    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=2)
    try:
        repository = PostgresFinancialQueryRepository(pool)
        transport = httpx.ASGITransport(
            app=create_app(financial_repository=repository)
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://financial.test") as client:
            response = await client.post(
                "/api/v1/financials/query",
                json=_request().model_dump(mode="json"),
            )
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["facts"]
            assert {row["fs_div"] for row in payload["facts"]} == {"CFS", "OFS"}
            assert all(row["account_id"] and row["rcept_no"] for row in payload["facts"])
            assert all(row["raw_amount"] is not None for row in payload["facts"])

            accounts = await client.get(
                "/api/v1/financials/accounts",
                params={"q": "영업이익", "company": "SAMSUNG_ELECTRONICS"},
            )
            assert accounts.status_code == 200, accounts.text
            assert accounts.json()["accounts"]
    finally:
        await pool.close()


class _StubFinancialQueryRepository(PostgresFinancialQueryRepository):
    def __init__(self, rows: list[dict], coverage: list[dict]) -> None:
        super().__init__(pool=None)  # type: ignore[arg-type]
        self._rows = rows
        self._coverage = coverage

    async def get_fact(self, request: FinancialQueryRequest) -> list[dict]:
        return self._rows

    async def get_coverage(
        self,
        *,
        companies: list[Company],
        start_year: int,
        end_year: int,
        report_codes: list[str],
        fs_divs: list[str],
    ) -> list[dict]:
        return self._coverage


class _FakeAsyncConnection:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.sql: str | None = None
        self.args: tuple[Any, ...] | None = None

    async def fetch(self, sql: str, *args: object) -> list[dict[str, object]]:
        self.sql = sql
        self.args = args
        return self.rows


class _FakePoolAcquireContext:
    def __init__(self, connection: _FakeAsyncConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _FakeAsyncConnection:
        return self.connection

    async def __aexit__(self, *_) -> None:
        return None


class _FakePool:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.connection = _FakeAsyncConnection(rows)

    def acquire(self) -> _FakePoolAcquireContext:
        return _FakePoolAcquireContext(self.connection)


def _financial_row_for_test(
    *,
    business_year: int,
    reprt_code: str = "11011",
    fs_div: str = "CFS",
    company: str = "SAMSUNG_ELECTRONICS",
    company_code: str = "00126380",
    metric_key: str = "operating_profit",
    statement: str = "IS",
    account_id: str = "ifrs-full_ProfitLossFromOperatingActivities",
    account_nm: str = "영업이익",
    account_detail: str | None = "",
    ord_: int = 1,
    amount_numeric: int | float = 0,
    currency: str = "KRW",
    filing_date: str = "2025-12-31",
    rcept_no: str = "20251231001111",
    acquired_at: str = "2026-01-01T00:00:00+00:00",
    snapshot_id: str = "snapshot-001",
    financial_statement_line_id: int = 1,
    filing_revision_id: int = 1,
    selected_latest: bool = True,
    raw_amount: str | None = "0",
) -> dict[str, object]:
    return {
        "corp_code": company_code,
        "company": company,
        "company_label": company,
        "business_year": business_year,
        "reprt_code": reprt_code,
        "fs_div": fs_div,
        "sj_div": statement,
        "metric_key": metric_key,
        "comparison_grade": "REVIEWED",
        "account_id": account_id,
        "account_nm": account_nm,
        "account_detail": account_detail,
        "ord": ord_,
        "currency": currency,
        "amount_numeric": amount_numeric,
        "raw_amount": raw_amount,
        "value_basis": "ANNUAL",
        "source_field": "thstrm_amount",
        "source_url": "https://example.local/source",
        "rcept_no": rcept_no,
        "filing_date": datetime.fromisoformat(filing_date),
        "acquired_at": datetime.fromisoformat(acquired_at),
        "correction_kind": "ORIGINAL",
        "row_hash": "0" * 64,
        "snapshot_id": snapshot_id,
        "financial_statement_line_id": financial_statement_line_id,
        "filing_revision_id": filing_revision_id,
        "selected_latest": selected_latest,
    }


class _RecordingFinancialQueryRepository:
    def __init__(self) -> None:
        self.requests: list[FinancialQueryRequest] = []

    async def query(self, request: FinancialQueryRequest) -> dict[str, Any]:
        self.requests.append(request)
        return {
            "facts": [],
            "evidence": [],
            "calculations": [],
            "coverage": [
                {
                    "company": request.companies[0].value,
                    "business_year": 2024,
                    "reprt_code": request.report_codes[0],
                    "fs_div": request.fs_divs[0],
                    "status": "LOADED",
                    "api_status_code": None,
                    "rcept_no": None,
                    "source_row_count": 0,
                    "loaded_row_count": 0,
                    "checked_at": datetime.now(UTC).isoformat(),
                }
            ],
            "warnings": [],
        }


class _RecordingCoverageRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[list[Company], int, int, list[str] | None, list[str] | None]] = []

    async def get_coverage(
        self,
        *,
        companies: list[Company] | None,
        start_year: int,
        end_year: int,
        report_codes: list[str] | None = None,
        fs_divs: list[str] | None = None,
    ) -> list[dict]:
        self.calls.append(
            (companies or [], start_year, end_year, report_codes, fs_divs)
        )
        return [{"company": "SAMSUNG_ELECTRONICS", "business_year": 2025, "reprt_code": "11011", "fs_div": "CFS"}]


@pytest.mark.parametrize("mode", list(FinancialQueryMode))
@pytest.mark.asyncio
async def test_financial_api_query_supports_all_modes(mode: FinancialQueryMode) -> None:
    repository = _RecordingFinancialQueryRepository()
    transport = httpx.ASGITransport(app=create_app(financial_repository=repository))

    async with httpx.AsyncClient(transport=transport, base_url="http://financial.test") as client:
        response = await client.post(
            "/api/v1/financials/query",
            json=_request_for_mode(mode).model_dump(mode="json"),
        )

    assert response.status_code == 200, response.text
    assert response.json()["coverage"]
    assert repository.requests[0].mode == mode
    assert repository.requests[0].as_of is not None


@pytest.mark.asyncio
async def test_financial_api_query_forwards_as_of() -> None:
    repository = _RecordingFinancialQueryRepository()
    transport = httpx.ASGITransport(app=create_app(financial_repository=repository))
    payload = _request(mode=FinancialQueryMode.FACT)
    payload.as_of = datetime(2026, 1, 1, tzinfo=UTC)

    async with httpx.AsyncClient(transport=transport, base_url="http://financial.test") as client:
        response = await client.post("/api/v1/financials/query", json=payload.model_dump(mode="json"))

    assert response.status_code == 200, response.text
    assert repository.requests[0].as_of == datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_yoy_calculation_uses_consecutive_periods_and_stable_key() -> None:
    rows = [
        {
            "company": "SAMSUNG_ELECTRONICS",
            "account_id": "ifrs-full_Revenue",
            "report_code": "11011",
            "fs_div": "CFS",
            "value_basis": "ANNUAL",
            "currency": "KRW",
            "business_year": 2024,
            "period": "2024",
            "value": "100",
        },
        {
            "company": "SAMSUNG_ELECTRONICS",
            "account_id": "ifrs-full_Revenue",
            "report_code": "11011",
            "fs_div": "CFS",
            "value_basis": "ANNUAL",
            "currency": "KRW",
            "business_year": 2025,
            "period": "2025",
            "value": "120",
        },
        {
            "company": "SAMSUNG_ELECTRONICS",
            "account_id": "ifrs-full_Revenue",
            "report_code": "11011",
            "fs_div": "CFS",
            "value_basis": "ANNUAL",
            "currency": "KRW",
            "business_year": 2027,
            "period": "2027",
            "value": "130",
        },
        {
            "company": "SAMSUNG_ELECTRONICS",
            "account_id": "ifrs-full_Revenue",
            "report_code": "11013",
            "fs_div": "CFS",
            "value_basis": "ANNUAL",
            "currency": "KRW",
            "business_year": 2025,
            "period": "2025Q1",
            "value": "60",
        },
    ]
    repository = _StubFinancialQueryRepository(rows=rows, coverage=[])
    _, calculations = await repository.calculate_yoy(
        _request(
            mode=FinancialQueryMode.YOY,
            start_period="2024",
            end_period="2027",
        )
    )

    assert len(calculations) == 1
    assert calculations[0]["from_period"] == "2024"
    assert calculations[0]["to_period"] == "2025"


@pytest.mark.parametrize(
    ("metric_key", "expected_statement"),
    [
        ("total_assets", "BS"),
        ("total_liabilities", "BS"),
        ("total_equity", "BS"),
        ("operating_cash_flow", "CF"),
        ("revenue", "IS"),
        ("operating_profit", "IS"),
        ("net_income", "IS"),
    ],
)
@pytest.mark.asyncio
async def test_financial_rows_prefers_standard_metric_statement(metric_key: str, expected_statement: str) -> None:
    rows = [
        _financial_row_for_test(
            business_year=2025,
            metric_key=metric_key,
            statement=expected_statement,
            account_id="preferred-account",
            amount_numeric=10,
            financial_statement_line_id=10,
        ),
        _financial_row_for_test(
            business_year=2025,
            metric_key=metric_key,
            statement="CIS" if expected_statement == "IS" else ("IS" if expected_statement == "CIS" else "IS"),
            account_id="other-account",
            amount_numeric=20,
            financial_statement_line_id=20,
        ),
    ]
    if expected_statement == "BS":
        rows[1]["sj_div"] = "IS"
    if expected_statement == "CF":
        rows[1]["sj_div"] = "IS"
    repository = PostgresFinancialQueryRepository(_FakePool(rows))
    request = _request(
        mode=FinancialQueryMode.FACT,
        measures=[FinancialMeasure(metric_key=metric_key)],
        value_basis=FinancialValueBasis.POINT_IN_TIME if metric_key in {"total_assets", "total_liabilities", "total_equity"} else FinancialValueBasis.ANNUAL,
    )
    facts = await repository._rows(request, request.measures[0])

    assert len(facts) == 1
    assert facts[0]["statement"] == expected_statement
    assert facts[0]["metric_key"] == metric_key


@pytest.mark.asyncio
async def test_financial_rows_selects_latest_revision_with_zero_value() -> None:
    rows = [
        _financial_row_for_test(
            business_year=2025,
            metric_key="revenue",
            statement="IS",
            amount_numeric=100,
            filing_date="2025-01-01",
            rcept_no="20250101000000",
            acquired_at="2025-01-01T00:00:00+00:00",
            snapshot_id="snapshot-old",
            financial_statement_line_id=1,
            raw_amount="100",
        ),
            _financial_row_for_test(
                business_year=2025,
                metric_key="revenue",
                statement="IS",
                amount_numeric=0,
            filing_date="2026-01-01",
            rcept_no="20260101000000",
            acquired_at="2026-01-01T00:00:00+00:00",
            snapshot_id="snapshot-new",
            financial_statement_line_id=2,
            raw_amount="0",
        ),
    ]
    repository = PostgresFinancialQueryRepository(_FakePool(rows))
    request = _request(
        mode=FinancialQueryMode.STATEMENT,
        measures=[FinancialMeasure(metric_key="revenue")],
        value_basis=FinancialValueBasis.ANNUAL,
    )
    facts = await repository._rows(request, request.measures[0], statement_only=True)

    assert len(facts) == 1
    assert facts[0]["value"] == "0"


@pytest.mark.asyncio
async def test_coverage_warning_is_scoped_to_requested_scope() -> None:
    repository = _StubFinancialQueryRepository(
        rows=[{"company": "SAMSUNG_ELECTRONICS"}],
        coverage=[
            {
                "company": "SAMSUNG_ELECTRONICS",
                "business_year": 2025,
                "reprt_code": "11011",
                "fs_div": "CFS",
                "status": "LOADED",
            }
        ],
    )
    payload = repository.query(
        _request(
            companies=[Company.SAMSUNG_ELECTRONICS, Company.SK_HYNIX],
            fs_divs=["CFS", "OFS"],
            start_period="2025",
            end_period="2025",
            report_codes=["11011"],
            mode=FinancialQueryMode.FACT,
        )
    )
    result = await payload
    assert result["warnings"] == ["CORPUS_INCOMPLETE"]
    assert result["coverage"] == [
        {
            "company": "SAMSUNG_ELECTRONICS",
            "business_year": 2025,
            "reprt_code": "11011",
            "fs_div": "CFS",
            "status": "LOADED",
        }
    ]


@pytest.mark.asyncio
async def test_financial_coverage_endpoint_forwards_filters() -> None:
    repository = _RecordingCoverageRepository()
    transport = httpx.ASGITransport(app=create_app(financial_repository=repository))

    async with httpx.AsyncClient(transport=transport, base_url="http://financial.test") as client:
        response = await client.get(
            "/api/v1/financials/coverage",
            params=[
                ("company", "SAMSUNG_ELECTRONICS"),
                ("start_year", "2024"),
                ("end_year", "2025"),
                ("report_codes", "11011"),
                ("report_codes", "11013"),
                ("fs_divs", "CFS"),
                ("fs_divs", "OFS"),
            ],
        )

    assert response.status_code == 200, response.text
    assert response.json()["coverage"] == [
        {"company": "SAMSUNG_ELECTRONICS", "business_year": 2025, "reprt_code": "11011", "fs_div": "CFS"}
    ]
    assert repository.calls[0][0] == [Company.SAMSUNG_ELECTRONICS]
    assert repository.calls[0][1] == 2024
    assert repository.calls[0][2] == 2025
    assert repository.calls[0][3] == ["11011", "11013"]
    assert repository.calls[0][4] == ["CFS", "OFS"]


@pytest.mark.asyncio
async def test_financial_timeline_endpoint_exposes_real_period_matrix() -> None:
    repository = _RecordingCoverageRepository()
    repository.get_coverage = lambda **kwargs: _timeline_coverage(kwargs)  # type: ignore[method-assign]
    transport = httpx.ASGITransport(app=create_app(financial_repository=repository))

    async with httpx.AsyncClient(transport=transport, base_url="http://financial.test") as client:
        response = await client.get(
            "/api/v1/financials/timeline",
            params={"as_of": "2026-07-22T00:00:00Z"},
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert (payload["start_year"], payload["end_year"]) == (2015, 2026)
    assert len(payload["periods"]) == 48
    assert payload["latest_common_period"] == "2026Q1"
    assert payload["periods"][-3]["flow_value_basis"] == "YTD"
    assert payload["periods"][-1]["flow_value_basis"] == "ANNUAL"


async def _timeline_coverage(kwargs: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for company in kwargs["companies"]:
        for fs_div in ("CFS", "OFS"):
            rows.append(
                {
                    "company": company.value,
                    "business_year": 2026,
                    "reprt_code": "11013",
                    "fs_div": fs_div,
                    "status": "LOADED",
                    "rcept_no": "20260515000000",
                    "source_row_count": 1,
                    "loaded_row_count": 1,
                    "checked_at": "2026-05-16T00:00:00Z",
                }
            )
    return rows


@pytest.mark.asyncio
async def test_financial_timeline_requires_current_year() -> None:
    repository = _RecordingCoverageRepository()
    transport = httpx.ASGITransport(app=create_app(financial_repository=repository))

    async with httpx.AsyncClient(transport=transport, base_url="http://financial.test") as client:
        response = await client.get(
            "/api/v1/financials/timeline",
            params={"include_current": "false"},
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_standard_metrics_have_alias_entries() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for the alias contract test")
    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=2)
    try:
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT DISTINCT metric_key
                FROM metric_alias
                WHERE metric_key = ANY($1::text[])
                """,
                list(STANDARD_METRICS),
            )
        assert {row["metric_key"] for row in rows} == STANDARD_METRICS
    finally:
        await pool.close()
