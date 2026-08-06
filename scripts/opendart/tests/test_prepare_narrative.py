from __future__ import annotations

import hashlib
import io
import json
import math
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts.opendart.load_snapshot import (
    LoadError,
    load_snapshot,
    validate_live_snapshot,
    validate_source_chunks,
)
from scripts.opendart.prepare_narrative import (
    EMBEDDING_MODEL,
    PreparationError,
    chunk_sections,
    extract_markup_sections,
    prepare_narrative_snapshot,
)
from scripts.opendart.sync import (
    build_snapshot_id,
    build_source_chunk_id,
    canonical_json_bytes,
    sha256_bytes,
)
from scripts.opendart.snapshot_v2 import _snapshot_id


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
    "source_url": FILING["source_url"],
}
PROVENANCE = {
    "source_system": "OpenDART",
    "acquisition_mode": "LIVE_API",
    "evidence_status": "OFFICIAL_API_VERIFIED",
    "acquired_at": "2026-07-19T00:00:00+00:00",
}
LIMITATIONS = ["offline LIVE_API-shaped test snapshot"]


def make_archive(entries: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content.encode("utf-8"))
    return buffer.getvalue()


def valid_archive(body: str | None = None) -> bytes:
    narrative = body or ("삼성전자 2025 사업보고서의 공식 설명입니다. " * 30)
    return make_archive(
        {
            "report.xml": (
                '<?xml version="1.0" encoding="utf-8"?>'
                "<DOCUMENT><SECTION-1><TITLE>삼성전자 2025 사업보고서</TITLE>"
                f"<P>{narrative}</P></SECTION-1></DOCUMENT>"
            ),
            "notes/segment.html": (
                "<html><body><h1>주석</h1><h2>부문 정보</h2>"
                "<p>DS 부문 관련 공식 공시 본문입니다.</p></body></html>"
            ),
            "image.bin": "not narrative text",
        }
    )


def create_input_snapshot(root: Path, archive_bytes: bytes) -> Path:
    payloads = {
        "raw/document.zip": archive_bytes,
        "normalized/financial_fact.json": canonical_json_bytes(FACT),
    }
    files = [
        {
            "path": path,
            "media_type": "application/zip" if path.endswith(".zip") else "application/json",
            "sha256": sha256_bytes(payload),
            "size_bytes": len(payload),
            "origin": "OPENDART_API",
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
    target.mkdir(parents=True)
    for relative, payload in payloads.items():
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    (target / "manifest.json").write_bytes(canonical_json_bytes(manifest))
    return target


def create_input_snapshot_v2(root: Path, archive_bytes: bytes) -> Path:
    coverage = [
        {
            "fs_div": "CFS",
            "status": "LOADED",
            "api_status_code": "000",
            "source_row_count": 0,
            "loaded_row_count": 0,
        },
        {
            "fs_div": "OFS",
            "status": "LOADED",
            "api_status_code": "000",
            "source_row_count": 0,
            "loaded_row_count": 0,
        },
    ]
    revisions = [
        {
            "rcept_no": FILING["rcept_no"],
            "report_name": FILING["report_name"],
            "filing_date": FILING["filing_date"],
            "correction_kind": "ORIGINAL",
            "selected_latest": True,
            "source_url": FILING["source_url"],
            "raw": {"corp_code": FILING["corp_code"], "rcept_dt": "20260310", "report_nm": FILING["report_name"]},
        }
    ]
    payloads = {
        "raw/list.json": canonical_json_bytes({"status": "000"}),
        "raw/fnlttSinglAcntAll_CFS.json": canonical_json_bytes({"status": "000"}),
        "raw/fnlttSinglAcntAll_OFS.json": canonical_json_bytes({"status": "000"}),
        "normalized/financial_statement_lines.jsonl": b"",
        "raw/document.zip": archive_bytes,
        "normalized/document_index.json": canonical_json_bytes(
            {"rcept_no": FILING["rcept_no"], "files": ["report.xml"]}
        ),
    }
    files = []
    for path, payload in payloads.items():
        media_type = "application/zip" if path.endswith(".zip") else "application/json"
        files.append(
            {
                "path": path,
                "media_type": media_type,
                "sha256": sha256_bytes(payload),
                "size_bytes": len(payload),
                "origin": "OPENDART_API" if path.startswith("raw/") else "LOCAL_DERIVATION",
            }
        )
    manifest = {
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
        "revisions": revisions,
        "coverage": coverage,
        "files": files,
        "limitations": ["offline test fixture for v2 narrative preparation"],
    }
    descriptor = {key: manifest[key] for key in manifest if key != "snapshot_id"}
    manifest["snapshot_id"] = _snapshot_id(descriptor)
    target = root / manifest["snapshot_id"]
    target.mkdir(parents=True)
    for relative, payload in payloads.items():
        file_path = target / relative
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(payload)
    (target / "manifest.json").write_bytes(canonical_json_bytes(manifest))
    return target


class DeterministicFakeEmbedder:
    model_name = EMBEDDING_MODEL

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            vector = [0.0] * 384
            vector[int.from_bytes(digest[:2], "big") % 384] = 3.0
            vector[int.from_bytes(digest[2:4], "big") % 384] += 4.0
            vectors.append(vector)
        return vectors


class BadDimensionEmbedder:
    model_name = EMBEDDING_MODEL

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] + [0.0] * 382 for _ in texts]


class AlternateDeterministicEmbedder:
    model_name = EMBEDDING_MODEL

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return [[0.0, 1.0] + [0.0] * 382 for _ in texts]


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


def tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): sha256_bytes(path.read_bytes())
        for path in root.rglob("*")
        if path.is_file()
    }


class NarrativePreparationTests(unittest.TestCase):
    def test_table_cells_preserve_metric_and_value_on_the_same_row(self) -> None:
        sections = extract_markup_sections(
            "notes.xml",
            (
                "<TABLE><TR><TD><P>영업이익</P></TD>"
                "<TD><P>24,858,075</P></TD></TR></TABLE>"
            ).encode("utf-8"),
        )
        self.assertIn("영업이익 | 24,858,075", sections[0].text)

    def test_table_headers_map_each_segment_to_its_own_value(self) -> None:
        sections = extract_markup_sections(
            "notes.xml",
            (
                "<TABLE><TR><TH>segment</TH><TH>DX</TH><TH>DS</TH></TR>"
                "<TR><TD>영업이익</TD><TD>10,000</TD><TD>(5,000)</TD></TR>"
                "</TABLE>"
            ).encode("utf-8"),
        )
        text = sections[0].text
        self.assertIn("segment | DX | DS", text)
        self.assertIn("영업이익 | 10,000 | (5,000)", text)
        self.assertIn("부문=DX | 영업이익=10,000", text)
        self.assertIn("부문=DS | 영업이익=(5,000)", text)

    def test_xml_cdata_is_preserved_as_visible_text(self) -> None:
        sections = extract_markup_sections(
            "report.xml",
            "<DOCUMENT><TITLE>주석</TITLE><![CDATA[삼성전자 2025 CDATA 본문]]></DOCUMENT>".encode(
                "utf-8"
            ),
        )
        self.assertIn("삼성전자 2025 CDATA 본문", sections[0].text)

    def test_deterministic_ids_exact_overlap_and_full_coverage(self) -> None:
        body = " ".join(f"삼성전자공시문장{i:03d}" for i in range(100))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = create_input_snapshot(root / "input", valid_archive(body))
            first = prepare_narrative_snapshot(
                source,
                embedder=DeterministicFakeEmbedder(),
                output_root=root / "out-one",
                max_chars=80,
                overlap_chars=20,
            )
            second = prepare_narrative_snapshot(
                source,
                embedder=DeterministicFakeEmbedder(),
                output_root=root / "out-two",
                max_chars=80,
                overlap_chars=20,
            )
            first_payload = json.loads(
                (first / "normalized/source_chunks.json").read_text(encoding="utf-8")
            )
            second_payload = json.loads(
                (second / "normalized/source_chunks.json").read_text(encoding="utf-8")
            )
            first_manifest = json.loads(
                (first / "manifest.json").read_text(encoding="utf-8")
            )

        self.assertEqual(first.name, second.name)
        self.assertEqual(first_payload, second_payload)
        self.assertEqual(first_payload["coverage_status"], "FULL_TEXT")
        self.assertEqual(first_payload["covered_document_files"], 2)
        self.assertEqual(first_payload["total_supported_document_files"], 2)
        chunk_file = next(
            item
            for item in first_manifest["files"]
            if item["path"] == "normalized/source_chunks.json"
        )
        self.assertEqual(chunk_file["origin"], "LOCAL_DERIVATION")
        report_chunks = [
            item
            for item in first_payload["chunks"]
            if item["section_path"].startswith("report.xml")
        ]
        self.assertGreater(len(report_chunks), 1)
        for left, right in zip(report_chunks, report_chunks[1:]):
            self.assertEqual(left["content"][-20:], right["content"][:20])
        for chunk in first_payload["chunks"]:
            expected = build_source_chunk_id(
                source_snapshot_id=first_payload["source_snapshot_id"],
                rcept_no=chunk["rcept_no"],
                corp_code=chunk["corp_code"],
                business_year=chunk["business_year"],
                section_path=chunk["section_path"],
                chunk_no=chunk["chunk_no"],
                content_sha256=chunk["content_sha256"],
                embedding_model=first_payload["embedding_model"],
                embedding_sha256=chunk["embedding_sha256"],
            )
            self.assertEqual(chunk["chunk_id"], expected)
            norm = math.sqrt(sum(value * value for value in chunk["embedding"]))
            self.assertAlmostEqual(norm, 1.0, places=7)

    def test_different_embedding_artifacts_do_not_reuse_global_chunk_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = create_input_snapshot(root / "input", valid_archive())
            first = prepare_narrative_snapshot(
                source,
                embedder=DeterministicFakeEmbedder(),
                output_root=root / "out-one",
            )
            second = prepare_narrative_snapshot(
                source,
                embedder=AlternateDeterministicEmbedder(),
                output_root=root / "out-two",
            )
            first_payload = json.loads(
                (first / "normalized/source_chunks.json").read_text(encoding="utf-8")
            )
            second_payload = json.loads(
                (second / "normalized/source_chunks.json").read_text(encoding="utf-8")
            )
        self.assertNotEqual(first.name, second.name)
        first_ids = {chunk["chunk_id"] for chunk in first_payload["chunks"]}
        second_ids = {chunk["chunk_id"] for chunk in second_payload["chunks"]}
        self.assertTrue(first_ids.isdisjoint(second_ids))

    def test_input_snapshot_is_byte_for_byte_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = create_input_snapshot(root / "input", valid_archive())
            before = tree_hashes(source)
            prepare_narrative_snapshot(
                source,
                embedder=DeterministicFakeEmbedder(),
                output_root=root / "output",
            )
            after = tree_hashes(source)
        self.assertEqual(before, after)

    def test_rejects_archive_path_traversal(self) -> None:
        archive = make_archive({"../report.xml": "삼성전자 2025 사업보고서"})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = create_input_snapshot(root / "input", archive)
            with self.assertRaisesRegex(PreparationError, "unsafe path"):
                prepare_narrative_snapshot(
                    source,
                    embedder=DeterministicFakeEmbedder(),
                    output_root=root / "output",
                )

    def test_rejects_archive_over_existing_uncompressed_size_limit(self) -> None:
        archive = make_archive(
            {"report.xml": "<report>삼성전자 2025 " + ("가" * 500) + "</report>"}
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = create_input_snapshot(root / "input", archive)
            with patch("scripts.opendart.sync.MAX_DOCUMENT_UNCOMPRESSED_BYTES", 128):
                with self.assertRaisesRegex(PreparationError, "safe size limit"):
                    prepare_narrative_snapshot(
                        source,
                        embedder=DeterministicFakeEmbedder(),
                        output_root=root / "output",
                    )

    def test_rejects_non_384d_embedder_before_publishing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = create_input_snapshot(root / "input", valid_archive())
            output = root / "output"
            with self.assertRaisesRegex(PreparationError, "exactly 384"):
                prepare_narrative_snapshot(
                    source,
                    embedder=BadDimensionEmbedder(),
                    output_root=output,
                )
            self.assertFalse(output.exists())

    def test_enriched_snapshot_is_loader_compatible_without_network(self) -> None:
        connection = FakeConnection()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = create_input_snapshot(root / "input", valid_archive())
            enriched = prepare_narrative_snapshot(
                source,
                embedder=DeterministicFakeEmbedder(),
                output_root=root / "output",
            )
            result = load_snapshot(
                enriched,
                "postgresql://offline-unit-test",
                connect_factory=lambda _: connection,
            )
        self.assertGreater(result["source_chunks"], 0)
        self.assertEqual(result["narrative_corpus_status"], "FULL_TEXT")
        self.assertEqual(result["narrative_covered_files"], 2)
        self.assertEqual(result["narrative_total_files"], 2)
        statements = "\n".join(sql for sql, _ in connection.cursor_instance.executions)
        self.assertIn("narrative_corpus_status", statements)
        self.assertIn("INSERT INTO source_chunk", statements)
        self.assertTrue(connection.closed)

    def test_prepares_schema_2_0_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = create_input_snapshot_v2(root / "input", valid_archive())
            enriched = prepare_narrative_snapshot(
                source,
                embedder=DeterministicFakeEmbedder(),
                output_root=root / "output",
            )
            payload = json.loads(
                (enriched / "normalized/source_chunks.json").read_text(encoding="utf-8")
            )
            manifest = json.loads((enriched / "manifest.json").read_text(encoding="utf-8"))
            source_manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], "2.0")
        self.assertEqual(payload["source_snapshot_id"], source_manifest["snapshot_id"])
        self.assertEqual(manifest["provenance"]["evidence_status"], "OFFICIAL_API_VERIFIED")

    def test_loader_rejects_mismatched_full_text_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = create_input_snapshot(root / "input", valid_archive())
            enriched = prepare_narrative_snapshot(
                source,
                embedder=DeterministicFakeEmbedder(),
                output_root=root / "output",
            )
            payload = json.loads(
                (enriched / "normalized/source_chunks.json").read_text(encoding="utf-8")
            )
            manifest = json.loads(
                (enriched / "manifest.json").read_text(encoding="utf-8")
            )
        payload["covered_document_files"] = 1
        payload["total_supported_document_files"] = 2
        payload["covered_document_paths"] = payload["supported_document_paths"][:1]
        with self.assertRaisesRegex(LoadError, "FULL_TEXT.*do not match"):
            validate_source_chunks(payload, manifest)

    def test_loader_rejects_random_parent_snapshot_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = create_input_snapshot(root / "input", valid_archive())
            enriched = prepare_narrative_snapshot(
                source,
                embedder=DeterministicFakeEmbedder(),
                output_root=root / "output",
            )
            payload = json.loads(
                (enriched / "normalized/source_chunks.json").read_text(encoding="utf-8")
            )
            manifest = json.loads(
                (enriched / "manifest.json").read_text(encoding="utf-8")
            )
        payload["source_snapshot_id"] = "f" * 64
        with self.assertRaisesRegex(LoadError, "does not match its manifest"):
            validate_source_chunks(payload, manifest)

    def test_manifest_origin_and_limitations_are_content_addressed(self) -> None:
        for mutation in ("origin", "limitations"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = create_input_snapshot(root / "input", valid_archive())
                enriched = prepare_narrative_snapshot(
                    source,
                    embedder=DeterministicFakeEmbedder(),
                    output_root=root / "output",
                )
                manifest_path = enriched / "manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if mutation == "origin":
                    fact_file = next(
                        item
                        for item in manifest["files"]
                        if item["path"] == "normalized/financial_fact.json"
                    )
                    fact_file["origin"] = "LOCAL_DERIVATION"
                else:
                    manifest["limitations"].append("tampered limitation")
                manifest_path.write_bytes(canonical_json_bytes(manifest))
                with self.assertRaisesRegex(LoadError, "content address"):
                    validate_live_snapshot(enriched)

    def test_full_text_counts_are_checked_against_the_raw_archive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = create_input_snapshot(root / "input", valid_archive())
            enriched = prepare_narrative_snapshot(
                source,
                embedder=DeterministicFakeEmbedder(),
                output_root=root / "output",
            )
            payload = json.loads(
                (enriched / "normalized/source_chunks.json").read_text(encoding="utf-8")
            )
            manifest = json.loads(
                (enriched / "manifest.json").read_text(encoding="utf-8")
            )
            archive_bytes = (enriched / "raw/document.zip").read_bytes()
        fake_paths = [f"fake/{index:04d}.xml" for index in range(999)]
        payload["covered_document_paths"] = fake_paths
        payload["supported_document_paths"] = fake_paths
        payload["covered_document_files"] = 999
        payload["total_supported_document_files"] = 999
        with self.assertRaisesRegex(LoadError, "does not match raw document archive"):
            validate_source_chunks(
                payload,
                manifest,
                document_archive=archive_bytes,
            )

    def test_declared_chunking_is_replayed_against_archive_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = create_input_snapshot(root / "input", valid_archive())
            enriched = prepare_narrative_snapshot(
                source,
                embedder=DeterministicFakeEmbedder(),
                output_root=root / "output",
            )
            payload = json.loads(
                (enriched / "normalized/source_chunks.json").read_text(encoding="utf-8")
            )
            manifest = json.loads(
                (enriched / "manifest.json").read_text(encoding="utf-8")
            )
            archive_bytes = (enriched / "raw/document.zip").read_bytes()
        payload["chunking"] = {
            "algorithm": "fixed_unicode_chars_v1",
            "max_chars": 80,
            "overlap_chars": 20,
        }
        with self.assertRaisesRegex(LoadError, "deterministic archive chunking"):
            validate_source_chunks(
                payload,
                manifest,
                document_archive=archive_bytes,
            )


if __name__ == "__main__":
    unittest.main()
