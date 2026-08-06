from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts.opendart.load_snapshot_v2 import (
    LoadV2Error,
    _coverage_for_v2,
    _snapshot_narrative_status,
    _snapshot_id,
    load_no_filing_coverage,
    load_snapshot_v2,
)
from scripts.opendart.sync import canonical_json_bytes, sha256_bytes


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
    "period_start": "2025-01-01",
    "period_end": "2025-12-31",
    "filing_date": "2026-03-10",
    "source_url": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260310002820",
}
SNAPSHOT_BASE = {
    "schema_version": "2.0",
    "dataset": "opendart-filing",
    "immutable": True,
    "provenance": {
        "source_system": "OpenDART",
        "acquisition_mode": "LIVE_API",
        "evidence_status": "OFFICIAL_API_VERIFIED",
        "acquired_at": "2026-07-21T00:00:00+00:00",
        "selection_policy": "latest_non_withdrawn_receipt",
    },
    "companies": [COMPANY],
    "filings": [FILING],
    "revisions": [
        {
            "rcept_no": FILING["rcept_no"],
            "report_name": FILING["report_name"],
            "filing_date": FILING["filing_date"],
            "correction_kind": "ORIGINAL",
            "selected_latest": True,
            "source_url": FILING["source_url"],
            "raw": {
                "corp_code": FILING["corp_code"],
                "rcept_dt": "20260310",
                "report_nm": FILING["report_name"],
            },
        }
    ],
    "coverage": [
        {
            "fs_div": "CFS",
            "status": "LOADED",
            "api_status_code": "000",
            "source_row_count": 1,
            "loaded_row_count": 1,
        },
        {
            "fs_div": "OFS",
            "status": "LOADED",
            "api_status_code": "000",
            "source_row_count": 1,
            "loaded_row_count": 1,
        },
    ],
    "limitations": ["test fixture for v2 load"],
}


def _document_archive() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("x/report.xml", "<report>official</report>")
    return buffer.getvalue()


def _statement_rows() -> list[dict[str, object]]:
    base = {
        "rcept_no": FILING["rcept_no"],
        "corp_code": FILING["corp_code"],
        "business_year": FILING["business_year"],
        "reprt_code": FILING["reprt_code"],
        "account_id": "ifrs-full_Assets",
        "account_nm": "자산총계",
        "account_detail": None,
        "ord": 1,
        "currency": "KRW",
        "thstrm_nm": "2025.12",
        "thstrm_amount_raw": "123",
        "thstrm_amount": 123,
        "thstrm_add_amount_raw": None,
        "thstrm_add_amount": None,
        "frmtrm_nm": None,
        "frmtrm_amount_raw": None,
        "frmtrm_amount": None,
        "frmtrm_q_nm": None,
        "frmtrm_q_amount_raw": None,
        "frmtrm_q_amount": None,
        "frmtrm_add_amount_raw": None,
        "frmtrm_add_amount": None,
        "bfefrmtrm_nm": None,
        "bfefrmtrm_amount_raw": None,
        "bfefrmtrm_amount": None,
        "raw": {"_fixture": True},
    }
    cfs = dict(base)
    cfs.update({"fs_div": "CFS", "sj_div": "BS", "row_hash": "0" * 64})
    ofs = dict(base)
    ofs.update({"fs_div": "OFS", "sj_div": "IS", "row_hash": "1" * 64})
    return [cfs, ofs]


def _build_snapshot(snapshot_root: Path, include_chunks: bool = False, chunk_status: str = "FULL_TEXT") -> Path:
    statement_bytes = b"\n".join(canonical_json_bytes(row) for row in _statement_rows())
    document_payload = _document_archive()
    files = [
        {
            "path": "raw/list.json",
            "media_type": "application/json",
            "sha256": sha256_bytes(canonical_json_bytes({"status": "000", "message": "ok"})),
            "size_bytes": len(canonical_json_bytes({"status": "000", "message": "ok"})),
            "origin": "OPENDART_API",
        },
        {
            "path": "raw/fnlttSinglAcntAll_CFS.json",
            "media_type": "application/json",
            "sha256": sha256_bytes(canonical_json_bytes({"status": "000"})),
            "size_bytes": len(canonical_json_bytes({"status": "000"})),
            "origin": "OPENDART_API",
        },
        {
            "path": "raw/fnlttSinglAcntAll_OFS.json",
            "media_type": "application/json",
            "sha256": sha256_bytes(canonical_json_bytes({"status": "000"})),
            "size_bytes": len(canonical_json_bytes({"status": "000"})),
            "origin": "OPENDART_API",
        },
        {
            "path": "normalized/financial_statement_lines.jsonl",
            "media_type": "application/json",
            "sha256": sha256_bytes(statement_bytes),
            "size_bytes": len(statement_bytes),
            "origin": "LOCAL_DERIVATION",
        },
        {
            "path": "raw/document.zip",
            "media_type": "application/zip",
            "sha256": sha256_bytes(document_payload),
            "size_bytes": len(document_payload),
            "origin": "OPENDART_API",
        },
        {
            "path": "normalized/document_index.json",
            "media_type": "application/json",
            "sha256": sha256_bytes(canonical_json_bytes({"rcept_no": FILING["rcept_no"], "files": ["x/report.xml"]})),
            "size_bytes": len(canonical_json_bytes({"rcept_no": FILING["rcept_no"], "files": ["x/report.xml"]})),
            "origin": "LOCAL_DERIVATION",
        },
    ]

    if include_chunks:
        chunks_payload = {
            "schema_version": "1.0",
            "source_snapshot_id": "0" * 64,
            "coverage_status": chunk_status,
            "covered_document_paths": ["x/report.xml"] if chunk_status != "SOURCE_NOT_AVAILABLE" else [],
            "supported_document_paths": ["x/report.xml"],
            "covered_document_files": 1 if chunk_status != "SOURCE_NOT_AVAILABLE" else 0,
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
            "chunks": [],
        }
        payload_bytes = canonical_json_bytes(chunks_payload)
        files.append(
            {
                "path": "normalized/source_chunks.json",
                "media_type": "application/json",
                "sha256": sha256_bytes(payload_bytes),
                "size_bytes": len(payload_bytes),
                "origin": "LOCAL_DERIVATION",
            }
        )
    descriptor = {**SNAPSHOT_BASE, "files": files}
    snapshot_id = _snapshot_id(descriptor)
    descriptor["snapshot_id"] = snapshot_id

    snapshot_dir = snapshot_root / snapshot_id
    snapshot_dir.mkdir(parents=True)
    (snapshot_dir / "manifest.json").write_bytes(canonical_json_bytes(descriptor))

    (snapshot_dir / "raw").mkdir(parents=True, exist_ok=True)
    (snapshot_dir / "raw/list.json").write_bytes(
        canonical_json_bytes({"status": "000", "message": "ok"})
    )
    (snapshot_dir / "raw/fnlttSinglAcntAll_CFS.json").write_bytes(
        canonical_json_bytes({"status": "000"})
    )
    (snapshot_dir / "raw/fnlttSinglAcntAll_OFS.json").write_bytes(
        canonical_json_bytes({"status": "000"})
    )
    (snapshot_dir / "raw/document.zip").write_bytes(document_payload)

    (snapshot_dir / "normalized").mkdir(parents=True, exist_ok=True)
    (snapshot_dir / "normalized/document_index.json").write_bytes(
        canonical_json_bytes({"rcept_no": FILING["rcept_no"], "files": ["x/report.xml"]})
    )
    (snapshot_dir / "normalized/financial_statement_lines.jsonl").write_bytes(statement_bytes)
    if include_chunks:
        (snapshot_dir / "normalized/source_chunks.json").write_bytes(payload_bytes)
    return snapshot_dir


class FakeCursor:
    def __init__(self, source_chunk_revision_fk: bool = False) -> None:
        self.source_chunk_revision_fk = source_chunk_revision_fk
        self.last_query = ""
        self.executions: list[tuple[str, tuple | None]] = []

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple | None = None) -> None:
        self.last_query = " ".join(sql.split())
        self.executions.append((self.last_query, params))

    def fetchone(self) -> tuple[object, ...] | None:
        if "information_schema.columns" in self.last_query:
            return (1,) if self.source_chunk_revision_fk else None
        if "SELECT filing_revision_id" in self.last_query:
            return ("42",)
        return None


class FakeConnection:
    def __init__(self, source_chunk_revision_fk: bool = False) -> None:
        self.cursor_instance = FakeCursor(source_chunk_revision_fk)
        self.closed = False

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def cursor(self) -> FakeCursor:
        return self.cursor_instance

    def close(self) -> None:
        self.closed = True


def _fake_source_chunks(_payload: dict, _manifest: dict) -> list[dict[str, object]]:
    return [
        {
            "chunk_id": "0" * 64,
            "rcept_no": FILING["rcept_no"],
            "corp_code": FILING["corp_code"],
            "business_year": FILING["business_year"],
            "section_path": "x/report.xml > section",
            "chunk_no": 0,
            "content": "정규화 청크",
            "content_sha256": sha256_bytes("정규화 청크".encode("utf-8")),
            "embedding": [1.0] + [0.0] * 383,
            "source_url": FILING["source_url"],
        }
    ]


class LoadSnapshotV2Tests(unittest.TestCase):
    def test_load_no_filing_coverage_writes_cfs_ofs_rows(self) -> None:
        connection = FakeConnection()
        result = load_no_filing_coverage(
            database_url="postgresql://offline",
            corp_code=FILING["corp_code"],
            business_year=FILING["business_year"],
            report_code=FILING["reprt_code"],
            status="NOT_FILED",
            connect_factory=lambda _: connection,
        )
        inserts = [
            entry for entry in connection.cursor_instance.executions if "INSERT INTO data_coverage" in entry[0]
        ]
        self.assertEqual(result["coverage"], 2)
        self.assertEqual(len(inserts), 2)
        for statement, params in inserts:
            self.assertEqual(params[0], None)
            self.assertEqual(params[7], None)
            self.assertIn(params[4], {"CFS", "OFS"})
            self.assertEqual(params[8], 0)
            self.assertEqual(params[9], 0)
            self.assertEqual(params[5], "NOT_FILED")

    def test_coverage_for_v2_reads_status_from_chunks(self) -> None:
        manifest = {"files": [{"path": "normalized/source_chunks.json"}]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "normalized/source_chunks.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "coverage_status": "PARTIAL_TEXT",
                        "covered_document_files": 2,
                        "total_supported_document_files": 3,
                    }
                ),
                encoding="utf-8",
            )
            status, covered, retrievable = _coverage_for_v2(manifest, root, path)
        self.assertEqual(status, "PARTIAL_TEXT")
        self.assertEqual(covered, 2)
        self.assertEqual(retrievable, 3)

    def test_snapshot_narrative_status_falls_back_to_source_not_available(self) -> None:
        self.assertEqual(_snapshot_narrative_status("NOT_BUILT"), "SOURCE_NOT_AVAILABLE")
        self.assertEqual(_snapshot_narrative_status(""), "SOURCE_NOT_AVAILABLE")

    def test_load_snapshot_v2_without_revision_fk_column_uses_legacy_source_chunk_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot = _build_snapshot(Path(directory), include_chunks=True)
            connection = FakeConnection(source_chunk_revision_fk=False)
            with patch("scripts.opendart.load_snapshot_v2._validate_source_chunks_v2", _fake_source_chunks):
                result = load_snapshot_v2(
                    snapshot, "postgresql://offline", connect_factory=lambda _: connection
                )
        chunk_inserts = [
            sql for sql, _ in connection.cursor_instance.executions if "INSERT INTO source_chunk" in sql
        ]
        self.assertEqual(result["chunks"], 1)
        self.assertEqual(len(chunk_inserts), 1)
        self.assertIn("chunk_id, snapshot_id, rcept_no", chunk_inserts[0])
        self.assertNotIn("filing_revision_id", chunk_inserts[0])
        self.assertTrue(connection.closed)

    def test_load_snapshot_v2_with_revision_fk_column_uses_new_source_chunk_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot = _build_snapshot(Path(directory), include_chunks=True)
            connection = FakeConnection(source_chunk_revision_fk=True)
            with patch("scripts.opendart.load_snapshot_v2._validate_source_chunks_v2", _fake_source_chunks):
                result = load_snapshot_v2(
                    snapshot, "postgresql://offline", connect_factory=lambda _: connection
                )
        chunk_inserts = [
            sql for sql, _ in connection.cursor_instance.executions if "INSERT INTO source_chunk" in sql
        ]
        self.assertEqual(result["chunks"], 1)
        self.assertEqual(len(chunk_inserts), 1)
        self.assertIn("filing_revision_id", chunk_inserts[0])

    def test_load_snapshot_v2_rejects_invalid_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot_root = Path(directory) / "bad-snapshot"
            snapshot_root.mkdir()
            (snapshot_root / "manifest.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(LoadV2Error):
                load_snapshot_v2(snapshot_root, "postgresql://offline", connect_factory=lambda _: FakeConnection())


if __name__ == "__main__":
    unittest.main()
