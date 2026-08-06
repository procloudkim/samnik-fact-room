#!/usr/bin/env python3
"""Refresh the latest OpenDART scope snapshots.

The command performs a lightweight incremental pass:
1) resolve one latest receipt per scope (company/year/report),
2) skip full acquisition when the latest receipt has already been loaded,
3) fetch structured filing statements from OpenDART when receipt changed,
4) load the immutable structured snapshot into Postgres.

Scheduling is conservative:
- current-year scopes are checked at most once per day,
- non-current scopes are checked at most once per week.

No narrative documents are fetched here.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from scripts.opendart.backfill import (
    CountingClient,
    _is_fatal_abort,
    _is_retryable,
    _extract_status_code,
    load_dotenv_key,
    load_no_filing_coverage,
    sync_company_v2,
    load_snapshot_v2,
)
from scripts.opendart.snapshot_v2 import NoFilingError, select_filing_revisions
from scripts.opendart.sync import (
    COMPANIES,
    OpenDartClient,
    REPORT_DEFINITIONS,
    SyncError,
    _filing_window,
)

DEFAULT_CHECKPOINT = Path("tmp/opendart-refresh/checkpoint.json")
MAX_REQUESTS_DEFAULT = 64
MAX_REQUESTS_HARD = 720
MAX_RETRIES = 4
BACKOFF_SECONDS = (1, 2, 4)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Refresh OpenDART latest snapshots")
    parser.add_argument("--from-year", type=int, default=date.today().year - 3)
    parser.add_argument("--to-year", type=int, default=date.today().year)
    parser.add_argument(
        "--company",
        choices=["all", *COMPANIES.keys()],
        default="all",
    )
    parser.add_argument(
        "--report-code",
        choices=["all", "11013", "11012", "11014", "11011"],
        default="all",
    )
    parser.add_argument(
        "--checkpoint",
        type=Path,
        default=DEFAULT_CHECKPOINT,
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--max-requests",
        type=int,
        default=MAX_REQUESTS_DEFAULT,
    )
    parser.add_argument(
        "--as-of",
        default=date.today().isoformat(),
        help="Reference date for scheduling (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("data/snapshots"),
    )
    return parser


def _parse_as_of(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("as-of date must be YYYY-MM-DD") from exc


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _targets(args: argparse.Namespace) -> list[tuple[str, int, str]]:
    companies = list(COMPANIES) if args.company == "all" else [args.company]
    reports = (
        ["11013", "11012", "11014", "11011"]
        if args.report_code == "all"
        else [args.report_code]
    )
    return [
        (company, year, report)
        for year in range(args.to_year, args.from_year - 1, -1)
        for company in companies
        for report in reports
    ]


def _period_has_ended(year: int, report_code: str, as_of: date) -> bool:
    month, day = (int(part) for part in REPORT_DEFINITIONS[report_code]["period_end"].split("-"))
    return date(year, month, day) < as_of


def _key(company: str, year: int, report: str) -> str:
    return f"{company}:{year}:{report}"


def _read_checkpoint(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"updated_at": None, "records": {}}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise SyncError("refresh checkpoint is invalid")
    records = payload.get("records")
    if not isinstance(records, Mapping):
        records = payload.get("completed")
    if not isinstance(records, Mapping):
        raise SyncError("refresh checkpoint records are invalid")
    normalized: dict[str, Any] = {}
    for key, value in records.items():
        normalized[str(key)] = _normalize_record(value)
    return {"updated_at": payload.get("updated_at"), "records": normalized}


def _write_checkpoint(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    os.replace(temporary, path)


def _normalize_record(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {
            "status": None,
            "raw_snapshot_id": None,
            "latest_receipt": None,
            "attempt_count": 0,
            "checked_at": None,
            "coverage": {},
            "last_error": None,
        }
    return {
        "status": value.get("status"),
        "raw_snapshot_id": value.get("raw_snapshot_id"),
        "latest_receipt": value.get("latest_receipt"),
        "attempt_count": _as_int(value.get("attempt_count"), 0),
        "checked_at": value.get("checked_at"),
        "coverage": value.get("coverage", {}),
        "last_error": value.get("last_error"),
    }


def _as_int(value: Any, fallback: int) -> int:
    if isinstance(value, bool) or isinstance(value, float):
        return fallback
    return int(value) if isinstance(value, int) else fallback


def _parse_checked_at(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _is_due_for_check(record: Mapping[str, Any], year: int, as_of: date) -> bool:
    status = str(record.get("status") or "")
    if status == "FAILED":
        return True

    checked_at = _parse_checked_at(record.get("checked_at"))
    if checked_at is None:
        return True

    checked_day = checked_at.date()
    if year == as_of.year:
        return checked_day < as_of
    return checked_day <= as_of - timedelta(days=7)


def _no_filing_status(exc: NoFilingError, year: int, report_code: str, as_of: date) -> str:
    _begin, period_end = _filing_window(year, report_code)
    period_end_date = date.fromisoformat(f"{period_end[:4]}-{period_end[4:6]}-{period_end[6:]}")
    return "NOT_YET_FILED" if period_end_date >= as_of else "NOT_FILED"


def _derive_no_filing_coverage(status: str) -> dict[str, dict[str, Any]]:
    return {
        "CFS": {"status": status},
        "OFS": {"status": status},
    }


def _read_manifest_coverage(snapshot_path: Path) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads((snapshot_path / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SyncError("refresh: snapshot manifest is invalid") from exc

    coverage = payload.get("coverage")
    if not isinstance(coverage, list):
        raise SyncError("refresh: snapshot coverage is missing")

    status_map: dict[str, dict[str, Any]] = {}
    for row in coverage:
        if not isinstance(row, Mapping):
            continue
        fs = str(row.get("fs_div", ""))
        if fs not in {"CFS", "OFS"}:
            continue
        status_map[fs] = {
            "status": str(row.get("status", "")),
            "api_status_code": row.get("api_status_code"),
            "source_row_count": row.get("source_row_count", 0),
            "loaded_row_count": row.get("loaded_row_count", 0),
        }
    if set(status_map) != {"CFS", "OFS"}:
        raise SyncError("refresh: snapshot coverage is incomplete")
    return status_map


def _is_terminal_status(status: str) -> bool:
    return status in {"LOADED", "NOT_YET_FILED", "NOT_FILED", "SOURCE_NOT_AVAILABLE"}


def _safe_api_call(
    *,
    callback: Callable[[], Any],
) -> Any:
    attempt = 0
    while True:
        try:
            return callback()
        except SyncError as exc:
            code = _extract_status_code(exc)
            if _is_retryable(code):
                attempt += 1
                if attempt >= MAX_RETRIES:
                    raise
                time.sleep(BACKOFF_SECONDS[attempt - 1])
                continue
            raise
        except NoFilingError:
            raise


def _safe_sync(
    client: CountingClient,
    company: Any,
    year: int,
    report_code: str,
    output_root: Path,
) -> Path:
    attempt = 0
    while True:
        try:
            return sync_company_v2(client, company, year, report_code, output_root, include_document=False)
        except NoFilingError:
            raise
        except SyncError as exc:
            attempt += 1
            code = _extract_status_code(exc)
            if not _is_retryable(code) or attempt >= MAX_RETRIES:
                raise
            time.sleep(BACKOFF_SECONDS[attempt - 1])


def _fetch_latest_receipt(
    client: CountingClient,
    company: Any,
    year: int,
    report_code: str,
) -> str:
    begin_date, _period_end = _filing_window(year, report_code)
    payload = _safe_api_call(
        callback=lambda: client.get_json(
            "list.json",
            {
                "corp_code": company.corp_code,
                "bgn_de": begin_date,
                "end_de": _filing_window(year, report_code)[1],
                "pblntf_ty": "A",
                "page_count": "100",
            },
        ),
    )
    _, revisions = select_filing_revisions(payload, company, year, report_code)
    if not revisions:
        raise SyncError("refresh: no filing revisions were selected")
    return str(revisions[0]["rcept_no"])


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.from_year < 2015 or args.to_year < args.from_year:
        raise SystemExit("invalid year range")
    as_of = _parse_as_of(args.as_of)

    all_targets = _targets(args)
    targets = [
        target
        for target in all_targets
        if _period_has_ended(target[1], target[2], as_of)
    ]
    prefiling_targets = [target for target in all_targets if target not in targets]
    if args.dry_run:
        print(
            json.dumps(
                {
                    "http_targets": len(targets),
                    "prefiling_targets": len(prefiling_targets),
                }
            )
        )
        return 0

    try:
        api_key = os.environ.get("OPENDART_API_KEY", "") or load_dotenv_key(
            Path(".env"), "OPENDART_API_KEY"
        )
        database_url = os.environ.get("DATABASE_URL", "") or load_dotenv_key(
            Path(".env"), "DATABASE_URL"
        )
        if not api_key:
            raise SystemExit("OPENDART_API_KEY is not configured")
        if not database_url:
            raise SystemExit("DATABASE_URL is not configured")
        checkpoint = _read_checkpoint(args.checkpoint) if args.resume else {"updated_at": None, "records": {}}
        records = checkpoint.setdefault("records", {})

        max_requests = min(max(0, args.max_requests), MAX_REQUESTS_HARD)
        if max_requests == 0:
            raise SystemExit("max-requests must be greater than zero")
        used_requests = 0

        def _reserve_request(_report_code: str) -> None:
            nonlocal used_requests
            if used_requests >= max_requests:
                raise SyncError("request budget is exceeded")
            used_requests += 1

        client = CountingClient(OpenDartClient(api_key), reserve_request=_reserve_request)
        summary: dict[str, int] = {}
        targets_done = 0

        # A period that has not ended cannot have a valid filing yet. Persist
        # this known state without spending an OpenDART request so DB coverage
        # and the public timeline share the same complete scope matrix.
        for company_slug, year, report_code in prefiling_targets:
            key = _key(company_slug, year, report_code)
            company = COMPANIES[company_slug]
            record = _normalize_record(records.get(key, {}))
            if not _is_due_for_check(record, year, as_of) and record.get("status") == "NOT_YET_FILED":
                continue
            checked_at = _now_iso()
            load_no_filing_coverage(
                database_url=database_url,
                corp_code=company.corp_code,
                business_year=year,
                report_code=report_code,
                status="NOT_YET_FILED",
                api_status_code=None,
                checked_at=checked_at,
            )
            record.update(
                {
                    "status": "NOT_YET_FILED",
                    "latest_receipt": None,
                    "coverage": _derive_no_filing_coverage("NOT_YET_FILED"),
                    "checked_at": checked_at,
                    "last_error": None,
                }
            )
            records[key] = record
            summary["NOT_YET_FILED"] = summary.get("NOT_YET_FILED", 0) + 1
            targets_done += 1

        for company_slug, year, report_code in targets:
            key = _key(company_slug, year, report_code)
            company = COMPANIES[company_slug]
            previous = records.get(key, {})
            record = _normalize_record(previous)
            if not _is_due_for_check(record, year, as_of) and _is_terminal_status(str(record.get("status") or "")):
                continue

            client.set_report_code(report_code)
            before_count = client.request_count
            try:
                latest_receipt = _fetch_latest_receipt(client, company, year, report_code)
                if str(record.get("status")) == "LOADED" and record.get("latest_receipt") == latest_receipt:
                    record["checked_at"] = _now_iso()
                    record["last_error"] = None
                    record["coverage"] = record.get("coverage", {})
                    record["status"] = "LOADED"
                else:
                    snapshot_path = _safe_sync(
                        client=client,
                        company=company,
                        year=year,
                        report_code=report_code,
                        output_root=args.output_root,
                    )
                    record["raw_snapshot_id"] = snapshot_path.name
                    record["coverage"] = _read_manifest_coverage(snapshot_path)
                    load_result = load_snapshot_v2(snapshot_path, database_url)
                    record["snapshot_id"] = load_result.get("snapshot_id")
                    record["status"] = "LOADED"
                    record["latest_receipt"] = latest_receipt
                    record["checked_at"] = _now_iso()
                    record["last_error"] = None
            except NoFilingError as exc:
                status = _no_filing_status(exc, year, report_code, as_of)
                record["status"] = status
                record["latest_receipt"] = None
                record["coverage"] = _derive_no_filing_coverage(status)
                record["checked_at"] = _now_iso()
                record["last_error"] = {"code": getattr(exc, "status_code", None), "message": str(exc)}
                load_no_filing_coverage(
                    database_url=database_url,
                    corp_code=company.corp_code,
                    business_year=year,
                    report_code=report_code,
                    status=status,
                    api_status_code=getattr(exc, "status_code", None),
                    checked_at=record["checked_at"],
                )
            except SyncError as exc:
                code = _extract_status_code(exc)
                record["status"] = "FAILED"
                record["last_error"] = {"code": code, "message": str(exc)}
                record["checked_at"] = _now_iso()
                if _is_fatal_abort(code):
                    records[key] = record
                    _write_checkpoint(
                        args.checkpoint,
                        {
                            "updated_at": _now_iso(),
                            "targets_total": len(all_targets),
                            "targets_done": targets_done,
                            "records": records,
                        },
                    )
                    raise

            attempts = client.request_count - before_count
            record["attempt_count"] = _as_int(record.get("attempt_count"), 0) + attempts
            records[key] = record
            summary[str(record["status"])] = summary.get(str(record["status"]), 0) + 1
            targets_done += 1
            _write_checkpoint(
                args.checkpoint,
                {
                    "updated_at": _now_iso(),
                    "targets_total": len(all_targets),
                    "targets_done": targets_done,
                    "records": records,
                },
            )

        print(
            json.dumps(
                {
                    "targets_total": len(all_targets),
                    "targets_done": targets_done,
                    "request_count": used_requests,
                    "request_budget": max_requests,
                    "status_counts": summary,
                },
                ensure_ascii=False,
            )
        )
        return 1 if summary.get("FAILED", 0) else 0
    except SyncError as exc:
        print(f"error: {exc}", end="")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
