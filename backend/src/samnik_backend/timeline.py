from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from .domain import Company
from .financial_contract import CoverageStatus, SUPPORTED_FS_DIVS, SUPPORTED_REPORT_CODES


class CoverageRepository(Protocol):
    async def get_coverage(
        self,
        *,
        companies: list[Company] | None,
        start_year: int,
        end_year: int,
        report_codes: list[str] | None = None,
        fs_divs: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        ...


class TimelineCell(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company: Company
    business_year: int
    report_code: str
    fs_div: str
    status: CoverageStatus
    rcept_no: str | None = None
    api_status_code: str | None = None
    checked_at: datetime | None = None
    source_row_count: int = 0
    loaded_row_count: int = 0
    reason: str | None = None


class TimelineCompanyStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company: Company
    cfs: TimelineCell
    ofs: TimelineCell
    filing_date_estimate: "FilingDateEstimate | None" = None


class FilingDateEstimate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    estimated_date: date
    sample_size: int
    history_start_year: int
    history_end_year: int
    average_lag_days: float
    methodology: str = "MEAN_FIRST_FILING_LAG"


class TimelinePeriodRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    business_year: int
    report_code: str
    period: str
    label: str
    period_end: date
    expected_by: date
    flow_value_basis: str
    stock_value_basis: str = "POINT_IN_TIME"
    companies: list[TimelineCompanyStatus]


class TimelineMatrix(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of: datetime
    start_year: int
    end_year: int
    completed_years: int
    filing_estimate_window_years: int = 10
    latest_common_period: str | None
    periods: list[TimelinePeriodRow]


REPORT_ORDER = ["11013", "11012", "11014", "11011"]
FS_ORDER = ["CFS", "OFS"]
DEFAULT_COMPANIES = [Company.SAMSUNG_ELECTRONICS, Company.SK_HYNIX]
_SUPPORTED_REPORTS = {item for item in SUPPORTED_REPORT_CODES}
_SUPPORTED_FS = {item for item in SUPPORTED_FS_DIVS}
SUPPORTED_MIN_YEAR = 2015
FILING_ESTIMATE_WINDOW_YEARS = 10


def _period_label(business_year: int, report_code: str) -> str:
    return {
        "11013": f"{business_year}Q1",
        "11012": f"{business_year}H1",
        "11014": f"{business_year}Q3",
        "11011": str(business_year),
    }[report_code]


def _report_label(report_code: str) -> str:
    return {
        "11013": "1분기",
        "11012": "반기 누적",
        "11014": "3분기 누적",
        "11011": "연간·4분기 포함",
    }[report_code]


def _period_end(business_year: int, report_code: str) -> date:
    month_day = {
        "11013": (3, 31),
        "11012": (6, 30),
        "11014": (9, 30),
        "11011": (12, 31),
    }[report_code]
    return date(business_year, *month_day)


def _deadline_for_year_and_report(business_year: int, report_code: str) -> datetime:
    period_end = _period_end(business_year, report_code)
    base = datetime.combine(period_end, datetime.max.time(), tzinfo=UTC)
    return base + timedelta(days=45 if report_code != "11011" else 90)


def _normalized_companies(companies: list[Company] | None) -> list[Company]:
    if not companies:
        return DEFAULT_COMPANIES
    normalized = [Company(item) for item in companies]
    if len(normalized) != len(set(normalized)):
        raise ValueError("companies must be unique")
    if not all(item in DEFAULT_COMPANIES for item in normalized):
        raise ValueError("unsupported company in timeline coverage matrix")
    return normalized


def _normalized_reports() -> list[str]:
    if set(REPORT_ORDER) != _SUPPORTED_REPORTS:
        raise ValueError("report code contract mismatch")
    return REPORT_ORDER.copy()


def _normalized_fs_divs() -> list[str]:
    if set(FS_ORDER) != _SUPPORTED_FS:
        raise ValueError("financial statement division contract mismatch")
    return FS_ORDER.copy()


def _parse_checked_at(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        parsed = value.strip()
        if not parsed:
            return None
        if parsed.endswith("Z"):
            parsed = f"{parsed[:-1]}+00:00"
        try:
            parsed_at = datetime.fromisoformat(parsed)
        except ValueError:
            return None
        if parsed_at.tzinfo is None:
            return parsed_at.replace(tzinfo=UTC)
        return parsed_at.astimezone(UTC)
    return None


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def _first_filing_dates(
    raw_rows: list[dict[str, Any]],
) -> dict[tuple[Company, int, str], date]:
    dates: dict[tuple[Company, int, str], date] = {}
    for row in raw_rows:
        try:
            company = _coerce_company(row["company"])
            business_year = int(row["business_year"])
            report_code = str(row["reprt_code"])
            filing_date = _parse_date(row.get("first_filing_date"))
        except (KeyError, TypeError, ValueError):
            continue
        if report_code not in _SUPPORTED_REPORTS or filing_date is None:
            continue
        key = (company, business_year, report_code)
        current = dates.get(key)
        if current is None or filing_date < current:
            dates[key] = filing_date
    return dates


def _filing_date_estimate(
    *,
    company: Company,
    business_year: int,
    report_code: str,
    filing_dates: dict[tuple[Company, int, str], date],
) -> FilingDateEstimate | None:
    history = sorted(
        (
            (year, filed_on)
            for (row_company, year, row_report), filed_on in filing_dates.items()
            if row_company == company and row_report == report_code and year < business_year
        ),
        key=lambda item: item[0],
    )[-FILING_ESTIMATE_WINDOW_YEARS:]
    if not history:
        return None
    lags = [
        (filed_on - _period_end(year, report_code)).days
        for year, filed_on in history
    ]
    average_lag = sum(lags) / len(lags)
    rounded_lag = math.floor(average_lag + 0.5)
    return FilingDateEstimate(
        estimated_date=_period_end(business_year, report_code) + timedelta(days=rounded_lag),
        sample_size=len(history),
        history_start_year=history[0][0],
        history_end_year=history[-1][0],
        average_lag_days=round(average_lag, 1),
    )


def _to_status(raw_status: Any) -> CoverageStatus:
    candidate = str(raw_status or "").strip().upper()
    for status in CoverageStatus:
        if status.value == candidate:
            return status
    return CoverageStatus.FAILED


def _coerce_company(value: Any) -> Company:
    if isinstance(value, Company):
        return value
    return Company(str(value))


def _missing_status_for(as_of: datetime, business_year: int, report_code: str) -> CoverageStatus:
    return (
        CoverageStatus.NOT_YET_FILED
        if as_of <= _deadline_for_year_and_report(business_year, report_code)
        else CoverageStatus.FAILED
    )


def _to_company_rows(
    *,
    company: Company,
    business_year: int,
    report_code: str,
    rows: dict[tuple[Company, int, str, str], dict[str, Any]],
    filing_date_estimate: FilingDateEstimate | None,
    as_of: datetime,
) -> TimelineCompanyStatus:
    cfs_row = rows.get((company, business_year, report_code, "CFS"))
    ofs_row = rows.get((company, business_year, report_code, "OFS"))
    if cfs_row is None:
        cfs_status = _missing_status_for(as_of, business_year, report_code)
        cfs_row_data = {
            "company": company,
            "business_year": business_year,
            "report_code": report_code,
            "fs_div": "CFS",
            "status": cfs_status,
            "reason": "NEVER_CHECKED" if cfs_status == CoverageStatus.FAILED else None,
        }
    else:
        cfs_row_data = {
            "company": company,
            "business_year": business_year,
            "report_code": report_code,
            "fs_div": cfs_row["fs_div"],
            "status": _to_status(cfs_row.get("status")),
            "rcept_no": str(cfs_row.get("rcept_no")) if cfs_row.get("rcept_no") else None,
            "api_status_code": str(cfs_row.get("api_status_code")) if cfs_row.get("api_status_code") else None,
            "checked_at": _parse_checked_at(cfs_row.get("checked_at")),
            "source_row_count": int(cfs_row.get("source_row_count") or 0),
            "loaded_row_count": int(cfs_row.get("loaded_row_count") or 0),
            "reason": (cfs_row.get("detail_json") or {}).get("reason")
            if isinstance(cfs_row.get("detail_json"), dict)
            else None,
        }
    if ofs_row is None:
        ofs_status = _missing_status_for(as_of, business_year, report_code)
        ofs_row_data = {
            "company": company,
            "business_year": business_year,
            "report_code": report_code,
            "fs_div": "OFS",
            "status": ofs_status,
            "reason": "NEVER_CHECKED" if ofs_status == CoverageStatus.FAILED else None,
        }
    else:
        ofs_row_data = {
            "company": company,
            "business_year": business_year,
            "report_code": report_code,
            "fs_div": ofs_row["fs_div"],
            "status": _to_status(ofs_row.get("status")),
            "rcept_no": str(ofs_row.get("rcept_no")) if ofs_row.get("rcept_no") else None,
            "api_status_code": str(ofs_row.get("api_status_code")) if ofs_row.get("api_status_code") else None,
            "checked_at": _parse_checked_at(ofs_row.get("checked_at")),
            "source_row_count": int(ofs_row.get("source_row_count") or 0),
            "loaded_row_count": int(ofs_row.get("loaded_row_count") or 0),
            "reason": (ofs_row.get("detail_json") or {}).get("reason")
            if isinstance(ofs_row.get("detail_json"), dict)
            else None,
        }

    return TimelineCompanyStatus(
        company=company,
        cfs=TimelineCell.model_validate(cfs_row_data),
        ofs=TimelineCell.model_validate(ofs_row_data),
        filing_date_estimate=filing_date_estimate,
    )


def _latest_rows_by_key(raw_rows: list[dict[str, Any]]) -> dict[tuple[Company, int, str, str], dict[str, Any]]:
    latest: dict[tuple[Company, int, str, str], dict[str, Any]] = {}
    for row in raw_rows:
        try:
            company = _coerce_company(row["company"])
            business_year = int(row["business_year"])
            report_code = str(row["reprt_code"])
            fs_div = str(row["fs_div"])
            if report_code not in _SUPPORTED_REPORTS or fs_div not in _SUPPORTED_FS:
                continue
            key = (company, business_year, report_code, fs_div)
            checked_at = _parse_checked_at(row.get("checked_at"))
            current = latest.get(key)
            if current is None:
                latest[key] = row
                continue
            previous = _parse_checked_at(current.get("checked_at"))
            if checked_at is not None and (previous is None or checked_at > previous):
                latest[key] = row
        except (TypeError, ValueError):
            continue
    return latest


async def build_financial_coverage_timeline(
    repository: CoverageRepository,
    *,
    as_of: datetime | None = None,
    companies: list[Company] | None = None,
) -> TimelineMatrix:
    as_of_value = as_of or datetime.now(UTC)
    if as_of_value.tzinfo is None:
        as_of_value = as_of_value.replace(tzinfo=UTC)
    effective_companies = _normalized_companies(companies)
    normalized_companies = sorted(effective_companies, key=lambda item: item.value)
    report_codes = _normalized_reports()
    fs_divs = _normalized_fs_divs()
    end_year = as_of_value.year
    start_year = SUPPORTED_MIN_YEAR
    raw_coverage = await repository.get_coverage(
        companies=normalized_companies,
        start_year=start_year,
        end_year=end_year,
        report_codes=report_codes,
        fs_divs=fs_divs,
    )
    latest_rows = _latest_rows_by_key(raw_coverage)
    filing_dates = _first_filing_dates(raw_coverage)
    periods: list[TimelinePeriodRow] = []
    for business_year in range(start_year, end_year + 1):
        for report_code in report_codes:
            periods.append(
                TimelinePeriodRow(
                    business_year=business_year,
                    report_code=report_code,
                    period=_period_label(business_year, report_code),
                    label=_report_label(report_code),
                    period_end=_period_end(business_year, report_code),
                    expected_by=_deadline_for_year_and_report(
                        business_year, report_code
                    ).date(),
                    flow_value_basis="ANNUAL" if report_code == "11011" else "YTD",
                    companies=[
                        _to_company_rows(
                            company=company,
                            business_year=business_year,
                            report_code=report_code,
                            rows=latest_rows,
                            filing_date_estimate=_filing_date_estimate(
                                company=company,
                                business_year=business_year,
                                report_code=report_code,
                                filing_dates=filing_dates,
                            ),
                            as_of=as_of_value,
                        )
                        for company in normalized_companies
                    ],
                )
            )
    latest_common_period = None
    for period in reversed(periods):
        if all(company.cfs.status == CoverageStatus.LOADED for company in period.companies):
            latest_common_period = period.period
            break
    return TimelineMatrix(
        as_of=as_of_value,
        start_year=start_year,
        end_year=end_year,
        completed_years=max(0, end_year - start_year),
        latest_common_period=latest_common_period,
        periods=periods,
    )
