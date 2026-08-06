from __future__ import annotations

from datetime import UTC, datetime

import pytest

from samnik_backend.domain import Company
from samnik_backend.financial_contract import CoverageStatus
from samnik_backend.timeline import (
    CoverageRepository,
    TimelineCompanyStatus,
    build_financial_coverage_timeline,
)


class _StubCoverageRepository(CoverageRepository):
    def __init__(self, coverage_rows: list[dict]) -> None:
        self.coverage_rows = coverage_rows
        self.calls: list[tuple] = []

    async def get_coverage(
        self,
        *,
        companies: list[Company] | None,
        start_year: int,
        end_year: int,
        report_codes: list[str] | None = None,
        fs_divs: list[str] | None = None,
    ) -> list[dict]:
        self.calls.append((companies, start_year, end_year, tuple(report_codes or []), tuple(fs_divs or [])))
        return self.coverage_rows


def _coverage_row(
    *,
    company: Company,
    business_year: int,
    report_code: str,
    fs_div: str,
    status: CoverageStatus = CoverageStatus.LOADED,
    checked_at: str = "2026-01-01T00:00:00+00:00",
    rcept_no: str | None = None,
    api_status_code: str | None = None,
    first_filing_date: str | None = None,
) -> dict:
    return {
        "company": company,
        "business_year": business_year,
        "reprt_code": report_code,
        "fs_div": fs_div,
        "status": status.value,
        "rcept_no": rcept_no,
        "api_status_code": api_status_code,
        "checked_at": checked_at,
        "first_filing_date": first_filing_date,
    }


def _find_cell(
    matrix,
    *,
    company: Company,
    business_year: int,
    report_code: str,
    fs_div: str,
) -> CoverageStatus:
    period = next(
        row
        for row in matrix.periods
        if row.business_year == business_year and row.report_code == report_code
    )
    company_row = next(
        row_company
        for row_company in period.companies
        if row_company.company == company
    )
    if fs_div == "CFS":
        return company_row.cfs.status
    return company_row.ofs.status


def _build_default_loaded_rows() -> list[dict]:
    rows: list[dict] = []
    for company in (Company.SAMSUNG_ELECTRONICS, Company.SK_HYNIX):
        for year in range(2015, 2027):
            for report_code in ("11013", "11012", "11014", "11011"):
                for fs_div in ("CFS", "OFS"):
                    if year == 2026 and report_code != "11013":
                        continue
                    filing_date = {
                        "11013": f"{year}-05-15",
                        "11012": f"{year}-08-14",
                        "11014": f"{year}-11-14",
                        "11011": f"{year + 1}-03-20",
                    }[report_code]
                    rows.append(
                        _coverage_row(
                            company=company,
                            business_year=year,
                            report_code=report_code,
                            fs_div=fs_div,
                            status=CoverageStatus.LOADED,
                            checked_at="2026-01-02T00:00:00+00:00" if year > 2023 else "2023-01-01T00:00:00+00:00",
                            rcept_no=f"R{year}{report_code}{fs_div}",
                            first_filing_date=filing_date,
                        )
                    )
    return rows


@pytest.mark.asyncio
async def test_financial_coverage_timeline_keeps_all_2015_plus_etl_years() -> None:
    as_of = datetime(2026, 7, 22, tzinfo=UTC)
    repository = _StubCoverageRepository(_build_default_loaded_rows())

    matrix = await build_financial_coverage_timeline(
        repository,
        as_of=as_of,
    )

    assert len(matrix.periods) == 48
    assert [item.report_code for item in matrix.periods[:4]] == ["11013", "11012", "11014", "11011"]
    assert matrix.as_of == as_of
    assert matrix.periods[0].period == "2015Q1"
    assert matrix.periods[0].label == "1분기"
    assert matrix.periods[0].flow_value_basis == "YTD"
    assert matrix.periods[3].flow_value_basis == "ANNUAL"
    assert matrix.latest_common_period == "2026Q1"
    assert matrix.periods[0].companies[0].company == Company.SAMSUNG_ELECTRONICS
    flat_statuses: list[CoverageStatus] = [
        status
        for period in matrix.periods
        for company in period.companies
        for status in (company.cfs.status, company.ofs.status)
    ]
    assert len(flat_statuses) == 192
    assert flat_statuses.count(CoverageStatus.LOADED) == 180
    assert flat_statuses.count(CoverageStatus.NOT_YET_FILED) == 12
    half_year = next(
        period
        for period in matrix.periods
        if period.business_year == 2026 and period.report_code == "11012"
    )
    samsung = next(
        item for item in half_year.companies if item.company == Company.SAMSUNG_ELECTRONICS
    )
    assert samsung.filing_date_estimate is not None
    assert samsung.filing_date_estimate.sample_size == 10
    assert samsung.filing_date_estimate.history_start_year == 2016
    assert samsung.filing_date_estimate.history_end_year == 2025
    assert samsung.filing_date_estimate.estimated_date.isoformat() == "2026-08-14"
    assert repository.calls == [(
        [Company.SAMSUNG_ELECTRONICS, Company.SK_HYNIX],
        2015,
        2026,
        ("11013", "11012", "11014", "11011"),
        ("CFS", "OFS"),
    )]


@pytest.mark.asyncio
async def test_financial_coverage_timeline_uses_not_yet_status_before_deadline() -> None:
    as_of = datetime(2026, 4, 1, tzinfo=UTC)
    repository = _StubCoverageRepository([])

    matrix = await build_financial_coverage_timeline(
        repository,
        as_of=as_of,
        companies=[Company.SAMSUNG_ELECTRONICS],
    )

    assert _find_cell(
        matrix,
        company=Company.SAMSUNG_ELECTRONICS,
        business_year=2026,
        report_code="11013",
        fs_div="CFS",
    ) == CoverageStatus.NOT_YET_FILED
    assert _find_cell(
        matrix,
        company=Company.SAMSUNG_ELECTRONICS,
        business_year=2025,
        report_code="11013",
        fs_div="OFS",
    ) == CoverageStatus.FAILED


@pytest.mark.asyncio
async def test_financial_coverage_timeline_marks_unchecked_missing_as_failed_after_deadline() -> None:
    as_of = datetime(2026, 6, 1, tzinfo=UTC)
    repository = _StubCoverageRepository([])

    matrix = await build_financial_coverage_timeline(
        repository,
        as_of=as_of,
        companies=[Company.SAMSUNG_ELECTRONICS],
    )

    assert _find_cell(
        matrix,
        company=Company.SAMSUNG_ELECTRONICS,
        business_year=2026,
        report_code="11013",
        fs_div="CFS",
    ) == CoverageStatus.FAILED
    assert _find_cell(
        matrix,
        company=Company.SAMSUNG_ELECTRONICS,
        business_year=2026,
        report_code="11011",
        fs_div="CFS",
    ) == CoverageStatus.NOT_YET_FILED


@pytest.mark.asyncio
async def test_financial_coverage_timeline_prefers_latest_checked_row() -> None:
    rows = [
        _coverage_row(
            company=Company.SAMSUNG_ELECTRONICS,
            business_year=2025,
            report_code="11011",
            fs_div="CFS",
            status=CoverageStatus.FAILED,
            checked_at="2025-01-01T00:00:00+00:00",
            rcept_no="old",
        ),
        _coverage_row(
            company=Company.SAMSUNG_ELECTRONICS,
            business_year=2025,
            report_code="11011",
            fs_div="CFS",
            status=CoverageStatus.LOADED,
            checked_at="2026-01-01T00:00:00+00:00",
            rcept_no="new",
        ),
        _coverage_row(
            company=Company.SK_HYNIX,
            business_year=2025,
            report_code="11011",
            fs_div="OFS",
            status=CoverageStatus.LOADED,
            checked_at="2025-06-01T00:00:00+00:00",
            rcept_no="only",
        ),
    ]
    repository = _StubCoverageRepository(rows)
    matrix = await build_financial_coverage_timeline(
        repository,
        as_of=datetime(2026, 7, 22, tzinfo=UTC),
        companies=[Company.SAMSUNG_ELECTRONICS, Company.SK_HYNIX],
    )

    samsung = next(
        period
        for period in matrix.periods
        if period.business_year == 2025 and period.report_code == "11011"
    )
    samsung_cfs = next(
        row for row in samsung.companies if row.company == Company.SAMSUNG_ELECTRONICS
    )
    assert isinstance(samsung_cfs, TimelineCompanyStatus)
    assert samsung_cfs.cfs.status == CoverageStatus.LOADED
    assert samsung_cfs.cfs.rcept_no == "new"
