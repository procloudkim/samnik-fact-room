from decimal import Decimal

import pytest

from samnik_backend.formatting import (
    format_korean_krw,
    format_krw_with_approximation,
    format_period_label,
    format_trillion_krw,
    period_sort_key,
)


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        ("43601051000000", "43조 6,010억 5,100만 원"),
        ("1000000000000", "1조 원"),
        ("123456789", "1억 2,345만 6,789원"),
        ("10000", "1만 원"),
        ("1234", "1,234원"),
        ("0", "0원"),
        ("-120000000", "-1억 2,000만 원"),
    ],
)
def test_format_korean_krw_uses_familiar_large_number_units(
    amount: str, expected: str
) -> None:
    assert format_korean_krw(Decimal(amount)) == expected


def test_trillion_display_is_explicitly_approximate() -> None:
    amount = Decimal("43601051000000")

    assert format_trillion_krw(amount) == "약 43.60조 원"
    assert format_krw_with_approximation(amount) == (
        "43조 6,010억 5,100만 원(약 43.60조 원)"
    )


def test_format_korean_krw_rejects_fractional_won() -> None:
    with pytest.raises(ValueError, match="integer"):
        format_korean_krw(Decimal("1.5"))


def test_interim_periods_are_friendly_and_chronological() -> None:
    assert format_period_label("2026Q1") == "2026년 1분기"
    assert format_period_label("2026H1") == "2026년 상반기"
    assert format_period_label("2026Q3") == "2026년 3분기"
    assert max(["2026Q1", "2026H1", "2025"], key=period_sort_key) == "2026H1"
