#!/usr/bin/env python3
"""Resume-safe OpenDART schema-v2 backfill runner."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from scripts.opendart.load_snapshot_v2 import (
    LoadV2Error,
    load_no_filing_coverage,
    load_snapshot_v2,
)
from scripts.opendart.prepare_narrative import (
    PassageEmbedder,
    PreparationError,
    SentenceTransformerEmbedder,
    prepare_narrative_snapshot,
)
from scripts.opendart.snapshot_v2 import (
    NoFilingError,
    load_dotenv_key,
    sync_company_v2,
)
from scripts.opendart.sync import (
    COMPANIES,
    SUPPORTED_REPORT_CODES,
    SyncError,
    _filing_window,
    canonical_json_bytes,
    OpenDartClient,
)


DEFAULT_CHECKPOINT = Path("tmp/opendart-backfill/checkpoint.json")
STATE_ORDER = ("PENDING", "ACQUIRED", "NARRATIVE_READY", "DB_LOADED", "VERIFIED", "FAILED")
MAX_REQUESTS_TOTAL = 720
MAX_REQUESTS_PER_REPORT = 180
MAX_RETRIES = 4
BACKOFF_SECONDS = (1, 2, 4)
INSTANT_RETRY_STATUSES = {"800", "900", "429"}
MAX_PERMISSIVE_HTTP_STATUS_PREFIX = "5"
FATAL_ABORT_STATUSES = {"010", "011", "012", "020", "901"}
UNSAFE_DOCUMENT_MARKERS = {"unsafe path", "invalid path", "symbolic link", "document archive contains a"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backfill immutable OpenDART schema-v2 snapshots")
    parser.add_argument("--from-year", type=int, default=2015)
    parser.add_argument("--to-year", type=int, default=date.today().year)
    parser.add_argument("--company", choices=["all", *COMPANIES.keys()], default="all")
    parser.add_argument("--report-code", choices=["all", *sorted(SUPPORTED_REPORT_CODES)], default="all")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--load", action="store_true", help="load each snapshot after processing")
    parser.add_argument("--max-requests", type=int)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--structured-only", action="store_true")
    mode.add_argument("--include-narrative", action="store_true")
    parser.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT)
    parser.add_argument("--output-root", type=Path, default=Path("data/snapshots"))
    return parser


def _targets(args: argparse.Namespace) -> list[tuple[str, int, str]]:
    companies = list(COMPANIES) if args.company == "all" else [args.company]
    reports = (
        ["11011", "11013", "11012", "11014"]
        if args.report_code == "all"
        else [args.report_code]
    )
    return [
        (company, year, report)
        for year in range(args.to_year, args.from_year - 1, -1)
        for company in companies
        for report in reports
    ]


def _key(company: str, year: int, report: str) -> str:
    return f"{company}:{year}:{report}"


def _not_yet_filed(year: int, report: str, today: date) -> bool:
    _, period_end = _filing_window(year, report)
    period_end = date.fromisoformat(f"{period_end[:4]}-{period_end[4:6]}-{period_end[6:]}")
    return period_end >= today


def _state_rank(state: str) -> int:
    try:
        return STATE_ORDER.index(state)
    except ValueError:
        return 0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_status_code(error: BaseException) -> str | None:
    if isinstance(error, SyncError) and hasattr(error, "status_code"):
        code = getattr(error, "status_code", None)
        if isinstance(code, str) and re.fullmatch(r"\d{3}", code):
            return code
    if match := re.search(r"\b(\d{3})\b", str(error)):
        value = match.group(1)
        if value:
            return value
    return None


def _is_retryable(status_code: str | None) -> bool:
    if status_code is None:
        return False
    return status_code in INSTANT_RETRY_STATUSES or (
        status_code.isdigit()
        and len(status_code) == 3
        and status_code.startswith(MAX_PERMISSIVE_HTTP_STATUS_PREFIX)
    )


def _is_fatal_abort(code: str | None) -> bool:
    return code in FATAL_ABORT_STATUSES


def _is_unsafe_path_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in UNSAFE_DOCUMENT_MARKERS)


def _no_filing_status(exc: NoFilingError, year: int, report: str, today: date) -> str:
    if getattr(exc, "status_code", None) in {"013", "014"}:
        return "SOURCE_NOT_AVAILABLE"
    return "NOT_YET_FILED" if _not_yet_filed(year, report, today) else "NOT_FILED"


def _derive_coverage_status(snapshot_dir: Path) -> dict[str, dict[str, Any]]:
    manifest = _read_manifest(snapshot_dir)
    coverage = manifest.get("coverage")
    if not isinstance(coverage, list) or not coverage:
        raise SyncError("backfill: snapshot coverage is missing")
    result: dict[str, dict[str, Any]] = {}
    for row in coverage:
        if not isinstance(row, Mapping):
            continue
        fs_div = str(row.get("fs_div", ""))
        status = str(row.get("status", ""))
        if fs_div in {"CFS", "OFS"}:
            result[fs_div] = {
                "status": status,
                "api_status_code": row.get("api_status_code"),
                "source_row_count": row.get("source_row_count", 0),
                "loaded_row_count": row.get("loaded_row_count", 0),
            }
    if set(result) != {"CFS", "OFS"}:
        raise SyncError("backfill: snapshot coverage did not declare CFS/OFS")
    return result


def _read_manifest(path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SyncError("backfill target snapshot manifest is invalid") from exc
    if not isinstance(payload, Mapping):
        raise SyncError("backfill target snapshot manifest is invalid")
    return payload


def _read_checkpoint(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"updated_at": None, "completed": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise SyncError("backfill checkpoint is invalid") from None
    if not isinstance(payload, Mapping):
        raise SyncError("backfill checkpoint is invalid")
    completed = payload.get("completed")
    if not isinstance(completed, Mapping):
        completed = payload.get("records")
    if not isinstance(completed, Mapping):
        raise SyncError("backfill checkpoint has invalid completed records")
    normalized = {}
    for key, value in completed.items():
        normalized[str(key)] = _normalize_record(value)
    return {
        "updated_at": payload.get("updated_at"),
        "completed": normalized,
    }


def _write_checkpoint(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(canonical_json_bytes(payload))
    os.replace(temporary, path)


def _normalize_record(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return _default_record()
    status = str(value.get("state") or value.get("status") or "PENDING")
    if status == "LOADED":
        status = "VERIFIED"
    last_error = value.get("last_error")
    error_code = value.get("error_code")
    if error_code is None and isinstance(last_error, Mapping):
        error_code = last_error.get("code")
    return {
        "state": status,
        "status": status,
        "raw_snapshot_id": value.get("raw_snapshot_id"),
        "enriched_snapshot_id": value.get("enriched_snapshot_id"),
        "coverage": value.get("coverage", {}),
        "attempt_count": _as_int(value.get("attempt_count"), 0),
        "api_attempts": _as_int(value.get("api_attempts"), _as_int(value.get("attempt_count"), 0)),
        "checked_at": value.get("checked_at"),
        "last_error": value.get("last_error"),
        "completed_state": value.get("completed_state"),
        "error_code": error_code,
    }


def _as_int(value: Any, fallback: int) -> int:
    if isinstance(value, bool) or isinstance(value, float):
        return fallback
    if isinstance(value, int):
        return value
    return fallback


def _default_record() -> dict[str, Any]:
    return {
        "state": "PENDING",
        "raw_snapshot_id": None,
        "enriched_snapshot_id": None,
        "coverage": {},
        "attempt_count": 0,
        "api_attempts": 0,
        "checked_at": None,
        "last_error": None,
        "completed_state": None,
    }


def _build_budgets(report_codes: list[str], explicit_limit: int | None) -> tuple[dict[str, int], int]:
    if explicit_limit is None:
        total_budget = MAX_REQUESTS_TOTAL
    else:
        if explicit_limit < 0:
            raise SyncError("max-requests must be non-negative")
        total_budget = min(explicit_limit, MAX_REQUESTS_TOTAL)
    per_report = {report: MAX_REQUESTS_PER_REPORT for report in report_codes}
    return per_report, total_budget


def _mark_failure(
    record: dict[str, Any],
    *,
    stage: str,
    error: BaseException,
) -> None:
    code = _extract_status_code(error)
    record["state"] = "FAILED"
    record["status"] = "FAILED"
    record["completed_state"] = stage
    record["error_code"] = code
    record["last_error"] = {
        "code": code,
        "message": str(error),
        "stage": stage,
    }
    record["checked_at"] = _now_iso()


def _mark_verified(record: dict[str, Any], state: str) -> None:
    record["state"] = state
    record["status"] = state
    record["completed_state"] = state
    record["error_code"] = None
    record["last_error"] = None
    record["checked_at"] = _now_iso()


def _record_coverage_for_no_filing(
    record: dict[str, Any], *,
    status: str,
    api_status_code: str | None = None,
) -> None:
    record["coverage"] = {
        "CFS": {"status": status, "api_status_code": api_status_code},
        "OFS": {"status": status, "api_status_code": api_status_code},
    }


def _coerce_snapshot_path(value: Any) -> Path | None:
    if isinstance(value, Path):
        return value
    if isinstance(value, str) and value.strip():
        return Path(value)
    try:
        if hasattr(value, "__fspath__"):
            return Path(os.fspath(value))
    except TypeError:
        pass
    return None


def _record_api_attempts(
    record: dict[str, Any],
    *,
    before_count: int,
    client: CountingClient,
) -> int:
    attempts = max(0, client.request_count - before_count)
    record["attempt_count"] = _as_int(record.get("attempt_count"), 0) + attempts
    record["api_attempts"] = record["attempt_count"]
    return attempts


class CountingClient:
    def __init__(
        self,
        client: OpenDartClient,
        reserve_request: Callable[[str], None] | None = None,
    ) -> None:
        self._client = client
        self.request_count = 0
        self._reserve_request = reserve_request
        self._report_code: str | None = None
        self.budget_enforced = reserve_request is not None

    def set_report_code(self, report_code: str) -> None:
        self._report_code = report_code

    def _reserve_request_slot(self) -> None:
        if self._reserve_request is None:
            self.request_count += 1
            return
        if self._report_code is None:
            raise SyncError("backfill: report code is not configured for budget reservation")
        self._reserve_request(self._report_code)
        self.request_count += 1

    def get_json(self, endpoint: str, params: Mapping[str, str]) -> Mapping[str, Any]:
        self._reserve_request_slot()
        return self._client.get_json(endpoint, params)

    def get_bytes(self, endpoint: str, params: Mapping[str, str]) -> bytes:
        self._reserve_request_slot()
        return self._client.get_bytes(endpoint, params)


def _safe_sync_v2(
    *,
    client: CountingClient,
    company: Any,
    year: int,
    report_code: str,
    output_root: Path,
    include_document: bool,
    retries: int = MAX_RETRIES,
) -> Path:
    attempt = 0
    while True:
        try:
            return sync_company_v2(
                client,
                company,
                year,
                report_code,
                output_root,
                include_document=include_document,
            )
        except NoFilingError:
            raise
        except SyncError as exc:
            attempt += 1
            code = _extract_status_code(exc)
            if not _is_retryable(code):
                raise
            if attempt >= retries:
                raise
            time.sleep(BACKOFF_SECONDS[attempt - 1])


def _safe_load_snapshot(snapshot_path: Path, database_url: str) -> dict[str, Any]:
    return load_snapshot_v2(snapshot_path, database_url)


def _safe_prepare_narrative(
    *,
    snapshot_path: Path,
    embedder: PassageEmbedder,
    output_root: Path,
) -> Path:
    return prepare_narrative_snapshot(
        snapshot_path,
        embedder=embedder,
        output_root=output_root,
    )


def _set_checkpoint(targets_done: int, targets_total: int, completed: dict[str, Any], path: Path) -> None:
    _write_checkpoint(
        path,
        {
            "updated_at": _now_iso(),
            "targets_done": targets_done,
            "targets_total": targets_total,
            "completed": completed,
        },
    )


def _resolve_raw_snapshot(record: dict[str, Any], output_root: Path) -> Path | None:
    snapshot_id = record.get("raw_snapshot_id")
    if isinstance(snapshot_id, str) and snapshot_id:
        snapshot = output_root / snapshot_id
        if snapshot.is_dir():
            return snapshot
    return None


def _resolve_enriched_snapshot(record: dict[str, Any], output_root: Path) -> Path | None:
    snapshot_id = record.get("enriched_snapshot_id")
    if isinstance(snapshot_id, str) and snapshot_id:
        snapshot = output_root / snapshot_id
        if snapshot.is_dir():
            return snapshot
    return _resolve_raw_snapshot(record, output_root)


def _record_to_payload(record: dict[str, Any]) -> dict[str, Any]:
    payload = dict(record)
    payload["attempt_count"] = _as_int(record.get("attempt_count"), 0)
    payload["api_attempts"] = _as_int(record.get("api_attempts"), _as_int(record.get("attempt_count"), 0))
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.from_year < 2015 or args.to_year < args.from_year:
        raise SystemExit("invalid year range")

    targets = _targets(args)
    if args.dry_run:
        print(
            json.dumps(
                {
                    "target_count": len(targets),
                    "targets": [list(item) for item in targets],
                },
                ensure_ascii=False,
            )
        )
        return 0

    api_key = load_dotenv_key(Path(".env"), "OPENDART_API_KEY") or os.environ.get("OPENDART_API_KEY", "")
    if not api_key:
        raise SystemExit("OPENDART_API_KEY is not configured")

    database_url = load_dotenv_key(Path(".env"), "DATABASE_URL") or os.environ.get("DATABASE_URL", "")
    if args.load and not database_url:
        raise SystemExit("DATABASE_URL is not configured for --load")

    try:
        checkpoint = _read_checkpoint(args.checkpoint) if args.resume else {"updated_at": None, "completed": {}}
    except SyncError as exc:
        raise SystemExit(str(exc))

    completed: dict[str, Any] = checkpoint.setdefault("completed", {})
    report_codes = sorted(SUPPORTED_REPORT_CODES if args.report_code == "all" else [args.report_code])
    per_report_budget, budget_total = _build_budgets(report_codes, args.max_requests)
    used_by_report = {report: 0 for report in report_codes}
    used_total = 0

    def _reserve_request(report_code: str) -> None:
        nonlocal used_by_report, used_total
        limit = per_report_budget.get(report_code, MAX_REQUESTS_PER_REPORT)
        if used_by_report.get(report_code, 0) + 1 > limit:
            raise SyncError(f"request budget for report {report_code} exceeded")
        if used_total + 1 > budget_total:
            raise SyncError("total request budget exceeded")
        used_by_report[report_code] = used_by_report.get(report_code, 0) + 1
        used_total += 1

    client = CountingClient(OpenDartClient(api_key), reserve_request=_reserve_request)
    embedder = SentenceTransformerEmbedder() if args.include_narrative else None
    status_counts: dict[str, int] = {}

    targets_done = 0

    for company_slug, year, report_code in targets:
        target_key = _key(company_slug, year, report_code)
        if per_report_budget.get(report_code, 0) <= 0 or used_total >= budget_total:
            break
        record = _default_record()
        if target_key in completed:
            stored = _normalize_record(completed[target_key])
            if stored["state"] == "VERIFIED" and args.resume:
                continue
            record = stored
        elif not args.resume:
            # preserve any legacy checkpoint entry if present
            stored = completed.get(target_key)
            if isinstance(stored, Mapping):
                record = _normalize_record(stored)

        resume_state = record["state"]
        if resume_state == "FAILED":
            resume_state = record.get("completed_state", "PENDING")
        if resume_state == "VERIFIED":
            continue
        client.set_report_code(report_code)

        before_count = client.request_count
        company = COMPANIES[company_slug]
        target_snapshot = None
        enriched_snapshot: Path | None = None
        record["attempt_count"] = _as_int(record.get("attempt_count"), 0)
        record["api_attempts"] = _as_int(record.get("api_attempts"), record["attempt_count"])

        try:
            # Stage: ACQUIRED (acquire raw statements; skip if already present in checkpoint).
            if _state_rank(resume_state) < _state_rank("ACQUIRED"):
                unsafe_archive = False
                for include_document in (False, True) if args.include_narrative else (False,):
                    try:
                        snapshot = _safe_sync_v2(
                            client=client,
                            company=company,
                            year=year,
                            report_code=report_code,
                            output_root=args.output_root,
                            include_document=include_document,
                        )
                        target_snapshot = _coerce_snapshot_path(snapshot)
                        if target_snapshot is None:
                            raise SyncError("backfill acquired an invalid snapshot path")
                        if include_document:
                            break
                    except SyncError as exc:
                        # fallback to statement-only acquisition when archive path is unsafe.
                        if _is_unsafe_path_error(exc) and include_document:
                            unsafe_archive = True
                            break
                        raise
                if target_snapshot is None:
                    raise SyncError("backfill could not acquire a usable snapshot")
                record["raw_snapshot_id"] = target_snapshot.name
                record["coverage"] = _derive_coverage_status(target_snapshot)
                if unsafe_archive:
                    record["coverage"]["NARRATIVE"] = {
                        "status": "SOURCE_NOT_AVAILABLE",
                        "api_status_code": "UNSAFE_ARCHIVE_PATH",
                    }
                record["state"] = "ACQUIRED"
                record["completed_state"] = "ACQUIRED"
                record["last_error"] = None
            else:
                resolved_raw = _resolve_raw_snapshot(record, args.output_root)
                if resolved_raw is None:
                    raise SyncError("backfill checkpoint references missing raw snapshot")
                target_snapshot = resolved_raw

            # Stage: NARRATIVE_READY
            if _state_rank(resume_state) < _state_rank("NARRATIVE_READY"):
                unsafe_narrative = (
                    record.get("coverage", {}).get("NARRATIVE", {}).get("api_status_code")
                    == "UNSAFE_ARCHIVE_PATH"
                )
                if (
                    args.include_narrative
                    and embedder is not None
                    and target_snapshot is not None
                    and not unsafe_narrative
                ):
                    try:
                        prepared = _safe_prepare_narrative(
                            snapshot_path=target_snapshot,
                            embedder=embedder,
                            output_root=args.output_root,
                        )
                        enriched_snapshot = prepared
                        record["enrichment_error"] = None
                    except PreparationError as exc:
                        if _is_unsafe_path_error(exc):
                            record["coverage"]["NARRATIVE"] = {
                                "status": "SOURCE_NOT_AVAILABLE",
                                "api_status_code": "UNSAFE_ARCHIVE_PATH",
                            }
                            enriched_snapshot = _resolve_raw_snapshot(record, args.output_root)
                        else:
                            raise
                elif unsafe_narrative:
                    enriched_snapshot = _resolve_raw_snapshot(record, args.output_root)
                else:
                    enriched_snapshot = target_snapshot
                if enriched_snapshot is None:
                    raise SyncError("backfill has no narrative-capable snapshot")
                record["enriched_snapshot_id"] = enriched_snapshot.name
                record["state"] = "NARRATIVE_READY"
                record["completed_state"] = "NARRATIVE_READY"
                record["last_error"] = None
            else:
                enriched_snapshot = _resolve_enriched_snapshot(record, args.output_root)
                if enriched_snapshot is None:
                    raise SyncError("backfill cannot resolve prepared snapshot")

            # Stage: DB_LOADED
            if args.load:
                if _state_rank(resume_state) < _state_rank("DB_LOADED"):
                    loaded = _safe_load_snapshot(enriched_snapshot, database_url)
                    record["load_result"] = loaded
                    record["state"] = "DB_LOADED"
                    record["completed_state"] = "DB_LOADED"

            # Stage: VERIFIED
            final_state = "VERIFIED"
            record["state"] = final_state
            record["completed_state"] = final_state
            record["last_error"] = None
            record["checked_at"] = _now_iso()
            status_counts[record["state"]] = status_counts.get(record["state"], 0) + 1
            completed[target_key] = _record_to_payload(record)
            request_count = _record_api_attempts(
                record, before_count=before_count, client=client
            )
            # request budget is enforced at request time while reserving tokens.
            _set_checkpoint(targets_done + 1, len(targets), completed, args.checkpoint)
            targets_done += 1
            continue

        except NoFilingError as exc:
            status = _no_filing_status(exc, year, report_code, date.today())
            record["raw_snapshot_id"] = None
            record["enriched_snapshot_id"] = None
            _record_coverage_for_no_filing(record, status=status, api_status_code=getattr(exc, "status_code", None))

            if args.load:
                loaded = load_no_filing_coverage(
                    database_url=database_url,
                    corp_code=COMPANIES[company_slug].corp_code,
                    business_year=year,
                    report_code=report_code,
                    status=status,
                    api_status_code=getattr(exc, "status_code", None),
                    checked_at=_now_iso(),
                )
                record["load_result"] = loaded
                record["state"] = "DB_LOADED" if status == "SOURCE_NOT_AVAILABLE" else "VERIFIED"
                record["completed_state"] = record["state"]
            else:
                record["state"] = "VERIFIED"
                record["completed_state"] = "VERIFIED"
            request_count = _record_api_attempts(
                record, before_count=before_count, client=client
            )
            record["last_error"] = None
            completed[target_key] = _record_to_payload(record)
            status_counts[record["state"]] = status_counts.get(record["state"], 0) + 1
            _set_checkpoint(targets_done + 1, len(targets), completed, args.checkpoint)
            targets_done += 1

        except (SyncError, PreparationError, LoadV2Error) as exc:
            status_code = _extract_status_code(exc)

            request_count = _record_api_attempts(
                record, before_count=before_count, client=client
            )

            stage = _state_rank(record["state"]) if record["state"] in STATE_ORDER else 0
            failed_at = "PENDING"
            if stage >= _state_rank("DB_LOADED"):
                failed_at = "DB_LOADED"
            if stage >= _state_rank("NARRATIVE_READY"):
                failed_at = "NARRATIVE_READY"
            elif stage >= _state_rank("ACQUIRED"):
                failed_at = "ACQUIRED"
            _mark_failure(record, stage=failed_at, error=exc)
            if _is_fatal_abort(status_code):
                completed[target_key] = _record_to_payload(record)
                _set_checkpoint(targets_done, len(targets), completed, args.checkpoint)
                raise
            completed[target_key] = _record_to_payload(record)
            status_counts.setdefault("FAILED", 0)
            status_counts["FAILED"] += 1
            _set_checkpoint(targets_done, len(targets), completed, args.checkpoint)

        except Exception as exc:  # pragma: no cover - defensive
            request_count = _record_api_attempts(
                record, before_count=before_count, client=client
            )
            _mark_failure(record, stage="PENDING", error=exc)
            completed[target_key] = _record_to_payload(record)
            status_counts.setdefault("FAILED", 0)
            status_counts["FAILED"] += 1
            _set_checkpoint(targets_done, len(targets), completed, args.checkpoint)

    print(
        json.dumps(
            {
                "targets_total": len(targets),
                "targets_done": targets_done,
                "per_report_requests": used_by_report,
                "total_requests": used_total,
                "status_counts": status_counts,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
