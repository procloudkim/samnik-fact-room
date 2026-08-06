from __future__ import annotations

import json
import os
import tempfile
import unittest
from typing import Any
from pathlib import Path
from unittest.mock import patch

from scripts.opendart import backfill
from scripts.opendart.sync import SyncError


class BackfillTests(unittest.TestCase):
    def test_derive_coverage_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "snapshot"
            snapshot.mkdir()
            (snapshot / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": "2.0",
                        "snapshot_id": "0" * 64,
                        "dataset": "opendart-filing",
                        "immutable": True,
                        "provenance": {
                            "source_system": "OpenDART",
                            "acquisition_mode": "LIVE_API",
                            "evidence_status": "OFFICIAL_API_VERIFIED",
                            "acquired_at": "2026-07-21T00:00:00+00:00",
                            "selection_policy": "latest_non_withdrawn_receipt",
                        },
                        "companies": [{"corp_code": "00126380"}],
                        "filings": [{"rcept_no": "x"}],
                        "coverage": [
                            {
                                "fs_div": "CFS",
                                "status": "LOADED",
                                "source_row_count": 1,
                                "loaded_row_count": 1,
                            },
                            {
                                "fs_div": "OFS",
                                "status": "SOURCE_NOT_AVAILABLE",
                                "source_row_count": 0,
                                "loaded_row_count": 0,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            coverage = backfill._derive_coverage_status(snapshot)
            self.assertEqual(coverage["CFS"]["status"], "LOADED")
            self.assertEqual(coverage["OFS"]["status"], "SOURCE_NOT_AVAILABLE")

    def test_no_filing_status_maps_013_to_source_not_available(self) -> None:
        status = backfill._no_filing_status(
            backfill.NoFilingError("no filing", status_code="013"), 2020, "11011", backfill.date(2026, 7, 21)
        )
        self.assertEqual(status, "SOURCE_NOT_AVAILABLE")

    def test_no_filing_status_uses_filing_window(self) -> None:
        status = backfill._no_filing_status(
            backfill.NoFilingError("no filing", status_code="404"),
            2025,
            "11011",
            backfill.date(2026, 6, 15),
        )
        self.assertEqual(status, "NOT_YET_FILED")

        status = backfill._no_filing_status(
            backfill.NoFilingError("no filing", status_code="404"),
            2025,
            "11011",
            backfill.date(2026, 6, 30),
        )
        self.assertEqual(status, "NOT_YET_FILED")

        status = backfill._no_filing_status(
            backfill.NoFilingError("no filing", status_code="404"),
            2025,
            "11011",
            backfill.date(2026, 7, 1),
        )
        self.assertEqual(status, "NOT_FILED")

    def test_main_fatal_status_stops_after_writing_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "checkpoint.json"
            os.environ["OPENDART_API_KEY"] = "test"
            with self.assertRaises(SyncError):
                with patch("scripts.opendart.backfill.sync_company_v2", side_effect=SyncError("status 010: denied")):
                    backfill.main(
                        [
                            "--from-year",
                            "2025",
                            "--to-year",
                            "2025",
                            "--company",
                            "samsung",
                            "--report-code",
                            "11011",
                            "--checkpoint",
                            str(checkpoint),
                        ]
                    )
            payload = json.loads(checkpoint.read_text(encoding="utf-8"))
            entry = payload["completed"]["samsung:2025:11011"]
            self.assertEqual(entry["state"], "FAILED")
            self.assertEqual(entry["last_error"]["code"], "010")

    def test_main_non_fatal_status_records_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "checkpoint.json"
            os.environ["OPENDART_API_KEY"] = "test"
            with patch("scripts.opendart.backfill.sync_company_v2", side_effect=SyncError("unexpected issue")):
                result = backfill.main(
                    [
                        "--from-year",
                        "2025",
                        "--to-year",
                        "2025",
                        "--company",
                        "samsung",
                        "--report-code",
                        "11011",
                        "--checkpoint",
                        str(checkpoint),
                    ]
                )
            self.assertEqual(result, 0)
            payload = json.loads(checkpoint.read_text(encoding="utf-8"))
            self.assertEqual(payload["completed"]["samsung:2025:11011"]["state"], "FAILED")

    def test_main_include_narrative_loads_enriched_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "checkpoint.json"
            os.environ["OPENDART_API_KEY"] = "test"
            os.environ["DATABASE_URL"] = "postgres://test"
            raw_snapshot = Path(tmp) / "raw-snapshot"
            prepared_snapshot = Path(tmp) / "prepared-snapshot"
            raw_snapshot.mkdir(parents=True)
            (raw_snapshot / "manifest.json").write_text(
                json.dumps(
                    {
                        "coverage": [
                            {"fs_div": "CFS", "status": "LOADED", "source_row_count": 1, "loaded_row_count": 1},
                            {"fs_div": "OFS", "status": "LOADED", "source_row_count": 1, "loaded_row_count": 1},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch("scripts.opendart.backfill.sync_company_v2", return_value=raw_snapshot),
                patch("scripts.opendart.backfill.prepare_narrative_snapshot", return_value=prepared_snapshot) as prepare_fn,
                patch("scripts.opendart.backfill.load_snapshot_v2", return_value={"snapshot_id": "loaded"}) as load_fn,
                patch("scripts.opendart.backfill.load_dotenv_key", side_effect=lambda _path, key: "test" if key == "OPENDART_API_KEY" else None),
            ):
                result = backfill.main(
                    [
                        "--from-year",
                        "2025",
                        "--to-year",
                        "2025",
                        "--company",
                        "samsung",
                        "--report-code",
                        "11011",
                        "--load",
                        "--include-narrative",
                        "--checkpoint",
                        str(checkpoint),
                    ]
                )
            self.assertEqual(result, 0)
            prepare_fn.assert_called_once()
            load_fn.assert_called_once()

            payload = json.loads(checkpoint.read_text(encoding="utf-8"))
            self.assertEqual(payload["completed"]["samsung:2025:11011"]["state"], "VERIFIED")

    def test_main_include_narrative_handles_unsafe_archive_without_failing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "checkpoint.json"
            os.environ["OPENDART_API_KEY"] = "test"
            raw_snapshot = Path(tmp) / "raw-snapshot"
            raw_snapshot.mkdir()
            (raw_snapshot / "manifest.json").write_text(
                json.dumps(
                    {
                        "coverage": [
                            {"fs_div": "CFS", "status": "LOADED", "source_row_count": 1, "loaded_row_count": 1},
                            {"fs_div": "OFS", "status": "LOADED", "source_row_count": 1, "loaded_row_count": 1},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            def _sync_company(*args: Any, **kwargs: Any) -> Path:
                if kwargs.get("include_document"):
                    raise backfill.SyncError("document archive contains a symbolic link")
                return raw_snapshot

            with (
                patch("scripts.opendart.backfill.sync_company_v2", side_effect=_sync_company) as sync_fn,
                patch("scripts.opendart.backfill.prepare_narrative_snapshot") as prepare_fn,
                patch(
                    "scripts.opendart.backfill.load_dotenv_key",
                    side_effect=lambda _path, key: "test" if key == "OPENDART_API_KEY" else None,
                ),
            ):
                result = backfill.main(
                    [
                        "--from-year",
                        "2025",
                        "--to-year",
                        "2025",
                        "--company",
                        "samsung",
                        "--report-code",
                        "11011",
                        "--include-narrative",
                        "--output-root",
                        tmp,
                        "--checkpoint",
                        str(checkpoint),
                    ]
                )

            self.assertEqual(result, 0)
            self.assertEqual(sync_fn.call_count, 2)
            prepare_fn.assert_not_called()

            payload = json.loads(checkpoint.read_text(encoding="utf-8"))
            entry = payload["completed"]["samsung:2025:11011"]
            self.assertEqual(entry["state"], "VERIFIED")
            self.assertEqual(entry["raw_snapshot_id"], "raw-snapshot")
            self.assertEqual(entry["enriched_snapshot_id"], "raw-snapshot")
            self.assertEqual(entry["coverage"]["NARRATIVE"]["status"], "SOURCE_NOT_AVAILABLE")
            self.assertEqual(entry["coverage"]["NARRATIVE"]["api_status_code"], "UNSAFE_ARCHIVE_PATH")

    def test_main_no_filing_loads_coverage_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "checkpoint.json"
            os.environ["OPENDART_API_KEY"] = "test"
            os.environ["DATABASE_URL"] = "postgres://test"
            with (
                patch(
                    "scripts.opendart.backfill.sync_company_v2",
                    side_effect=backfill.NoFilingError("no filing", status_code="013"),
                ) as sync_fn,
                patch(
                    "scripts.opendart.backfill.load_no_filing_coverage",
                    return_value={"status": "SOURCE_NOT_AVAILABLE"},
                ) as load_no_filing_fn,
            ):
                result = backfill.main(
                    [
                        "--from-year",
                        "2025",
                        "--to-year",
                        "2025",
                        "--company",
                        "samsung",
                        "--report-code",
                        "11011",
                        "--load",
                        "--checkpoint",
                        str(checkpoint),
                    ]
                )
            self.assertEqual(result, 0)
            sync_fn.assert_called_once()
            load_no_filing_fn.assert_called_once()
            payload = json.loads(checkpoint.read_text(encoding="utf-8"))
            entry = payload["completed"]["samsung:2025:11011"]
            self.assertEqual(entry["state"], "DB_LOADED")
            self.assertEqual(entry["coverage"]["CFS"]["status"], "SOURCE_NOT_AVAILABLE")

    def test_retryable_status_performs_backoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "checkpoint.json"
            os.environ["OPENDART_API_KEY"] = "test"
            raw_snapshot = Path(tmp) / "raw"
            raw_snapshot.mkdir()
            (raw_snapshot / "manifest.json").write_text(
                json.dumps(
                    {
                        "coverage": [
                            {"fs_div": "CFS", "status": "LOADED", "source_row_count": 1, "loaded_row_count": 1},
                            {"fs_div": "OFS", "status": "LOADED", "source_row_count": 1, "loaded_row_count": 1},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            calls = []
            with (
                patch(
                    "scripts.opendart.backfill.sync_company_v2",
                    side_effect=[SyncError("OpenDART list.json returned status 800"), raw_snapshot],
                ) as sync_fn,
                patch("time.sleep", side_effect=lambda delay: calls.append(delay)),
            ):
                result = backfill.main(
                    [
                        "--from-year",
                        "2025",
                        "--to-year",
                        "2025",
                        "--company",
                        "samsung",
                        "--report-code",
                        "11011",
                        "--checkpoint",
                        str(checkpoint),
                    ]
                )
            self.assertEqual(result, 0)
            self.assertEqual(sync_fn.call_count, 2)
            self.assertEqual(calls, [1])

    def test_resume_skips_only_verified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "checkpoint.json"
            completed_snapshot = Path(tmp) / "completed_snapshot"
            completed_snapshot.mkdir()
            (completed_snapshot / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": "2.0",
                        "snapshot_id": "0" * 64,
                        "dataset": "opendart-filing",
                        "immutable": True,
                        "provenance": {
                            "source_system": "OpenDART",
                            "acquisition_mode": "LIVE_API",
                            "evidence_status": "OFFICIAL_API_VERIFIED",
                            "acquired_at": "2026-07-21T00:00:00+00:00",
                            "selection_policy": "latest_non_withdrawn_receipt",
                        },
                        "companies": [{"corp_code": "00126380"}],
                        "filings": [{"rcept_no": "x"}],
                        "coverage": [
                            {
                                "fs_div": "CFS",
                                "status": "LOADED",
                                "source_row_count": 1,
                                "loaded_row_count": 1,
                            },
                            {
                                "fs_div": "OFS",
                                "status": "LOADED",
                                "source_row_count": 1,
                                "loaded_row_count": 1,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with checkpoint.open("w", encoding="utf-8") as file:
                file.write(
                    json.dumps(
                        {
                            "completed": {
                                "samsung:2025:11011": {
                                    "state": "VERIFIED",
                                    "raw_snapshot_id": "r",
                                },
                                "samsung:2025:11012": {
                                    "state": "FAILED",
                                    "completed_state": "PENDING",
                                    "raw_snapshot_id": None,
                                },
                                "samsung:2025:11013": {
                                    "state": "VERIFIED",
                                    "raw_snapshot_id": "r",
                                },
                                "samsung:2025:11014": {
                                    "state": "VERIFIED",
                                    "raw_snapshot_id": "r",
                                },
                            }
                        }
                    )
                )
            os.environ["OPENDART_API_KEY"] = "test"
            with patch("scripts.opendart.backfill.sync_company_v2", return_value=completed_snapshot) as sync_fn:
                result = backfill.main(
                    [
                        "--from-year",
                        "2025",
                        "--to-year",
                        "2025",
                        "--company",
                        "samsung",
                        "--report-code",
                        "all",
                        "--resume",
                        "--checkpoint",
                        str(checkpoint),
                    ]
                )
            self.assertEqual(result, 0)
            self.assertEqual(sync_fn.call_count, 1)

    def test_max_requests_counts_retry_attempts_on_failure(self) -> None:
        def _failing_sync(*args: Any, **kwargs: Any) -> Path:
            # First positional argument is the CountingClient wrapped by backfill.
            client = args[0]
            client._reserve_request_slot()
            raise backfill.SyncError("OpenDART list.json returned status 500")

        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "checkpoint.json"
            os.environ["OPENDART_API_KEY"] = "test"
            with (
                patch("scripts.opendart.backfill.sync_company_v2", side_effect=_failing_sync) as sync_fn,
                patch("time.sleep"),
            ):
                result = backfill.main(
                    [
                        "--from-year",
                        "2025",
                        "--to-year",
                        "2025",
                        "--company",
                        "samsung",
                        "--report-code",
                        "11011",
                        "--max-requests",
                        "3",
                        "--checkpoint",
                        str(checkpoint),
                    ]
                )
            self.assertEqual(result, 0)
            self.assertEqual(sync_fn.call_count, 4)
            payload = json.loads(checkpoint.read_text(encoding="utf-8"))
            entry = payload["completed"]["samsung:2025:11011"]
            self.assertEqual(entry["state"], "FAILED")
            self.assertEqual(entry["attempt_count"], 3)
            self.assertEqual(payload["completed"]["samsung:2025:11011"]["api_attempts"], 3)

    def test_resume_continues_from_failed_db_loaded_without_refetch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "checkpoint.json"
            raw_snapshot = Path(tmp) / "raw"
            enriched_snapshot = Path(tmp) / "enriched"
            raw_snapshot.mkdir()
            enriched_snapshot.mkdir()
            for directory in (raw_snapshot, enriched_snapshot):
                (directory / "manifest.json").write_text(
                    json.dumps(
                        {
                            "coverage": [
                                {
                                    "fs_div": "CFS",
                                    "status": "LOADED",
                                    "source_row_count": 1,
                                    "loaded_row_count": 1,
                                },
                                {
                                    "fs_div": "OFS",
                                    "status": "LOADED",
                                    "source_row_count": 1,
                                    "loaded_row_count": 1,
                                },
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
            with checkpoint.open("w", encoding="utf-8") as file:
                file.write(
                    json.dumps(
                        {
                            "completed": {
                                "samsung:2025:11011": {
                                    "state": "FAILED",
                                    "completed_state": "DB_LOADED",
                                    "raw_snapshot_id": "raw",
                                    "enriched_snapshot_id": "enriched",
                                }
                            }
                        }
                    )
                )
            os.environ["OPENDART_API_KEY"] = "test"
            with patch("scripts.opendart.backfill.sync_company_v2") as sync_fn:
                result = backfill.main(
                    [
                        "--from-year",
                        "2025",
                        "--to-year",
                        "2025",
                        "--company",
                        "samsung",
                        "--report-code",
                        "11011",
                        "--resume",
                        "--output-root",
                        tmp,
                        "--checkpoint",
                        str(checkpoint),
                    ]
                )
            self.assertEqual(result, 0)
            self.assertEqual(sync_fn.call_count, 0)
            payload = json.loads(checkpoint.read_text(encoding="utf-8"))
            self.assertEqual(payload["completed"]["samsung:2025:11011"]["state"], "VERIFIED")


if __name__ == "__main__":
    unittest.main()
