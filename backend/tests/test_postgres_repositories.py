from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from samnik_backend.domain import Company, ConversationRecord, FsDiv, Metric, ValueScope
from samnik_backend.postgres import (
    PostgresConversationRepository,
    PostgresEvidenceRepository,
    PostgresFinancialRepository,
)


class FakeConnection:
    def __init__(self, row=None, rows=None) -> None:
        self.row = row
        self.rows = rows or []
        self.fetchrow_calls: list[tuple[str, tuple]] = []
        self.fetch_calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql: str, *args):
        self.fetchrow_calls.append((sql, args))
        return self.row

    async def fetch(self, sql: str, *args):
        self.fetch_calls.append((sql, args))
        return self.rows


class FakeAcquire:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self.connection

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class FakePool:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)


@pytest.mark.asyncio
async def test_financial_repository_rejects_unsupported_period_before_sql() -> None:
    synthetic_period = "2025' synthetic boundary input"
    as_of = datetime(2026, 7, 19, tzinfo=UTC)
    connection = FakeConnection(row=None)
    repository = PostgresFinancialRepository(FakePool(connection))

    result = await repository.get_fact(
        company=Company.SAMSUNG_ELECTRONICS,
        metric=Metric.OPERATING_PROFIT,
        period=synthetic_period,
        value_scope=ValueScope.CUMULATIVE,
        fs_div=FsDiv.CFS,
        as_of=as_of,
    )

    assert result is None
    assert connection.fetchrow_calls == []


@pytest.mark.asyncio
async def test_financial_repository_maps_a_parameterized_row_to_domain_fact() -> None:
    as_of = datetime(2026, 7, 19, tzinfo=UTC)
    connection = FakeConnection(
        row={
            "corp_code": "00126380",
            "metric_key": "operating_profit",
            "amount_numeric": "43601051000000",
            "currency": "KRW",
            "business_year": 2025,
            "reprt_code": "11011",
            "fs_div": "CFS",
            "row_hash": "fact:samsung:operating-profit:2025:cfs",
            "raw_amount": "43,601,051,000,000",
            "unit_scale": 1,
            "period_start": datetime(2025, 1, 1, tzinfo=UTC).date(),
            "period_end": datetime(2025, 12, 31, tzinfo=UTC).date(),
            "sj_div": "IS",
        }
    )
    repository = PostgresFinancialRepository(FakePool(connection))

    fact = await repository.get_fact(
        company=Company.SAMSUNG_ELECTRONICS,
        metric=Metric.OPERATING_PROFIT,
        period="2025",
        value_scope=ValueScope.CUMULATIVE,
        fs_div=FsDiv.CFS,
        as_of=as_of,
    )

    assert fact is not None
    assert fact.company == Company.SAMSUNG_ELECTRONICS
    assert fact.value == 43_601_051_000_000
    assert fact.evidence_id == "fact:samsung:operating-profit:2025:cfs"
    assert fact.source_raw_amount == "43,601,051,000,000"
    assert fact.period_start.isoformat() == "2025-01-01"
    assert fact.period_end.isoformat() == "2025-12-31"
    assert fact.report_code == "11011"
    assert fact.section == "IS"
    sql, args = connection.fetchrow_calls[0]
    assert "OFFICIAL_API_VERIFIED" in sql
    assert all(f"${index}" in sql for index in (1, 2, 3, 4, 5, 6, 7))
    assert "$8" not in sql
    assert args == (
        "00126380",
        "operating_profit",
        2025,
        "11011",
        "CFS",
        as_of,
        "CUMULATIVE",
    )


@pytest.mark.asyncio
async def test_evidence_repository_selects_snapshot_scoped_to_requested_company() -> None:
    as_of = datetime(2026, 7, 19, tzinfo=UTC)
    connection = FakeConnection(row=None)
    repository = PostgresEvidenceRepository(FakePool(connection))

    result = await repository.search(
        company=Company.SK_HYNIX,
        query_text="SK하이닉스 영업이익",
        embedding=[0.0] * 384,
        as_of=as_of,
        limit=5,
    )

    assert result.corpus_complete is False
    sql, args = connection.fetchrow_calls[0]
    assert "JOIN filing" in sql
    assert "f.corp_code = $2" in sql
    assert "f.business_year = $3" in sql
    assert "f.reprt_code = $4" in sql
    assert "WHEN 'FULL_TEXT' THEN 0" in sql
    assert "si.snapshot_id DESC" in sql
    assert args == (as_of, "00164779", None, None)


@pytest.mark.asyncio
async def test_evidence_repository_binds_period_receipt_and_embedding_model() -> None:
    as_of = datetime(2026, 7, 19, tzinfo=UTC)
    connection = FakeConnection(
        row={
            "snapshot_id": "a" * 64,
            "acquired_at": as_of,
            "narrative_corpus_status": "FULL_TEXT",
            "narrative_covered_files": 2,
            "narrative_total_files": 2,
            "rcept_no": "20260310002820",
            "business_year": 2025,
            "reprt_code": "11011",
        },
        rows=[],
    )
    repository = PostgresEvidenceRepository(FakePool(connection))

    result = await repository.search(
        company=Company.SAMSUNG_ELECTRONICS,
        period="2025",
        query_text="삼성전자 DS 부문 영업이익",
        embedding=[0.0] * 384,
        as_of=as_of,
        limit=5,
    )

    assert result.corpus_complete is True
    assert result.searched_filing_ids == ["20260310002820"]
    assert connection.fetchrow_calls[0][1] == (
        as_of,
        "00126380",
        2025,
        "11011",
    )
    search_sql, search_args = connection.fetch_calls[0]
    assert "sc.rcept_no = $3" in search_sql
    assert "sc.embedding_model = $6" in search_sql
    assert search_args[0:3] == (
        "a" * 64,
        "00126380",
        "20260310002820",
    )
    assert search_args[5:] == ("intfloat/multilingual-e5-small", 5)


@pytest.mark.asyncio
async def test_evidence_repository_get_by_ids_uses_financial_statement_line_union_chunks() -> None:
    connection = FakeConnection(
        rows=[
            {
                "evidence_id": "fact:samsung:operating-profit:2025:cfs",
                "corp_code": "00126380",
                "business_year": 2025,
                "rcept_no": "20260310002820",
                "report_name": "삼성전자 2025 사업보고서",
                "filing_date": date(2026, 3, 10),
                "source_url": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260310002820",
                "excerpt": None,
                "section_path": "IS",
                "source_kind": "FINANCIAL_FACT",
            },
            {
                "evidence_id": "chunk:20260310002820:1",
                "corp_code": "00126380",
                "business_year": 2025,
                "rcept_no": "20260310002820",
                "report_name": "삼성전자 2025 사업보고서",
                "filing_date": date(2026, 3, 10),
                "source_url": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260310002820",
                "excerpt": "요약된 본문",
                "section_path": "요약",
                "source_kind": "FILING_CHUNK",
            },
        ]
    )
    repository = PostgresEvidenceRepository(FakePool(connection))

    result = await repository.get_by_ids(["fact:samsung:operating-profit:2025:cfs", "chunk:20260310002820:1"])

    assert len(result) == 2
    assert {item.source_kind for item in result} == {"FINANCIAL_FACT", "FILING_CHUNK"}
    sql, _ = connection.fetch_calls[0]
    assert "financial_statement_line" in sql
    assert "source_chunk" in sql
    assert "UNION ALL" in sql


@pytest.mark.asyncio
async def test_conversation_repository_decodes_asyncpg_jsonb_string() -> None:
    record = ConversationRecord(conversation_id="conversation-jsonb")
    connection = FakeConnection(row={"state": record.model_dump_json()})
    repository = PostgresConversationRepository(FakePool(connection))

    restored = await repository.get(record.conversation_id)

    assert restored == record
