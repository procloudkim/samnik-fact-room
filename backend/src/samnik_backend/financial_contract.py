from __future__ import annotations

from enum import StrEnum


SUPPORTED_COMPANIES = frozenset({"00126380", "00164779"})
SUPPORTED_REPORT_CODES = frozenset({"11013", "11012", "11014", "11011"})
SUPPORTED_FS_DIVS = frozenset({"CFS", "OFS"})
SUPPORTED_STATEMENT_DIVS = frozenset({"BS", "IS", "CIS", "CF", "SCE"})
STANDARD_METRICS = frozenset(
    {
        "revenue",
        "operating_profit",
        "net_income",
        "total_assets",
        "total_liabilities",
        "total_equity",
        "operating_cash_flow",
    }
)


class FinancialQueryMode(StrEnum):
    FACT = "FACT"
    TIME_SERIES = "TIME_SERIES"
    COMPARE = "COMPARE"
    YOY = "YOY"
    CFS_OFS_DIFF = "CFS_OFS_DIFF"
    STATEMENT = "STATEMENT"


class FinancialValueBasis(StrEnum):
    POINT_IN_TIME = "POINT_IN_TIME"
    QUARTER_ONLY = "QUARTER_ONLY"
    YTD = "YTD"
    ANNUAL = "ANNUAL"


class CoverageStatus(StrEnum):
    LOADED = "LOADED"
    NOT_FILED = "NOT_FILED"
    NOT_YET_FILED = "NOT_YET_FILED"
    SOURCE_NOT_AVAILABLE = "SOURCE_NOT_AVAILABLE"
    FAILED = "FAILED"


class NarrativeCoverageStatus(StrEnum):
    FULL_TEXT = "FULL_TEXT"
    PARTIAL_TEXT = "PARTIAL_TEXT"
    SOURCE_NOT_AVAILABLE = "SOURCE_NOT_AVAILABLE"


def assert_contract() -> None:
    """Fail fast when a supported-scope constant is changed incompletely."""

    assert SUPPORTED_REPORT_CODES == {"11013", "11012", "11014", "11011"}
    assert SUPPORTED_FS_DIVS == {"CFS", "OFS"}
    assert SUPPORTED_STATEMENT_DIVS == {"BS", "IS", "CIS", "CF", "SCE"}
    assert len(STANDARD_METRICS) == 7
