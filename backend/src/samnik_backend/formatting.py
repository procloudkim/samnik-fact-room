from __future__ import annotations

from decimal import Decimal
import re


_KRW_GROUPS = (
    (10**12, "조"),
    (10**8, "억"),
    (10**4, "만"),
)


def format_korean_krw(amount: Decimal) -> str:
    """Render an integer KRW amount with Korean large-number units."""

    if amount != amount.to_integral_value():
        raise ValueError("KRW amount must be an integer")
    integer = int(amount)
    if integer == 0:
        return "0원"

    sign = "-" if integer < 0 else ""
    remainder = abs(integer)
    parts: list[str] = []
    for divisor, label in _KRW_GROUPS:
        quotient, remainder = divmod(remainder, divisor)
        if quotient:
            parts.append(f"{quotient:,}{label}")
    if remainder:
        parts.append(f"{remainder:,}원")
    else:
        parts.append("원")
    return sign + " ".join(parts)


def format_trillion_krw(amount: Decimal) -> str:
    """Render a two-decimal trillion-won approximation."""

    trillion = amount / Decimal("1000000000000")
    return f"약 {trillion:,.2f}조 원"


def format_krw_with_approximation(amount: Decimal) -> str:
    return f"{format_korean_krw(amount)}({format_trillion_krw(amount)})"


def period_sort_key(period: str) -> tuple[int, int]:
    """Sort supported disclosure periods in calendar/report order."""

    match = re.fullmatch(r"(20\d{2})(Q1|H1|Q3)?", period)
    if not match:
        return (0, 0)
    rank = {"Q1": 1, "H1": 2, "Q3": 3, None: 4}[match.group(2)]
    return (int(match.group(1)), rank)


def format_period_label(period: str) -> str:
    """Render repository period keys in plain Korean."""

    match = re.fullmatch(r"(20\d{2})(Q1|H1|Q3)?", period)
    if not match:
        return period
    labels = {"Q1": "1분기", "H1": "상반기", "Q3": "3분기", None: "연간"}
    return f"{match.group(1)}년 {labels[match.group(2)]}"
