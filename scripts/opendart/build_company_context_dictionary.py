#!/usr/bin/env python3
"""Build a governed company-context dictionary from official annual reports.

The output is intentionally deterministic and model-free. Candidate concepts are
assigned to one MECE category, then retained only when they actually occur in the
selected 2015-2025 annual-report corpus. Each retained term carries year coverage,
frequency, and example section paths so the dictionary remains auditable.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncpg

from scripts.opendart.backfill import load_dotenv_key


YEAR_START = 2015
YEAR_END = 2025
REPORT_CODE = "11011"
DEFAULT_OUTPUT = Path(
    "backend/src/samnik_backend/data/company_context_dictionary_2015_2025.json"
)

COMPANIES = {
    "00126380": {
        "key": "SAMSUNG_ELECTRONICS",
        "name": "삼성전자",
        "specific_terms": {
            "BUSINESS_PORTFOLIO": (
                "DX부문", "DS부문", "SDC", "Harman", "MX사업부", "VD사업부",
                "생활가전", "네트워크", "메모리", "System LSI", "Foundry",
                "파운드리", "스마트폰", "TV", "디스플레이 패널", "OLED",
                "DRAM", "NAND", "HBM", "Exynos", "Galaxy",
            ),
            "OPERATIONS_CAPABILITIES": (
                "평택", "화성", "기흥", "온양", "수원", "구미", "베트남",
                "반도체 생산능력", "디스플레이 생산능력",
            ),
        },
    },
    "00164779": {
        "key": "SK_HYNIX",
        "name": "SK하이닉스",
        "specific_terms": {
            "BUSINESS_PORTFOLIO": (
                "DRAM", "NAND Flash", "NAND", "HBM", "eSSD", "SSD", "CIS",
                "메모리 반도체", "Solidigm", "솔리다임", "MCP", "LPDDR",
                "DDR5", "QLC", "기업용 SSD",
            ),
            "OPERATIONS_CAPABILITIES": (
                "이천", "청주", "우시", "다롄", "M15", "M16", "M15X",
                "용인 반도체 클러스터", "생산능력", "미세공정",
            ),
        },
    },
}

TAXONOMY: dict[str, dict[str, Any]] = {
    "BUSINESS_PORTFOLIO": {
        "label": "사업 포트폴리오",
        "terms": (
            "사업부문", "제품", "서비스", "반도체", "메모리", "비메모리",
            "DRAM", "NAND", "HBM", "파운드리", "디스플레이", "스마트폰",
            "가전", "서버", "데이터센터", "인공지능", "AI",
        ),
    },
    "OPERATIONS_CAPABILITIES": {
        "label": "운영·기술 역량",
        "terms": (
            "생산능력", "생산실적", "가동률", "설비", "공장", "공정",
            "수율", "연구개발", "연구개발비", "특허", "품질", "공급망",
            "원재료", "협력사", "조달", "재고관리", "양산",
        ),
    },
    "CUSTOMER_MARKET": {
        "label": "고객·시장",
        "terms": (
            "주요 고객", "매출처", "고객", "시장점유율", "시장", "수요",
            "공급", "판매", "수출", "내수", "지역별", "국내", "해외",
            "중국", "미국", "유럽", "아시아", "경쟁", "가격",
        ),
    },
    "FINANCIAL_DRIVERS": {
        "label": "재무 동인",
        "terms": (
            "매출액", "영업이익", "당기순이익", "원가", "판매비와관리비",
            "재고자산", "재고자산평가충당금", "평가충당금", "매출채권",
            "대손충당금", "기대신용손실", "매입채무", "운전자본",
            "영업활동현금흐름", "현금흐름", "환율", "이자율", "손상차손",
        ),
    },
    "CAPITAL_ALLOCATION": {
        "label": "자본배분",
        "terms": (
            "설비투자", "시설투자", "CAPEX", "투자", "배당", "배당금",
            "주주환원", "자기주식", "자사주", "소각", "차입금", "사채",
            "자금조달", "유상증자", "전환사채", "신주인수권", "인수합병",
        ),
    },
    "RISK_GOVERNANCE": {
        "label": "리스크·거버넌스",
        "terms": (
            "시장위험", "신용위험", "유동성위험", "환율변동위험",
            "이자율변동위험", "파생상품", "우발부채", "채무보증", "소송",
            "규제", "내부통제", "내부회계", "핵심감사사항", "감사의견",
            "회계추정", "공정가치", "이사회", "감사위원회", "준법",
        ),
    },
    "PEOPLE_SUSTAINABILITY": {
        "label": "인적자본·지속가능성",
        "terms": (
            "임직원", "종업원", "인재", "교육", "안전", "산업재해",
            "환경", "온실가스", "탄소", "에너지", "용수", "폐기물",
            "지속가능경영", "사회공헌", "인권", "다양성", "ESG",
        ),
    },
}

LENS_TERMS = {
    "FINANCIAL_RESILIENCE": (
        "유동성위험", "신용위험", "차입금", "사채", "채무보증", "우발부채",
        "현금흐름", "자금조달", "이자율변동위험", "환율변동위험",
    ),
    "WORKING_CAPITAL": (
        "재고자산", "재고자산평가충당금", "평가충당금", "매출채권",
        "대손충당금", "기대신용손실", "매입채무", "운전자본",
        "영업활동현금흐름",
    ),
    "EARNINGS_QUALITY": (
        "영업이익", "당기순이익", "영업활동현금흐름", "손상차손",
        "공정가치", "회계추정", "원가", "판매비와관리비",
    ),
    "DILUTION": (
        "유상증자", "전환사채", "신주인수권", "자기주식", "자사주", "소각",
    ),
    "CAPITAL_ALLOCATION": (
        "설비투자", "시설투자", "CAPEX", "연구개발비", "배당", "배당금",
        "주주환원", "자기주식", "자사주", "소각", "자금조달",
    ),
    "AUDIT_ACCOUNTING": (
        "핵심감사사항", "감사의견", "내부회계", "내부통제", "회계추정",
        "공정가치", "손상차손",
    ),
    "CONCENTRATION": (
        "사업부문", "주요 고객", "매출처", "시장점유율", "지역별", "원재료",
        "협력사", "공급망",
    ),
}


@dataclass
class TermEvidence:
    occurrence_count: int = 0
    years: set[int] = field(default_factory=set)
    sections: list[str] = field(default_factory=list)


def _unique_terms(*groups: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(term for group in groups for term in group))


def _count_occurrences(text_lower: str, term: str) -> int:
    return text_lower.count(term.lower())


def _selected_annual_chunks_sql() -> str:
    return """
        WITH selected AS (
            SELECT DISTINCT ON (f.corp_code, f.business_year)
                   f.corp_code, f.business_year, f.snapshot_id, f.rcept_no,
                   f.filing_date, f.report_name
            FROM filing f
            JOIN snapshot_import si ON si.snapshot_id = f.snapshot_id
            LEFT JOIN filing_revision fr
              ON fr.snapshot_id = f.snapshot_id AND fr.rcept_no = f.rcept_no
            WHERE f.reprt_code = $1
              AND f.business_year BETWEEN $2 AND $3
              AND f.corp_code = ANY($4::bpchar[])
              AND f.evidence_status = 'OFFICIAL_API_VERIFIED'
              AND EXISTS (
                  SELECT 1 FROM source_chunk present
                  WHERE present.snapshot_id = f.snapshot_id
                    AND present.rcept_no = f.rcept_no
                    AND present.evidence_status = 'OFFICIAL_API_VERIFIED'
              )
            ORDER BY f.corp_code, f.business_year,
                     COALESCE(fr.selected_latest, false) DESC,
                     f.filing_date DESC, si.acquired_at DESC, f.snapshot_id DESC
        )
        SELECT s.corp_code::text AS corp_code, s.business_year,
               s.snapshot_id::text AS snapshot_id, s.rcept_no::text AS rcept_no,
               s.filing_date, s.report_name, sc.chunk_id::text AS chunk_id,
               sc.section_path, sc.content
        FROM selected s
        JOIN source_chunk sc
          ON sc.snapshot_id = s.snapshot_id AND sc.rcept_no = s.rcept_no
        WHERE sc.evidence_status = 'OFFICIAL_API_VERIFIED'
        ORDER BY s.corp_code, s.business_year, sc.chunk_id
    """


async def build_dictionary(database_url: str) -> dict[str, Any]:
    connection = await asyncpg.connect(database_url)
    try:
        corpus: dict[str, dict[str, Any]] = {}
        for corp_code, metadata in COMPANIES.items():
            category_terms = {
                key: _unique_terms(
                    tuple(category["terms"]),
                    tuple(metadata.get("specific_terms", {}).get(key, ())),
                )
                for key, category in TAXONOMY.items()
            }
            corpus[corp_code] = {
                "metadata": metadata,
                "category_terms": category_terms,
                "term_evidence": {
                    key: {term: TermEvidence() for term in terms}
                    for key, terms in category_terms.items()
                },
                "available_years": set(),
                "receipts": {},
                "chunk_count": 0,
                "source_hasher": hashlib.sha256(),
            }

        async with connection.transaction():
            cursor = connection.cursor(
                _selected_annual_chunks_sql(),
                REPORT_CODE,
                YEAR_START,
                YEAR_END,
                list(COMPANIES),
                prefetch=100,
            )
            async for row in cursor:
                corp_code = row["corp_code"].strip()
                state = corpus[corp_code]
                year = int(row["business_year"])
                section = str(row["section_path"])
                content = str(row["content"])
                text_lower = f"{section}\n{content}".lower()
                state["available_years"].add(year)
                state["receipts"][year] = row["rcept_no"].strip()
                state["chunk_count"] += 1
                state["source_hasher"].update(
                    f"{row['snapshot_id'].strip()}|{row['chunk_id'].strip()}\n".encode()
                )
                for category_key, terms in state["category_terms"].items():
                    for term in terms:
                        count = _count_occurrences(text_lower, term)
                        if count == 0:
                            continue
                        evidence = state["term_evidence"][category_key][term]
                        evidence.occurrence_count += count
                        evidence.years.add(year)
                        if section not in evidence.sections and len(evidence.sections) < 3:
                            evidence.sections.append(section)

        output_companies: dict[str, Any] = {}
        expected_years = list(range(YEAR_START, YEAR_END + 1))
        for corp_code, state in corpus.items():
            metadata = state["metadata"]
            categories: dict[str, Any] = {}
            observed_by_term: dict[str, TermEvidence] = {}
            for category_key, category in TAXONOMY.items():
                retained = []
                for term, evidence in state["term_evidence"][category_key].items():
                    if evidence.occurrence_count == 0:
                        continue
                    observed_by_term[term] = evidence
                    years = sorted(evidence.years)
                    retained.append(
                        {
                            "term": term,
                            "occurrence_count": evidence.occurrence_count,
                            "document_year_count": len(years),
                            "first_year": years[0],
                            "last_year": years[-1],
                            "years": years,
                            "example_sections": evidence.sections,
                        }
                    )
                retained.sort(
                    key=lambda item: (
                        -item["document_year_count"],
                        -item["occurrence_count"],
                        item["term"].lower(),
                    )
                )
                categories[category_key] = {
                    "label": category["label"],
                    "keywords": retained,
                }

            expansions = {}
            for lens_key, candidate_terms in LENS_TERMS.items():
                observed = [
                    term for term in candidate_terms if term in observed_by_term
                ]
                observed.sort(
                    key=lambda term: (
                        -len(observed_by_term[term].years),
                        -observed_by_term[term].occurrence_count,
                        candidate_terms.index(term),
                    )
                )
                expansions[lens_key] = observed[:12]

            available_years = sorted(state["available_years"])
            output_companies[metadata["key"]] = {
                "corp_code": corp_code,
                "corp_name": metadata["name"],
                "available_years": available_years,
                "missing_years": [
                    year for year in expected_years if year not in state["available_years"]
                ],
                "receipt_by_year": {
                    str(year): state["receipts"][year]
                    for year in sorted(state["receipts"])
                },
                "chunk_count": state["chunk_count"],
                "source_fingerprint_sha256": state["source_hasher"].hexdigest(),
                "categories": categories,
                "query_expansion_by_lens": expansions,
            }

        return {
            "schema_version": "1.0",
            "methodology": "GOVERNED_TERM_OCCURRENCE_V1",
            "source_scope": {
                "source_system": "OpenDART",
                "report_code": REPORT_CODE,
                "year_start": YEAR_START,
                "year_end": YEAR_END,
                "evidence_status": "OFFICIAL_API_VERIFIED",
                "selection": "latest annual filing snapshot with official source chunks",
            },
            "taxonomy": {
                key: {"label": value["label"]} for key, value in TAXONOMY.items()
            },
            "companies": output_companies,
        }
    finally:
        await connection.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build the 2015-2025 annual-report company context dictionary"
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


async def _main() -> None:
    args = _parser().parse_args()
    database_url = os.environ.get("DATABASE_URL", "") or load_dotenv_key(
        Path(".env"), "DATABASE_URL"
    )
    if not database_url:
        raise SystemExit("DATABASE_URL is not configured")
    payload = await build_dictionary(database_url)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    args.output.write_text(rendered, encoding="utf-8")
    for company, details in payload["companies"].items():
        print(
            f"{company}: years={len(details['available_years'])} "
            f"chunks={details['chunk_count']} missing={details['missing_years']}"
        )
    print(f"wrote {args.output}")


if __name__ == "__main__":
    asyncio.run(_main())
