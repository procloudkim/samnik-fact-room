from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files
from typing import Any

from .domain import Company


_DICTIONARY_RESOURCE = "data/company_context_dictionary_2015_2025.json"


@lru_cache(maxsize=1)
def load_company_context_dictionary() -> dict[str, Any]:
    """Load the deterministic annual-report context dictionary bundled with the API."""

    resource = files("samnik_backend").joinpath(_DICTIONARY_RESOURCE)
    return json.loads(resource.read_text(encoding="utf-8"))


def company_context_terms(
    company: Company,
    lens_key: str,
    *,
    limit: int = 12,
) -> tuple[str, ...]:
    """Return bounded, corpus-observed terms for one company and analysis lens."""

    if limit <= 0:
        return ()
    payload = load_company_context_dictionary()
    company_payload = payload.get("companies", {}).get(company.value, {})
    terms = company_payload.get("query_expansion_by_lens", {}).get(lens_key, [])
    return tuple(str(term) for term in terms[:limit] if str(term).strip())


def company_context_coverage(company: Company) -> tuple[int, ...]:
    payload = load_company_context_dictionary()
    years = (
        payload.get("companies", {})
        .get(company.value, {})
        .get("available_years", [])
    )
    return tuple(int(year) for year in years)
