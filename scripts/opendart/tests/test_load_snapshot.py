from __future__ import annotations

import json
import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.opendart.load_snapshot import (
    LoadError,
    load_snapshot,
    validate_financial_fact,
    validate_live_snapshot,
    validate_source_chunks,
)
from scripts.opendart.sync import (
    build_snapshot_id,
    build_source_chunk_id,
    canonical_json_bytes,
    sha256_bytes,
)


COMPANY = {
    "corp_code": "00126380",
    "corp_name": "삼성전자",
    "stock_code": "005930",
}
FILING = {
    "corp_code": "00126380",
    "rcept_no": "20260310002820",
    "report_name": "사업보고서 (2025.12)",
    "business_year": 2025,
    "reprt_code": "11011",
    "fs_div": "CFS",
    "period_start": "2025-01-01",
    "period_end": "2025-12-31",
    "filing_date": "2026-03-10",
    "source_url": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260310002820",
}
FACT = {
    "evidence_id": "fact:samsung:2025:operating_profit",
    "corp_code": "00126380",
    "corp_name": "삼성전자",
    "stock_code": "005930",
    "rcept_no": "20260310002820",
    "business_year": 2025,
    "metric_key": "operating_profit",
    "account_id": "ifrs-full_ProfitLossFromOperatingActivities",
    "account_nm": "영업이익",
    "sj_div": "IS",
    "fs_div": "CFS",
    "period_start": "2025-01-01",
    "period_end": "2025-12-31",
    "value_scope": "CONSOLIDATED_ANNUAL",
    "raw_amount": "43601051000000",
    "amount_numeric": "43601051000000",
    "currency": "KRW",
    "unit_scale": 1,
    "source_field": "thstrm_amount",
    "evidence_status": "OFFICIAL_API_VERIFIED",
    "source_url": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260310002820",
}
PROVENANCE = {
    "source_system": "OpenDART",
    "acquisition_mode": "LIVE_API",
    "evidence_status": "OFFICIAL_API_VERIFIED",
    "acquired_at": "2026-07-19T00:00:00+00:00",
}
LIMITATIONS = ["offline loader fixture"]


def make_document_zip(content: str = "공식 API 문서에서 정규화한 테스트 청크") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "report.xml",
            (
                '<report><title>삼성전자 2025</title>'
                f"<p>{content}</p></report>"
            ).encode("utf-8"),
        )
    return buffer.getvalue()


class FakeCursor:
    def __init__(self) -> None:
        self.executions: list[tuple[str, tuple | None]] = []

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple | None = None) -> None:
        self.executions.append((" ".join(sql.split()), params))


class FakeConnection:
    def __init__(self) -> None:
        self.cursor_instance = FakeCursor()
        self.closed = False

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def cursor(self) -> FakeCursor:
        return self.cursor_instance

    def close(self) -> None:
        self.closed = True


def create_live_snapshot(root: Path, include_chunks: bool = False) -> Path:
    payloads: dict[str, bytes] = {
        "raw/document.zip": make_document_zip(),
        "normalized/financial_fact.json": canonical_json_bytes(FACT),
    }
    parent_files = [
        {
            "path": path,
            "media_type": "application/json" if path.endswith(".json") else "application/zip",
            "sha256": sha256_bytes(payload),
            "size_bytes": len(payload),
            "origin": "OPENDART_API",
        }
        for path, payload in payloads.items()
    ]
    parent_snapshot_id = build_snapshot_id(
        [COMPANY],
        [FILING],
        parent_files,
        provenance=PROVENANCE,
        limitations=LIMITATIONS,
    )
    if include_chunks:
        content = "공식 API 문서에서 정규화한 테스트 청크"
        content_hash = sha256_bytes(content.encode("utf-8"))
        embedding = [1.0] + [0.0] * 383
        embedding_hash = sha256_bytes(canonical_json_bytes(embedding))
        chunk_id = build_source_chunk_id(
            source_snapshot_id=parent_snapshot_id,
            rcept_no=FILING["rcept_no"],
            corp_code=FILING["corp_code"],
            business_year=FILING["business_year"],
            section_path="report.xml > 삼성전자 2025",
            chunk_no=0,
            content_sha256=content_hash,
            embedding_model="intfloat/multilingual-e5-small",
            embedding_sha256=embedding_hash,
        )
        payloads["normalized/source_chunks.json"] = canonical_json_bytes(
            {
                "schema_version": "1.0",
                "source_snapshot_id": parent_snapshot_id,
                "coverage_status": "FULL_TEXT",
                "covered_document_paths": ["report.xml"],
                "supported_document_paths": ["report.xml"],
                "covered_document_files": 1,
                "total_supported_document_files": 1,
                "embedding_model": "intfloat/multilingual-e5-small",
                "embedding_dimensions": 384,
                "embedding_normalized": True,
                "embedding_input_prefix": "passage: ",
                "chunking": {
                    "algorithm": "fixed_unicode_chars_v1",
                    "max_chars": 1200,
                    "overlap_chars": 200,
                },
                "chunks": [
                    {
                        "chunk_id": chunk_id,
                        "rcept_no": FILING["rcept_no"],
                        "corp_code": FILING["corp_code"],
                        "business_year": FILING["business_year"],
                        "section_path": "report.xml > 삼성전자 2025",
                        "chunk_no": 0,
                        "content": content,
                        "content_sha256": content_hash,
                        "embedding": embedding,
                        "embedding_sha256": embedding_hash,
                        "source_url": FILING["source_url"],
                        "evidence_status": "OFFICIAL_API_VERIFIED",
                    }
                ],
            }
        )
    files = [
        {
            "path": path,
            "media_type": "application/json" if path.endswith(".json") else "application/zip",
            "sha256": sha256_bytes(payload),
            "size_bytes": len(payload),
            "origin": (
                "LOCAL_DERIVATION"
                if path == "normalized/source_chunks.json"
                else "OPENDART_API"
            ),
        }
        for path, payload in payloads.items()
    ]
    snapshot_id = build_snapshot_id(
        [COMPANY],
        [FILING],
        files,
        provenance=PROVENANCE,
        limitations=LIMITATIONS,
    )
    manifest = {
        "schema_version": "1.0",
        "snapshot_id": snapshot_id,
        "dataset": "opendart-filing",
        "immutable": True,
        "provenance": PROVENANCE,
        "companies": [COMPANY],
        "filings": [FILING],
        "files": files,
        "limitations": LIMITATIONS,
    }
    target = root / snapshot_id
    target.mkdir()
    for relative, payload in payloads.items():
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    (target / "manifest.json").write_bytes(canonical_json_bytes(manifest))
    return target


class SnapshotLoaderTests(unittest.TestCase):
    def test_accepts_official_first_quarter_cumulative_fact(self) -> None:
        filing = {
            **FILING,
            "rcept_no": "20260515000001",
            "report_name": "분기보고서 (2026.03)",
            "business_year": 2026,
            "reprt_code": "11013",
            "period_start": "2026-01-01",
            "period_end": "2026-03-31",
            "filing_date": "2026-05-15",
            "source_url": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260515000001",
        }
        fact = {
            **FACT,
            "evidence_id": "fact:samsung:2026Q1:operating_profit",
            "rcept_no": filing["rcept_no"],
            "business_year": 2026,
            "period_start": filing["period_start"],
            "period_end": filing["period_end"],
            "value_scope": "CONSOLIDATED_CUMULATIVE",
            "source_url": filing["source_url"],
        }

        validated = validate_financial_fact(fact, {"filings": [filing]})

        self.assertEqual(validated["evidence_id"], fact["evidence_id"])
        self.assertEqual(validated["value_scope"], "CONSOLIDATED_CUMULATIVE")

    def test_snapshot_without_chunks_records_not_built_coverage(self) -> None:
        connection = FakeConnection()
        with tempfile.TemporaryDirectory() as directory:
            snapshot = create_live_snapshot(Path(directory), include_chunks=False)
            result = load_snapshot(
                snapshot,
                "postgresql://offline-unit-test",
                connect_factory=lambda _: connection,
            )
        self.assertEqual(result["source_chunks"], 0)
        self.assertEqual(result["narrative_corpus_status"], "NOT_BUILT")
        self.assertEqual(result["narrative_covered_files"], 0)
        self.assertEqual(result["narrative_total_files"], 0)

    def test_loads_official_snapshot_and_precomputed_chunk_without_network(self) -> None:
        connection = FakeConnection()
        with tempfile.TemporaryDirectory() as directory:
            snapshot = create_live_snapshot(Path(directory), include_chunks=True)
            result = load_snapshot(
                snapshot,
                "postgresql://offline-unit-test",
                connect_factory=lambda _: connection,
            )
        self.assertEqual(result["financial_facts"], 1)
        self.assertEqual(result["source_chunks"], 1)
        self.assertEqual(result["narrative_corpus_status"], "FULL_TEXT")
        statements = "\n".join(sql for sql, _ in connection.cursor_instance.executions)
        self.assertIn("INSERT INTO snapshot_import", statements)
        self.assertIn("INSERT INTO financial_fact", statements)
        self.assertIn("INSERT INTO source_chunk", statements)
        self.assertIn("%s::vector", statements)
        self.assertTrue(connection.closed)

    def test_rejects_repository_synthetic_fixture_before_database_access(self) -> None:
        repository = Path(__file__).resolve().parents[3]
        fixture = repository / "data/fixtures/synthetic"
        with self.assertRaisesRegex(LoadError, "synthetic import refused"):
            validate_live_snapshot(fixture)

    def test_database_failure_masks_database_url(self) -> None:
        secret_url = "postgresql://user:secret-password@localhost/db"
        with tempfile.TemporaryDirectory() as directory:
            snapshot = create_live_snapshot(Path(directory))

            def fail(url: str) -> object:
                raise RuntimeError(url)

            with self.assertRaises(LoadError) as caught:
                load_snapshot(snapshot, secret_url, connect_factory=fail)
        self.assertNotIn("secret-password", str(caught.exception))

    def test_chunk_contract_requires_exactly_384_finite_values(self) -> None:
        content = "test"
        content_hash = sha256_bytes(content.encode("utf-8"))
        invalid_embedding = [0.0] * 383
        invalid_embedding_hash = sha256_bytes(canonical_json_bytes(invalid_embedding))
        source_snapshot_id = "0" * 64
        chunk_id = build_source_chunk_id(
            source_snapshot_id=source_snapshot_id,
            rcept_no=FILING["rcept_no"],
            corp_code=FILING["corp_code"],
            business_year=FILING["business_year"],
            section_path="주석",
            chunk_no=0,
            content_sha256=content_hash,
            embedding_model="intfloat/multilingual-e5-small",
            embedding_sha256=invalid_embedding_hash,
        )
        payload = {
            "schema_version": "1.0",
            "source_snapshot_id": "0" * 64,
            "coverage_status": "FULL_TEXT",
            "covered_document_paths": ["report.xml"],
            "supported_document_paths": ["report.xml"],
            "covered_document_files": 1,
            "total_supported_document_files": 1,
            "embedding_model": "intfloat/multilingual-e5-small",
            "embedding_dimensions": 384,
            "embedding_normalized": True,
            "embedding_input_prefix": "passage: ",
            "chunking": {
                "algorithm": "fixed_unicode_chars_v1",
                "max_chars": 1200,
                "overlap_chars": 200,
            },
            "chunks": [
                {
                    "chunk_id": chunk_id,
                    "rcept_no": FILING["rcept_no"],
                    "corp_code": FILING["corp_code"],
                    "business_year": FILING["business_year"],
                    "section_path": "주석",
                    "chunk_no": 0,
                    "content": content,
                    "content_sha256": content_hash,
                    "embedding": invalid_embedding,
                    "embedding_sha256": invalid_embedding_hash,
                    "source_url": FILING["source_url"],
                    "evidence_status": "OFFICIAL_API_VERIFIED",
                }
            ],
        }
        parent_files = [
            {
                "path": "raw/document.zip",
                "media_type": "application/zip",
                "sha256": "a" * 64,
                "size_bytes": 1,
                "origin": "OPENDART_API",
            }
        ]
        payload["source_snapshot_id"] = build_snapshot_id(
            [COMPANY],
            [FILING],
            parent_files,
            provenance=PROVENANCE,
            limitations=LIMITATIONS,
        )
        chunks_bytes = canonical_json_bytes(payload)
        manifest = {
            "provenance": PROVENANCE,
            "limitations": LIMITATIONS,
            "companies": [COMPANY],
            "filings": [FILING],
            "files": [
                *parent_files,
                {
                    "path": "normalized/source_chunks.json",
                    "media_type": "application/json",
                    "sha256": sha256_bytes(chunks_bytes),
                    "size_bytes": len(chunks_bytes),
                    "origin": "LOCAL_DERIVATION",
                },
            ],
        }
        with self.assertRaisesRegex(LoadError, "384 finite"):
            validate_source_chunks(
                payload,
                manifest,
                document_archive=make_document_zip("test"),
            )


if __name__ == "__main__":
    unittest.main()
