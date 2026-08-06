#!/usr/bin/env python3
"""Immutable OpenDART schema-v2 snapshots with every returned statement row."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from scripts.opendart.sync import (
    COMPANIES,
    DATASET_NAME,
    REPORT_DEFINITIONS,
    SUPPORTED_REPORT_CODES,
    CompanySpec,
    DartClient,
    SyncError,
    _filing_window,
    _manifest_file,
    _media_type,
    _parse_yyyymmdd,
    _validate_receipt,
    _validate_year,
    canonical_json_bytes,
    inspect_document_archive,
    sha256_bytes,
)


SCHEMA_VERSION = "2.0"
SUPPORTED_FS_DIVS = ("CFS", "OFS")
SUPPORTED_STATEMENT_DIVS = frozenset({"BS", "IS", "CIS", "CF", "SCE"})
AMOUNT_FIELDS = (
    "thstrm_amount",
    "thstrm_add_amount",
    "frmtrm_amount",
    "frmtrm_q_amount",
    "frmtrm_add_amount",
    "bfefrmtrm_amount",
)


class NoFilingError(SyncError):
    """The requested periodic filing has not been found."""

    def __init__(self, message: str, status_code: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class SourceNotAvailableError(SyncError):
    """OpenDART has no financial statement payload for this scope."""


def _status(payload: Mapping[str, Any]) -> str:
    return str(payload.get("status", ""))


def _correction_kind(row: Mapping[str, Any]) -> str:
    report_name = str(row.get("report_nm", ""))
    if "철회" in report_name:
        return "WITHDRAWN"
    if "정정" in report_name:
        return "CORRECTION"
    return "ORIGINAL"


def select_filing_revisions(
    payload: Mapping[str, Any],
    company: CompanySpec,
    business_year: int,
    report_code: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    status = _status(payload)
    if status in {"013", "014"}:
        raise NoFilingError("no periodic filing returned by OpenDART", status_code=status)
    if status != "000":
        raise SyncError(f"OpenDART list.json returned status {status or 'missing'}")
    rows = payload.get("list")
    if not isinstance(rows, list):
        raise SyncError("OpenDART list.json response has no filing list")
    definition = REPORT_DEFINITIONS[report_code]
    marker = f"({business_year}.{definition['month']})"
    revisions: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        report_name = str(row.get("report_nm", ""))
        if str(row.get("corp_code", "")) != company.corp_code:
            continue
        if definition["title"] not in report_name or marker not in report_name:
            continue
        receipt = _validate_receipt(str(row.get("rcept_no", "")))
        revision = {
            "rcept_no": receipt,
            "report_name": report_name,
            "filing_date": _parse_yyyymmdd(row.get("rcept_dt"), "receipt"),
            "correction_kind": _correction_kind(row),
            "selected_latest": False,
            "raw": row,
        }
        revisions.append(revision)
    eligible = [item for item in revisions if item["correction_kind"] != "WITHDRAWN"]
    if not eligible:
        raise NoFilingError("no active periodic filing returned by OpenDART")
    eligible.sort(key=lambda item: (item["filing_date"], item["rcept_no"]), reverse=True)
    selected = eligible[0]
    selected["selected_latest"] = True
    revisions.sort(key=lambda item: (item["filing_date"], item["rcept_no"]))
    return selected, revisions


def _optional_integer(raw: Any) -> int | None:
    text = str(raw or "").strip()
    if not text or text in {"-", "–", "—"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    normalized = text.replace(",", "").replace(" ", "")
    if not re.fullmatch(r"[+-]?\d+", normalized):
        raise SyncError("OpenDART statement amount is not an integer")
    value = int(normalized)
    return -abs(value) if negative else value


def normalize_statement_rows(
    payload: Mapping[str, Any],
    *,
    company: CompanySpec,
    business_year: int,
    report_code: str,
    fs_div: str,
    rcept_no: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    status = _status(payload)
    if status in {"013", "014"}:
        return [], {"status": "SOURCE_NOT_AVAILABLE", "api_status_code": status}
    if status != "000":
        raise SyncError(
            f"OpenDART fnlttSinglAcntAll.json returned status {status or 'missing'}"
        )
    raw_rows = payload.get("list")
    if not isinstance(raw_rows, list):
        raise SyncError("OpenDART financial response has no list")
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_rows):
        if not isinstance(raw, dict):
            raise SyncError("OpenDART financial response contains a non-object row")
        for field, expected in (
            ("corp_code", company.corp_code),
            ("bsns_year", str(business_year)),
            ("reprt_code", report_code),
            ("fs_div", fs_div),
        ):
            value = raw.get(field)
            if value not in (None, "") and str(value) != expected:
                raise SyncError(f"OpenDART financial row {field} does not match request")
        returned_receipt = raw.get("rcept_no")
        if returned_receipt not in (None, "") and str(returned_receipt) != rcept_no:
            raise SyncError("OpenDART financial row receipt does not match selected filing")
        statement = str(raw.get("sj_div", "")).strip()
        if statement not in SUPPORTED_STATEMENT_DIVS:
            raise SyncError("OpenDART returned an unsupported statement division")
        account_id = str(raw.get("account_id", "")).strip()
        account_nm = str(raw.get("account_nm", "")).strip()
        if not account_id or not account_nm:
            raise SyncError("OpenDART financial row is missing account identity")
        row_hash = sha256_bytes(canonical_json_bytes(raw))
        line: dict[str, Any] = {
            "rcept_no": rcept_no,
            "corp_code": company.corp_code,
            "business_year": business_year,
            "reprt_code": report_code,
            "fs_div": fs_div,
            "sj_div": statement,
            "account_id": account_id,
            "account_nm": account_nm,
            "account_detail": str(raw.get("account_detail", "")).strip() or None,
            "ord": int(str(raw.get("ord", index) or index)),
            "currency": "KRW",
            "row_hash": row_hash,
            "raw": raw,
        }
        for field in AMOUNT_FIELDS:
            line[f"{field}_raw"] = None if raw.get(field) is None else str(raw.get(field))
            line[field] = _optional_integer(raw.get(field))
        for field in ("thstrm_nm", "frmtrm_nm", "frmtrm_q_nm", "bfefrmtrm_nm"):
            line[field] = str(raw.get(field, "")).strip() or None
        normalized.append(line)
    if len(normalized) != len(raw_rows):
        raise SyncError("normalized statement row count does not match source")
    return normalized, {
        "status": "LOADED",
        "api_status_code": status,
        "source_row_count": len(raw_rows),
        "loaded_row_count": len(normalized),
    }


def _snapshot_id(descriptor: Mapping[str, Any]) -> str:
    stable = dict(descriptor)
    stable_provenance = dict(stable["provenance"])
    stable_provenance.pop("acquired_at", None)
    stable["provenance"] = stable_provenance
    return sha256_bytes(canonical_json_bytes(stable))


def validate_v2_manifest(manifest: Mapping[str, Any]) -> None:
    required = {
        "schema_version", "snapshot_id", "dataset", "immutable", "provenance",
        "companies", "filings", "revisions", "coverage", "files", "limitations",
    }
    if set(manifest) != required or manifest.get("schema_version") != SCHEMA_VERSION:
        raise SyncError("snapshot manifest fields do not match schema version 2.0")
    if manifest.get("dataset") != DATASET_NAME or manifest.get("immutable") is not True:
        raise SyncError("snapshot v2 identity is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("snapshot_id", ""))):
        raise SyncError("snapshot v2 ID is invalid")
    if len(manifest.get("filings", [])) != 1:
        raise SyncError("snapshot v2 must contain exactly one selected filing")
    coverage = manifest.get("coverage")
    if not isinstance(coverage, list) or {item.get("fs_div") for item in coverage} != set(SUPPORTED_FS_DIVS):
        raise SyncError("snapshot v2 must declare CFS and OFS coverage")
    for item in coverage:
        if item.get("status") not in {"LOADED", "SOURCE_NOT_AVAILABLE"}:
            raise SyncError("snapshot v2 contains a non-terminal coverage status")
        if item.get("status") == "LOADED" and item.get("source_row_count") != item.get("loaded_row_count"):
            raise SyncError("snapshot v2 coverage row counts do not match")
    descriptor = {key: manifest[key] for key in required if key != "snapshot_id"}
    if _snapshot_id(descriptor) != manifest["snapshot_id"]:
        raise SyncError("snapshot v2 is not content addressed")


def _verify_existing(path: Path, expected: Mapping[str, Any]) -> None:
    try:
        actual = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise SyncError("existing snapshot v2 has no valid manifest") from None
    validate_v2_manifest(actual)
    if actual["snapshot_id"] != expected["snapshot_id"]:
        raise SyncError("existing snapshot v2 ID differs from expected input")
    for item in actual["files"]:
        file_path = path / item["path"]
        if not file_path.is_file() or sha256_bytes(file_path.read_bytes()) != item["sha256"]:
            raise SyncError("existing snapshot v2 file hash mismatch")


def sync_company_v2(
    client: DartClient,
    company: CompanySpec,
    business_year: int,
    report_code: str,
    output_root: Path,
    *,
    acquired_at: str | None = None,
    include_document: bool = True,
) -> Path:
    _validate_year(business_year)
    if business_year < 2015:
        raise SyncError("schema v2 supports business years from 2015")
    if report_code not in SUPPORTED_REPORT_CODES:
        raise SyncError("unsupported OpenDART report code")
    begin, end = _filing_window(business_year, report_code)
    list_payload = client.get_json(
        "list.json",
        {
            "corp_code": company.corp_code,
            "bgn_de": begin,
            "end_de": end,
            "pblntf_ty": "A",
            "page_count": "100",
        },
    )
    selected, revisions = select_filing_revisions(
        list_payload, company, business_year, report_code
    )
    receipt = selected["rcept_no"]
    all_lines: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]] = []
    file_payloads: list[tuple[str, bytes, str, str | None]] = [
        ("raw/list.json", canonical_json_bytes(list_payload), "OPENDART_API", "list.json")
    ]
    for fs_div in SUPPORTED_FS_DIVS:
        payload = client.get_json(
            "fnlttSinglAcntAll.json",
            {
                "corp_code": company.corp_code,
                "bsns_year": str(business_year),
                "reprt_code": report_code,
                "fs_div": fs_div,
            },
        )
        lines, state = normalize_statement_rows(
            payload,
            company=company,
            business_year=business_year,
            report_code=report_code,
            fs_div=fs_div,
            rcept_no=receipt,
        )
        all_lines.extend(lines)
        coverage.append({"fs_div": fs_div, **state})
        file_payloads.append(
            (f"raw/fnlttSinglAcntAll_{fs_div}.json", canonical_json_bytes(payload), "OPENDART_API", "fnlttSinglAcntAll.json")
        )
    lines_bytes = b"".join(canonical_json_bytes(line) for line in all_lines)
    file_payloads.append(
        ("normalized/financial_statement_lines.jsonl", lines_bytes, "LOCAL_DERIVATION", None)
    )
    if include_document:
        archive = client.get_bytes("document.xml", {"rcept_no": receipt})
        document_index = inspect_document_archive(archive, company, business_year)
        file_payloads.extend(
            [
                ("raw/document.zip", archive, "OPENDART_API", "document.xml"),
                ("normalized/document_index.json", canonical_json_bytes({"rcept_no": receipt, "files": document_index}), "LOCAL_DERIVATION", None),
            ]
        )
    files = [_manifest_file(path, payload, origin, endpoint) for path, payload, origin, endpoint in file_payloads]
    company_item = {
        "corp_code": company.corp_code,
        "corp_name": company.corp_name,
        "stock_code": company.stock_code,
    }
    source_url = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt}"
    filing = {
        "corp_code": company.corp_code,
        "rcept_no": receipt,
        "report_name": selected["report_name"],
        "business_year": business_year,
        "reprt_code": report_code,
        "period_start": f"{business_year}-01-01",
        "period_end": f"{business_year}-{REPORT_DEFINITIONS[report_code]['period_end']}",
        "filing_date": selected["filing_date"],
        "source_url": source_url,
    }
    revision_items = [
        {
            "rcept_no": item["rcept_no"],
            "report_name": item["report_name"],
            "filing_date": item["filing_date"],
            "correction_kind": item["correction_kind"],
            "selected_latest": item["selected_latest"],
            "source_url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={item['rcept_no']}",
            "raw": item["raw"],
        }
        for item in revisions
    ]
    provenance = {
        "source_system": "OpenDART",
        "acquisition_mode": "LIVE_API",
        "evidence_status": "OFFICIAL_API_VERIFIED",
        "acquired_at": acquired_at or datetime.now(timezone.utc).isoformat(),
        "selection_policy": "latest_non_withdrawn_receipt",
    }
    limitations = [
        "Every row returned by CFS and OFS fnlttSinglAcntAll is retained.",
        "OpenDART source fields are preserved in each normalized row.",
    ]
    descriptor = {
        "schema_version": SCHEMA_VERSION,
        "dataset": DATASET_NAME,
        "immutable": True,
        "provenance": provenance,
        "companies": [company_item],
        "filings": [filing],
        "revisions": revision_items,
        "coverage": coverage,
        "files": files,
        "limitations": limitations,
    }
    manifest = {"snapshot_id": _snapshot_id(descriptor), **descriptor}
    validate_v2_manifest(manifest)
    output_root.mkdir(parents=True, exist_ok=True)
    target = output_root / manifest["snapshot_id"]
    if target.exists():
        _verify_existing(target, manifest)
        return target
    temporary = Path(tempfile.mkdtemp(prefix=".opendart-v2-", dir=output_root))
    try:
        for path, payload, _origin, _endpoint in file_payloads:
            destination = temporary / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
        (temporary / "manifest.json").write_bytes(canonical_json_bytes(manifest))
        os.replace(temporary, target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return target


def load_dotenv_key(path: Path, key: str) -> str | None:
    if os.environ.get(key):
        return os.environ[key]
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        if name.strip() == key:
            value = value.strip().strip('"').strip("'")
            return value or None
    return None


__all__ = [
    "AMOUNT_FIELDS", "COMPANIES", "NoFilingError", "SCHEMA_VERSION",
    "SUPPORTED_FS_DIVS", "normalize_statement_rows", "select_filing_revisions",
    "sync_company_v2", "validate_v2_manifest", "load_dotenv_key",
]
