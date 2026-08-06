from __future__ import annotations

import io
import hashlib
import json
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts.opendart.sync import (
    COMPANIES,
    OpenDartClient,
    SyncError,
    build_snapshot_id,
    extract_operating_profit,
    inspect_document_archive,
    parse_krw_amount,
    select_annual_filing,
    select_filing,
    sync_company,
    validate_manifest_shape,
)


SAMSUNG = COMPANIES["samsung"]
SK_HYNIX = COMPANIES["sk-hynix"]
RECEIPT = "20260310002820"
SK_HYNIX_RECEIPT = "20260317000635"


def make_list_payload(receipt: str = RECEIPT, corp_code: str = "00126380") -> dict:
    return {
        "status": "000",
        "message": "정상",
        "list": [
            {
                "corp_code": corp_code,
                "corp_name": "삼성전자",
                "report_nm": "사업보고서 (2025.12)",
                "rcept_no": receipt,
                "rcept_dt": "20260310",
            }
        ],
    }


def make_financial_payload(
    receipt: str = RECEIPT,
    corp_code: str = "00126380",
    year: str = "2025",
) -> dict:
    return {
        "status": "000",
        "message": "정상",
        "list": [
            {
                "rcept_no": receipt,
                "reprt_code": "11011",
                "bsns_year": year,
                "corp_code": corp_code,
                "corp_name": "삼성전자",
                "sj_div": "IS",
                "account_id": "ifrs-full_ProfitLossFromOperatingActivities",
                "account_nm": "영업이익",
                "account_detail": "-",
                "thstrm_amount": "43,601,051,000,000",
                "ord": "10",
                "currency": "KRW",
                "fs_div": "CFS",
            }
        ],
    }


def make_document_zip(name: str = "삼성전자", year: str = "2025") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "report.xml",
            f'<?xml version="1.0" encoding="utf-8"?><report><title>{name}</title><year>{year}</year></report>',
        )
    return buffer.getvalue()


class FakeClient:
    def __init__(self) -> None:
        self.list_payload = make_list_payload()
        self.financial_payload = make_financial_payload()
        self.document_payload = make_document_zip()
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get_json(self, endpoint: str, params: dict[str, str]) -> dict:
        self.calls.append((endpoint, dict(params)))
        if endpoint == "list.json":
            return self.list_payload
        if endpoint == "fnlttSinglAcntAll.json":
            return self.financial_payload
        raise AssertionError(f"unexpected JSON endpoint {endpoint}")

    def get_bytes(self, endpoint: str, params: dict[str, str]) -> bytes:
        self.calls.append((endpoint, dict(params)))
        if endpoint != "document.xml":
            raise AssertionError(f"unexpected bytes endpoint {endpoint}")
        return self.document_payload


class AmountTests(unittest.TestCase):
    def test_parse_amount_formats(self) -> None:
        self.assertEqual(parse_krw_amount("43,601,051"), 43_601_051)
        self.assertEqual(parse_krw_amount("(1,200)"), -1_200)
        self.assertEqual(parse_krw_amount("-900"), -900)

    def test_parse_amount_rejects_non_integer(self) -> None:
        with self.assertRaises(SyncError):
            parse_krw_amount("KRW 100")


class SourceValidationTests(unittest.TestCase):
    def test_list_requires_expected_receipt(self) -> None:
        with self.assertRaisesRegex(SyncError, "expected receipt"):
            select_annual_filing(make_list_payload("20260310000000"), SAMSUNG, 2025, RECEIPT)

    def test_financial_payload_rejects_company_mismatch(self) -> None:
        with self.assertRaisesRegex(SyncError, "corp_code"):
            extract_operating_profit(
                make_financial_payload(corp_code="00164779"), SAMSUNG, 2025, RECEIPT
            )

    def test_financial_payload_rejects_period_mismatch(self) -> None:
        with self.assertRaisesRegex(SyncError, "bsns_year"):
            extract_operating_profit(
                make_financial_payload(year="2024"), SAMSUNG, 2025, RECEIPT
            )

    def test_selects_first_quarter_filing(self) -> None:
        receipt = "20260515000001"
        payload = make_list_payload(receipt)
        payload["list"][0].update(
            {"report_nm": "분기보고서 (2026.03)", "rcept_dt": "20260515"}
        )

        selected = select_filing(payload, SAMSUNG, 2026, "11013", receipt)

        self.assertEqual(selected["rcept_no"], receipt)
        self.assertEqual(selected["filing_date"], "2026-05-15")

    def test_half_year_uses_cumulative_amount_field(self) -> None:
        receipt = "20260814000001"
        payload = make_financial_payload(receipt=receipt, year="2026")
        row = payload["list"][0]
        row["reprt_code"] = "11012"
        row["thstrm_amount"] = "3,000"
        row["thstrm_add_amount"] = "8,000"

        fact = extract_operating_profit(
            payload, SAMSUNG, 2026, receipt, report_code="11012"
        )

        self.assertEqual(fact["amount_numeric"], "8000")
        self.assertEqual(fact["source_field"], "thstrm_add_amount")
        self.assertEqual(fact["period_end"], "2026-06-30")
        self.assertEqual(fact["value_scope"], "CONSOLIDATED_CUMULATIVE")
        self.assertEqual(fact["evidence_id"], "fact:samsung:2026H1:operating_profit")

    def test_financial_payload_ignores_separate_rows_and_selects_cfs(self) -> None:
        payload = make_financial_payload()
        separate = dict(payload["list"][0])
        separate["fs_div"] = "OFS"
        separate["thstrm_amount"] = "1"
        payload["list"].insert(0, separate)

        fact = extract_operating_profit(payload, SAMSUNG, 2025, RECEIPT)

        self.assertEqual(fact["fs_div"], "CFS")
        self.assertEqual(fact["amount_numeric"], "43601051000000")

    def test_financial_payload_accepts_rows_without_echoed_scope_fields(self) -> None:
        payload = make_financial_payload()
        payload["list"][0].pop("corp_name")
        payload["list"][0].pop("fs_div")

        fact = extract_operating_profit(payload, SAMSUNG, 2025, RECEIPT)

        self.assertEqual(fact["corp_name"], "삼성전자")
        self.assertEqual(fact["fs_div"], "CFS")

    def test_financial_payload_accepts_sk_hynix_dart_operating_income_id(self) -> None:
        payload = make_financial_payload(
            receipt=SK_HYNIX_RECEIPT,
            corp_code=SK_HYNIX.corp_code,
        )
        row = payload["list"][0]
        row.pop("fs_div")
        row["corp_name"] = SK_HYNIX.corp_name
        row["account_id"] = "dart_OperatingIncomeLoss"
        row["account_nm"] = "영업이익(손실)"
        row["sj_div"] = "CIS"
        row["thstrm_amount"] = "47206319000000"

        fact = extract_operating_profit(
            payload,
            SK_HYNIX,
            2025,
            SK_HYNIX_RECEIPT,
        )

        self.assertEqual(fact["amount_numeric"], "47206319000000")
        self.assertEqual(fact["account_id"], "dart_OperatingIncomeLoss")
        self.assertEqual(fact["sj_div"], "CIS")

    def test_document_rejects_path_traversal(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("../report.xml", "삼성전자 2025")
        with self.assertRaisesRegex(SyncError, "unsafe path"):
            inspect_document_archive(buffer.getvalue(), SAMSUNG, 2025)

    def test_document_allows_leading_slash_paths(self) -> None:
        buffer = io.BytesIO()
        document_text = "삼성전자 2025"
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("/report.xml", document_text)
        index = inspect_document_archive(buffer.getvalue(), SAMSUNG, 2025)
        self.assertEqual(len(index), 1)
        item = index[0]
        self.assertEqual(item["path"], "report.xml")
        self.assertEqual(item["size_bytes"], len(document_text.encode("utf-8")))
        self.assertEqual(item["sha256"], hashlib.sha256(document_text.encode("utf-8")).hexdigest())

    def test_document_requires_company_and_year(self) -> None:
        with self.assertRaisesRegex(SyncError, "requested company"):
            inspect_document_archive(make_document_zip(name="다른회사"), SAMSUNG, 2025)


class SnapshotTests(unittest.TestCase):
    def test_repository_synthetic_fixture_is_self_consistent_and_unverified(self) -> None:
        repository = Path(__file__).resolve().parents[3]
        fixture_root = repository / "data/fixtures/synthetic"
        manifest = json.loads(
            (fixture_root / "manifest.json").read_text(encoding="utf-8")
        )
        validate_manifest_shape(manifest)
        self.assertEqual(
            manifest["snapshot_id"],
            build_snapshot_id(
                manifest["companies"],
                manifest["filings"],
                manifest["files"],
                provenance=manifest["provenance"],
                limitations=manifest["limitations"],
            ),
        )
        for file_item in manifest["files"]:
            fixture_path = fixture_root / Path(file_item["path"]).name
            payload = fixture_path.read_bytes()
            self.assertEqual(len(payload), file_item["size_bytes"])
            self.assertEqual(hashlib.sha256(payload).hexdigest(), file_item["sha256"])
        facts = json.loads(
            (fixture_root / "financial_facts.json").read_text(encoding="utf-8")
        )
        chunks = json.loads(
            (fixture_root / "source_chunks.json").read_text(encoding="utf-8")
        )
        self.assertEqual(facts["evidence_status"], "SYNTHETIC_UNVERIFIED")
        self.assertEqual(chunks["evidence_status"], "SYNTHETIC_UNVERIFIED")
        self.assertTrue(
            all(
                fact["source_field"].startswith("SYNTHETIC_")
                for fact in facts["facts"]
            )
        )

    def test_sync_creates_valid_content_addressed_snapshot_without_secret(self) -> None:
        fake = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            target = sync_company(
                client=fake,
                company=SAMSUNG,
                business_year=2025,
                output_root=Path(directory),
                expected_receipt=RECEIPT,
                acquired_at="2026-07-19T00:00:00+00:00",
            )
            manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
            validate_manifest_shape(manifest)
            self.assertEqual(target.name, manifest["snapshot_id"])
            self.assertEqual(manifest["provenance"]["evidence_status"], "OFFICIAL_API_VERIFIED")
            fact = json.loads(
                (target / "normalized/financial_fact.json").read_text(encoding="utf-8")
            )
            self.assertEqual(fact["amount_numeric"], "43601051000000")
            persisted = "".join(
                path.read_text(encoding="utf-8", errors="ignore")
                for path in target.rglob("*")
                if path.is_file()
            )
            self.assertNotIn("test-secret-key", persisted)
            self.assertEqual(
                [call[0] for call in fake.calls],
                ["list.json", "fnlttSinglAcntAll.json", "document.xml"],
            )

    def test_sync_creates_first_quarter_snapshot(self) -> None:
        receipt = "20260515000001"
        fake = FakeClient()
        fake.list_payload = make_list_payload(receipt)
        fake.list_payload["list"][0].update(
            {"report_nm": "분기보고서 (2026.03)", "rcept_dt": "20260515"}
        )
        fake.financial_payload = make_financial_payload(receipt=receipt, year="2026")
        fake.financial_payload["list"][0]["reprt_code"] = "11013"
        fake.financial_payload["list"][0]["thstrm_amount"] = "7,000"
        fake.document_payload = make_document_zip(year="2026")

        with tempfile.TemporaryDirectory() as directory:
            target = sync_company(
                client=fake,
                company=SAMSUNG,
                business_year=2026,
                output_root=Path(directory),
                expected_receipt=receipt,
                acquired_at="2026-07-21T00:00:00+00:00",
                report_code="11013",
            )
            manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
            fact = json.loads(
                (target / "normalized/financial_fact.json").read_text(encoding="utf-8")
            )

        self.assertEqual(manifest["filings"][0]["reprt_code"], "11013")
        self.assertEqual(manifest["filings"][0]["period_end"], "2026-03-31")
        self.assertEqual(fact["evidence_id"], "fact:samsung:2026Q1:operating_profit")
        self.assertEqual(fact["value_scope"], "CONSOLIDATED_CUMULATIVE")
        self.assertEqual(fake.calls[0][1]["bgn_de"], "20260401")
        self.assertEqual(fake.calls[1][1]["reprt_code"], "11013")

    def test_sync_is_idempotent_for_identical_payload_across_timestamps(self) -> None:
        fake = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = sync_company(fake, SAMSUNG, 2025, root, RECEIPT, "2026-07-19T00:00:00+00:00")
            second = sync_company(fake, SAMSUNG, 2025, root, RECEIPT, "2026-07-20T00:00:00+00:00")
            self.assertEqual(first, second)

    def test_synthetic_manifest_cannot_be_marked_official(self) -> None:
        manifest = {
            "schema_version": "1.0",
            "snapshot_id": "a" * 64,
            "dataset": "opendart-filing",
            "immutable": True,
            "provenance": {
                "source_system": "OpenDART",
                "acquisition_mode": "SYNTHETIC_FIXTURE",
                "evidence_status": "OFFICIAL_API_VERIFIED",
                "acquired_at": "2026-07-19T00:00:00+00:00",
            },
            "companies": [{}],
            "filings": [{}],
            "files": [{"path": "fixture/x", "sha256": "b" * 64}],
            "limitations": ["test"],
        }
        with self.assertRaisesRegex(SyncError, "synthetic fixture"):
            validate_manifest_shape(manifest)


class CredentialSafetyTests(unittest.TestCase):
    def test_http_failure_does_not_expose_key(self) -> None:
        secret = "test-secret-key"
        client = OpenDartClient(secret)
        upstream_error = urllib.error.URLError(
            f"https://example.invalid/?crtfc_key={secret}"
        )
        with patch("urllib.request.urlopen", side_effect=upstream_error):
            with self.assertRaises(SyncError) as caught:
                client.get_json("list.json", {"corp_code": "00126380"})
        self.assertNotIn(secret, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
