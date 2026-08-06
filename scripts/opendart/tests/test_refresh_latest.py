from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from datetime import date

from scripts.opendart import refresh_latest
from scripts.opendart.snapshot_v2 import NoFilingError
from scripts.opendart.sync import SyncError


class RefreshLatestTests(unittest.TestCase):
    def test_list_no_filing_status_uses_period_window_even_for_013_014(self) -> None:
        self.assertEqual(
            refresh_latest._no_filing_status(
                NoFilingError("no filing", status_code="013"),
                year=2026,
                report_code="11012",
                as_of=date(2026, 7, 22),
            ),
            "NOT_YET_FILED",
        )
        self.assertEqual(
            refresh_latest._no_filing_status(
                NoFilingError("no filing", status_code="014"),
                year=2025,
                report_code="11011",
                as_of=date(2026, 7, 21),
            ),
            "NOT_FILED",
        )

    def test_dry_run_excludes_current_periods_that_have_not_ended(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            result = refresh_latest.main(
                [
                    "--from-year",
                    "2026",
                    "--to-year",
                    "2026",
                    "--company",
                    "all",
                    "--report-code",
                    "all",
                    "--as-of",
                    "2026-07-22",
                    "--dry-run",
                ]
            )
        self.assertEqual(result, 0)

    def test_future_period_persists_not_yet_filed_without_http(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "checkpoint.json"
            with (
                patch.dict(
                    os.environ,
                    {"OPENDART_API_KEY": "api", "DATABASE_URL": "postgres://test"},
                    clear=True,
                ),
                patch(
                    "scripts.opendart.refresh_latest.load_no_filing_coverage"
                ) as load_coverage,
                patch(
                    "scripts.opendart.refresh_latest._fetch_latest_receipt"
                ) as fetch_receipt,
            ):
                result = refresh_latest.main(
                    [
                        "--from-year",
                        "2026",
                        "--to-year",
                        "2026",
                        "--company",
                        "samsung",
                        "--report-code",
                        "11014",
                        "--checkpoint",
                        str(checkpoint),
                        "--as-of",
                        "2026-07-22",
                    ]
                )

            self.assertEqual(result, 0)
            fetch_receipt.assert_not_called()
            load_coverage.assert_called_once()
            self.assertEqual(load_coverage.call_args.kwargs["status"], "NOT_YET_FILED")

    def test_no_filing_status_uses_filing_window(self) -> None:
        self.assertEqual(
            refresh_latest._no_filing_status(
                NoFilingError("no filing", status_code="404"),
                year=2025,
                report_code="11011",
                as_of=date(2026, 6, 15),
            ),
            "NOT_YET_FILED",
        )
        self.assertEqual(
            refresh_latest._no_filing_status(
                NoFilingError("no filing", status_code="404"),
                year=2025,
                report_code="11011",
                as_of=date(2026, 7, 1),
            ),
            "NOT_FILED",
        )

    def test_resume_skips_sync_when_receipt_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "checkpoint.json"
            checkpoint_payload = {
                "records": {
                    "samsung:2025:11011": {
                        "status": "LOADED",
                        "latest_receipt": "20251101000000",
                        "raw_snapshot_id": "existing",
                        "checked_at": "2026-01-01T00:00:00+00:00",
                    }
                }
            }
            checkpoint.write_text(json.dumps(checkpoint_payload), encoding="utf-8")

            with (
                patch.dict(os.environ, {"OPENDART_API_KEY": "api", "DATABASE_URL": "postgres://test"}),
                patch("scripts.opendart.refresh_latest._fetch_latest_receipt", return_value="20251101000000") as fetch_fn,
                patch("scripts.opendart.refresh_latest._safe_sync") as sync_fn,
                patch("scripts.opendart.refresh_latest.load_snapshot_v2") as load_fn,
            ):
                result = refresh_latest.main(
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
                        "--resume",
                        "--as-of",
                        "2026-07-22",
                    ]
                )

            self.assertEqual(result, 0)
            fetch_fn.assert_called_once()
            sync_fn.assert_not_called()
            load_fn.assert_not_called()
            payload = json.loads(checkpoint.read_text(encoding="utf-8"))
            record = payload["records"]["samsung:2025:11011"]
            self.assertEqual(record["status"], "LOADED")
            self.assertEqual(record["latest_receipt"], "20251101000000")

    def test_changed_receipt_triggers_sync_and_load(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_root = Path(directory) / "snapshots"
            snapshot = Path(directory) / "snapshot-new"
            snapshot.mkdir()
            (snapshot / "manifest.json").write_text(
                json.dumps({"coverage": []}), encoding="utf-8"
            )
            checkpoint = Path(directory) / "checkpoint.json"
            checkpoint_payload = {
                "records": {
                    "samsung:2025:11011": {
                        "status": "LOADED",
                        "latest_receipt": "20251101000000",
                        "raw_snapshot_id": "existing",
                        "checked_at": "2025-01-01T00:00:00+00:00",
                    }
                }
            }
            checkpoint.write_text(json.dumps(checkpoint_payload), encoding="utf-8")

            with (
                patch.dict(os.environ, {"OPENDART_API_KEY": "api", "DATABASE_URL": "postgres://test"}),
                patch("scripts.opendart.refresh_latest._fetch_latest_receipt", return_value="20261201000000") as fetch_fn,
                patch(
                    "scripts.opendart.refresh_latest._safe_sync",
                    return_value=snapshot,
                ) as sync_fn,
                patch(
                    "scripts.opendart.refresh_latest._read_manifest_coverage",
                    return_value={
                        "CFS": {"status": "LOADED", "source_row_count": 1, "loaded_row_count": 1},
                        "OFS": {"status": "LOADED", "source_row_count": 1, "loaded_row_count": 1},
                    },
                ) as coverage_fn,
                patch(
                    "scripts.opendart.refresh_latest.load_snapshot_v2",
                    return_value={"snapshot_id": "snapshot-new"},
                ) as load_fn,
            ):
                result = refresh_latest.main(
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
                        "--output-root",
                        str(output_root),
                        "--resume",
                        "--as-of",
                        "2026-07-22",
                    ]
                )

            self.assertEqual(result, 0)
            fetch_fn.assert_called_once()
            sync_fn.assert_called_once()
            coverage_fn.assert_called_once()
            load_fn.assert_called_once()
            payload = json.loads(checkpoint.read_text(encoding="utf-8"))
            record = payload["records"]["samsung:2025:11011"]
            self.assertEqual(record["status"], "LOADED")
            self.assertEqual(record["latest_receipt"], "20261201000000")
            self.assertEqual(record["raw_snapshot_id"], "snapshot-new")

    def test_safe_api_call_retries_with_backoff(self) -> None:
        for code in ("800", "429", "500"):
            attempts = []

            def _callback() -> Path:
                attempts.append(1)
                raise SyncError(f"OpenDART list.json returned status {code}")

            with patch("scripts.opendart.refresh_latest.time.sleep") as sleep_fn:
                with self.assertRaises(SyncError):
                    refresh_latest._safe_api_call(callback=_callback)

            self.assertEqual(len(attempts), 4)
            self.assertEqual([delay.args[0] for delay in sleep_fn.call_args_list], [1, 2, 4])

    def test_failed_status_is_written_to_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "checkpoint.json"
            checkpoint_payload = {
                "records": {
                    "samsung:2025:11011": {
                        "status": "LOADED",
                        "latest_receipt": "20251101000000",
                        "raw_snapshot_id": "existing",
                        "checked_at": "2025-01-01T00:00:00+00:00",
                    }
                }
            }
            checkpoint.write_text(json.dumps(checkpoint_payload), encoding="utf-8")

            with (
                patch.dict(os.environ, {"OPENDART_API_KEY": "api", "DATABASE_URL": "postgres://test"}),
                patch("scripts.opendart.refresh_latest._fetch_latest_receipt", return_value="20261201000000"),
                patch(
                    "scripts.opendart.refresh_latest._safe_sync",
                    side_effect=SyncError("OpenDART sync failed"),
                ),
            ):
                result = refresh_latest.main(
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
                        "--resume",
                        "--as-of",
                        "2026-07-22",
                    ]
                )

            self.assertEqual(result, 1)
            payload = json.loads(checkpoint.read_text(encoding="utf-8"))
            record = payload["records"]["samsung:2025:11011"]
            self.assertEqual(record["status"], "FAILED")
            self.assertIn("OpenDART sync failed", record["last_error"]["message"])


if __name__ == "__main__":
    unittest.main()
