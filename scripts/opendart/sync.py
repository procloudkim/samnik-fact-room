#!/usr/bin/env python3
"""Create a validated, immutable OpenDART snapshot for the supported corpus.

Only the environment variable ``OPENDART_API_KEY`` supplies credentials. The
key is never accepted as a command-line argument, persisted, or included in an
error message. JSON API responses are serialized canonically before hashing.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Protocol, Sequence


API_BASE_URL = "https://opendart.fss.or.kr/api"
MANIFEST_SCHEMA_VERSION = "1.0"
DATASET_NAME = "opendart-filing"
ANNUAL_REPORT_CODE = "11011"
SUPPORTED_REPORT_CODES = frozenset({"11011", "11013", "11012", "11014"})
REPORT_DEFINITIONS: Mapping[str, Mapping[str, str]] = {
    "11013": {"title": "분기보고서", "month": "03", "suffix": "Q1", "period_end": "03-31"},
    "11012": {"title": "반기보고서", "month": "06", "suffix": "H1", "period_end": "06-30"},
    "11014": {"title": "분기보고서", "month": "09", "suffix": "Q3", "period_end": "09-30"},
    "11011": {"title": "사업보고서", "month": "12", "suffix": "", "period_end": "12-31"},
}
CONSOLIDATED_FS_DIV = "CFS"
OPERATING_PROFIT_ACCOUNT_IDS = frozenset(
    {
        "ifrs-full_ProfitLossFromOperatingActivities",
        "dart_OperatingIncomeLoss",
    }
)
OPERATING_PROFIT_ACCOUNT_NAMES = frozenset(
    {
        "영업이익",
        "영업이익(손실)",
        "영업손실",
    }
)
MAX_DOCUMENT_FILES = 1_000
MAX_DOCUMENT_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
SUPPORTED_DOCUMENT_SUFFIXES = frozenset({".xml", ".xhtml", ".html", ".htm"})


class SyncError(RuntimeError):
    """A safe, user-displayable acquisition or validation error."""


class SyncErrorWithCode(SyncError):
    """OpenDART HTTP error with a parsed status code."""

    def __init__(self, message: str, status_code: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class CompanySpec:
    slug: str
    corp_code: str
    corp_name: str
    stock_code: str
    expected_receipts: Mapping[int, str]


COMPANIES: Mapping[str, CompanySpec] = {
    "samsung": CompanySpec(
        slug="samsung",
        corp_code="00126380",
        corp_name="삼성전자",
        stock_code="005930",
        expected_receipts={2025: "20260310002820"},
    ),
    "sk-hynix": CompanySpec(
        slug="sk-hynix",
        corp_code="00164779",
        corp_name="SK하이닉스",
        stock_code="000660",
        expected_receipts={2025: "20260317000635"},
    ),
}


class DartClient(Protocol):
    def get_json(self, endpoint: str, params: Mapping[str, str]) -> dict[str, Any]: ...

    def get_bytes(self, endpoint: str, params: Mapping[str, str]) -> bytes: ...


class OpenDartClient:
    """Minimal stdlib-only HTTPS client with credential-safe failures."""

    def __init__(self, api_key: str, timeout_seconds: float = 30.0) -> None:
        api_key = api_key.strip()
        if not api_key or any(char.isspace() for char in api_key):
            raise SyncError("OPENDART_API_KEY is missing or malformed")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds

    def _request(self, endpoint: str, params: Mapping[str, str]) -> bytes:
        if endpoint not in {"list.json", "fnlttSinglAcntAll.json", "document.xml"}:
            raise SyncError(f"unsupported OpenDART endpoint: {endpoint}")
        query = urllib.parse.urlencode({"crtfc_key": self._api_key, **params})
        request = urllib.request.Request(
            f"{API_BASE_URL}/{endpoint}?{query}",
            headers={"Accept": "application/json, application/zip", "User-Agent": "samnik-fact-room/1.0"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            status_code = str(getattr(exc, "code", "")).strip()
            if status_code:
                raise SyncErrorWithCode(
                    f"OpenDART {endpoint} request failed (HTTP {status_code})",
                    status_code=status_code,
                ) from None
            raise SyncErrorWithCode(f"OpenDART {endpoint} request failed (HTTP)") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            # Exception strings can contain the full query URL. Do not propagate them.
            raise SyncError(
                f"OpenDART {endpoint} request failed ({type(exc).__name__})"
            ) from None

    def get_json(self, endpoint: str, params: Mapping[str, str]) -> dict[str, Any]:
        payload = self._request(endpoint, params)
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SyncError(f"OpenDART {endpoint} returned invalid JSON") from None
        if not isinstance(decoded, dict):
            raise SyncError(f"OpenDART {endpoint} returned a non-object JSON payload")
        return decoded

    def get_bytes(self, endpoint: str, params: Mapping[str, str]) -> bytes:
        return self._request(endpoint, params)


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _require_success(payload: Mapping[str, Any], endpoint: str) -> None:
    status_value = payload.get("status")
    status_code = str(status_value) if status_value is not None else ""
    if status_code != "000":
        # DART messages are safe, but keeping them out avoids accidental echo of
        # upstream diagnostic content and makes tests deterministic.
        raise SyncError(f"OpenDART {endpoint} returned status {status_code or 'missing'}")


def _normalize_name(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _validate_year(year: int) -> None:
    if not 1999 <= year <= 2100:
        raise SyncError("business year must be between 1999 and 2100")


def _validate_receipt(receipt: str) -> str:
    if not re.fullmatch(r"[0-9]{14}", receipt):
        raise SyncError("OpenDART receipt number must contain exactly 14 digits")
    return receipt


def _parse_yyyymmdd(value: Any, field_name: str) -> str:
    raw = str(value or "")
    try:
        parsed = datetime.strptime(raw, "%Y%m%d")
    except ValueError:
        raise SyncError(f"invalid {field_name} date returned by OpenDART") from None
    return parsed.date().isoformat()


def _period_key(business_year: int, report_code: str) -> str:
    try:
        suffix = REPORT_DEFINITIONS[report_code]["suffix"]
    except KeyError:
        raise SyncError("unsupported OpenDART report code") from None
    return f"{business_year}{suffix}"


def _filing_window(business_year: int, report_code: str) -> tuple[str, str]:
    if report_code == "11013":
        return f"{business_year}0401", f"{business_year}0630"
    if report_code == "11012":
        return f"{business_year}0701", f"{business_year}0930"
    if report_code == "11014":
        return f"{business_year}1001", f"{business_year}1231"
    if report_code == ANNUAL_REPORT_CODE:
        return f"{business_year + 1}0101", f"{business_year + 1}0630"
    raise SyncError("unsupported OpenDART report code")


def select_filing(
    payload: Mapping[str, Any],
    company: CompanySpec,
    business_year: int,
    report_code: str,
    expected_receipt: str | None,
) -> dict[str, Any]:
    """Select and strictly validate one requested filing returned by list.json."""

    _require_success(payload, "list.json")
    if report_code not in SUPPORTED_REPORT_CODES:
        raise SyncError("unsupported OpenDART report code")
    rows = payload.get("list")
    if not isinstance(rows, list):
        raise SyncError("OpenDART list.json response has no filing list")

    definition = REPORT_DEFINITIONS[report_code]
    period_marker = f"({business_year}.{definition['month']})"
    candidates: list[dict[str, Any]] = []
    for candidate in rows:
        if not isinstance(candidate, dict):
            continue
        if str(candidate.get("corp_code", "")) != company.corp_code:
            continue
        report_name = str(candidate.get("report_nm", ""))
        if definition["title"] not in report_name or period_marker not in report_name:
            continue
        if _normalize_name(candidate.get("corp_name")) != _normalize_name(company.corp_name):
            raise SyncError("list.json company name does not match the requested corporation")
        receipt = _validate_receipt(str(candidate.get("rcept_no", "")))
        if expected_receipt and receipt != expected_receipt:
            continue
        candidates.append(candidate)

    if not candidates:
        if expected_receipt:
            raise SyncError(
                f"expected receipt {expected_receipt} was not returned for "
                f"{company.corp_name} {_period_key(business_year, report_code)}"
            )
        raise SyncError(
            f"no filing found for {company.corp_name} "
            f"{_period_key(business_year, report_code)}"
        )

    candidates.sort(
        key=lambda row: (str(row.get("rcept_dt", "")), str(row.get("rcept_no", ""))),
        reverse=True,
    )
    selected = dict(candidates[0])
    selected["rcept_no"] = _validate_receipt(str(selected["rcept_no"]))
    selected["filing_date"] = _parse_yyyymmdd(selected.get("rcept_dt"), "receipt")
    return selected


def select_annual_filing(
    payload: Mapping[str, Any],
    company: CompanySpec,
    business_year: int,
    expected_receipt: str | None,
) -> dict[str, Any]:
    """Backward-compatible annual filing selector."""

    return select_filing(
        payload,
        company,
        business_year,
        ANNUAL_REPORT_CODE,
        expected_receipt,
    )


def parse_krw_amount(raw: Any) -> int:
    """Parse an OpenDART integer amount, including commas and parentheses."""

    text = str(raw or "").strip()
    if not text or text in {"-", "–", "—"}:
        raise SyncError("OpenDART amount is missing")
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    normalized = text.replace(",", "").replace(" ", "")
    if normalized.startswith("-"):
        negative = True
        normalized = normalized[1:]
    if not normalized.isdigit():
        raise SyncError("OpenDART amount is not an integer KRW value")
    value = int(normalized)
    return -value if negative else value


def extract_operating_profit(
    payload: Mapping[str, Any],
    company: CompanySpec,
    business_year: int,
    receipt: str,
    report_code: str = ANNUAL_REPORT_CODE,
) -> dict[str, Any]:
    """Validate CFS payload and normalize cumulative operating profit."""

    _require_success(payload, "fnlttSinglAcntAll.json")
    if report_code not in SUPPORTED_REPORT_CODES:
        raise SyncError("unsupported OpenDART report code")
    rows = payload.get("list")
    if not isinstance(rows, list) or not rows:
        raise SyncError("OpenDART financial response contains no rows")

    exact_accounts: list[dict[str, Any]] = []
    for row_value in rows:
        if not isinstance(row_value, dict):
            raise SyncError("OpenDART financial response contains a non-object row")
        row = dict(row_value)
        checks = {
            "corp_code": company.corp_code,
            "bsns_year": str(business_year),
            "rcept_no": receipt,
            "reprt_code": report_code,
        }
        for field, expected in checks.items():
            if str(row.get(field, "")) != expected:
                raise SyncError(f"financial response {field} does not match the request")
        if "corp_name" in row and _normalize_name(row.get("corp_name")) != _normalize_name(
            company.corp_name
        ):
            raise SyncError("financial response company name does not match the request")
        if "fs_div" in row:
            fs_div = str(row["fs_div"])
            if fs_div not in {CONSOLIDATED_FS_DIV, "OFS"}:
                raise SyncError("financial response fs_div is invalid")
            if fs_div != CONSOLIDATED_FS_DIV:
                continue
        account_id = str(row.get("account_id", ""))
        account_name = re.sub(r"\s+", "", str(row.get("account_nm", "")))
        is_operating_profit = (
            account_id in OPERATING_PROFIT_ACCOUNT_IDS
            or account_name in OPERATING_PROFIT_ACCOUNT_NAMES
        )
        if is_operating_profit and str(row.get("sj_div", "")) in {"IS", "CIS"}:
            exact_accounts.append(row)

    if not exact_accounts:
        raise SyncError("consolidated cumulative operating-profit account was not found")
    # Prefer the canonical IFRS concept when duplicate presentation rows exist.
    def ordinal(row: Mapping[str, Any]) -> int:
        try:
            return int(str(row.get("ord", "999999")) or "999999")
        except ValueError:
            return 999999

    exact_accounts.sort(
        key=lambda row: (
            str(row.get("account_id", ""))
            != "ifrs-full_ProfitLossFromOperatingActivities",
            ordinal(row),
        )
    )
    selected = exact_accounts[0]
    source_field = (
        "thstrm_add_amount"
        if report_code in {"11012", "11014"}
        and str(selected.get("thstrm_add_amount", "")).strip()
        else "thstrm_amount"
    )
    raw_amount = str(selected.get(source_field, "")).strip()
    amount_numeric = parse_krw_amount(raw_amount)
    definition = REPORT_DEFINITIONS[report_code]
    period_key = _period_key(business_year, report_code)
    return {
        "evidence_id": f"fact:{company.slug}:{period_key}:operating_profit",
        "corp_code": company.corp_code,
        "corp_name": company.corp_name,
        "stock_code": company.stock_code,
        "rcept_no": receipt,
        "business_year": business_year,
        "metric_key": "operating_profit",
        "account_id": str(selected.get("account_id", "")),
        "account_nm": str(selected.get("account_nm", "")),
        "sj_div": str(selected.get("sj_div", "")),
        "fs_div": CONSOLIDATED_FS_DIV,
        "period_start": f"{business_year}-01-01",
        "period_end": f"{business_year}-{definition['period_end']}",
        "value_scope": (
            "CONSOLIDATED_ANNUAL"
            if report_code == ANNUAL_REPORT_CODE
            else "CONSOLIDATED_CUMULATIVE"
        ),
        "raw_amount": raw_amount,
        "amount_numeric": str(amount_numeric),
        "currency": "KRW",
        "unit_scale": 1,
        "source_field": source_field,
        "evidence_status": "OFFICIAL_API_VERIFIED",
        "source_url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt}",
    }


def inspect_document_archive(
    archive_bytes: bytes,
    company: CompanySpec,
    business_year: int,
) -> list[dict[str, Any]]:
    """Validate document.xml's ZIP and return a safe hash-only file index."""

    if not zipfile.is_zipfile(io.BytesIO(archive_bytes)):
        raise SyncError("OpenDART document.xml did not return a ZIP archive")
    index: list[dict[str, Any]] = []
    combined_text: list[str] = []
    total_size = 0
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        infos = archive.infolist()
        if not infos or len(infos) > MAX_DOCUMENT_FILES:
            raise SyncError("OpenDART document archive has an invalid file count")
        for info in infos:
            normalized_name = info.filename.replace("\\", "/").lstrip("/")
            if not normalized_name:
                raise SyncError("OpenDART document archive contains an invalid path")
            path = PurePosixPath(normalized_name)
            if path.is_absolute() or ".." in path.parts:
                raise SyncError("OpenDART document archive contains an unsafe path")
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                raise SyncError("OpenDART document archive contains a symbolic link")
            if info.is_dir():
                continue
            total_size += info.file_size
            if total_size > MAX_DOCUMENT_UNCOMPRESSED_BYTES:
                raise SyncError("OpenDART document archive exceeds the safe size limit")
            file_bytes = archive.read(info)
            index.append(
                {
                    "path": normalized_name,
                    "size_bytes": len(file_bytes),
                    "sha256": sha256_bytes(file_bytes),
                }
            )
            if path.suffix.casefold() in SUPPORTED_DOCUMENT_SUFFIXES:
                combined_text.append(_decode_document_text(file_bytes))

    searchable = _normalize_name("\n".join(combined_text))
    if _normalize_name(company.corp_name) not in searchable:
        raise SyncError("document.xml content does not identify the requested company")
    if str(business_year) not in searchable:
        raise SyncError("document.xml content does not identify the requested business year")
    return sorted(index, key=lambda item: item["path"])


def _decode_document_text(payload: bytes) -> str:
    for encoding in ("utf-8", "cp949", "euc-kr"):
        try:
            decoded = payload.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        decoded = payload.decode("utf-8", errors="replace")
    without_tags = re.sub(r"<[^>]+>", " ", decoded)
    return html.unescape(without_tags)


def _manifest_file(path: str, payload: bytes, origin: str, endpoint: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "path": path,
        "media_type": _media_type(path),
        "sha256": sha256_bytes(payload),
        "size_bytes": len(payload),
        "origin": origin,
    }
    if endpoint:
        item["api_endpoint"] = endpoint
    return item


def _media_type(path: str) -> str:
    if path.endswith(".json"):
        return "application/json"
    if path.endswith(".zip"):
        return "application/zip"
    return "application/octet-stream"


def build_snapshot_id(
    companies: Sequence[Mapping[str, Any]],
    filings: Sequence[Mapping[str, Any]],
    files: Sequence[Mapping[str, Any]],
    *,
    provenance: Mapping[str, Any],
    limitations: Sequence[str],
) -> str:
    """Hash all stable manifest evidence metadata, excluding acquisition time."""

    descriptor = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "dataset": DATASET_NAME,
        "provenance": {
            key: provenance[key]
            for key in (
                "source_system",
                "acquisition_mode",
                "evidence_status",
                "verified_at",
                "value_verification_status",
            )
            if key in provenance
        },
        "companies": list(companies),
        "filings": list(filings),
        "files": [
            {
                "path": item["path"],
                "media_type": item["media_type"],
                "sha256": item["sha256"],
                "size_bytes": item["size_bytes"],
                "origin": item["origin"],
                **(
                    {"api_endpoint": item["api_endpoint"]}
                    if "api_endpoint" in item
                    else {}
                ),
            }
            for item in files
        ],
        "limitations": list(limitations),
    }
    return sha256_bytes(canonical_json_bytes(descriptor))


def build_source_chunk_id(
    *,
    source_snapshot_id: str,
    rcept_no: str,
    corp_code: str,
    business_year: int,
    section_path: str,
    chunk_no: int,
    content_sha256: str,
    embedding_model: str,
    embedding_sha256: str,
) -> str:
    """Build a stable chunk ID from content and its filing provenance.

    A content-only identifier would collapse identical boilerplate from two
    different filing sections into one database row. The parent snapshot and
    exact embedding artifact are also included so two enriched snapshots never
    collide at the global ``source_chunk`` primary key while referring to
    different vectors.
    """

    descriptor = {
        "source_snapshot_id": str(source_snapshot_id),
        "rcept_no": str(rcept_no),
        "corp_code": str(corp_code),
        "business_year": int(business_year),
        "section_path": str(section_path),
        "chunk_no": int(chunk_no),
        "content_sha256": str(content_sha256),
        "embedding_model": str(embedding_model),
        "embedding_sha256": str(embedding_sha256),
    }
    return sha256_bytes(canonical_json_bytes(descriptor))


def validate_manifest_shape(manifest: Mapping[str, Any]) -> None:
    """Dependency-free enforcement of the security-critical manifest invariants."""

    required = {
        "schema_version",
        "snapshot_id",
        "dataset",
        "immutable",
        "provenance",
        "companies",
        "filings",
        "files",
        "limitations",
    }
    if set(manifest) != required:
        raise SyncError("snapshot manifest fields do not match schema version 1.0")
    if manifest["schema_version"] != MANIFEST_SCHEMA_VERSION or manifest["dataset"] != DATASET_NAME:
        raise SyncError("snapshot manifest version or dataset is invalid")
    if manifest["immutable"] is not True:
        raise SyncError("snapshot manifest must be immutable")
    if not re.fullmatch(r"[0-9a-f]{64}", str(manifest["snapshot_id"])):
        raise SyncError("snapshot manifest ID is not a SHA-256 digest")
    provenance = manifest["provenance"]
    if not isinstance(provenance, dict) or provenance.get("source_system") != "OpenDART":
        raise SyncError("snapshot manifest provenance is invalid")
    required_provenance = {
        "source_system",
        "acquisition_mode",
        "evidence_status",
        "acquired_at",
    }
    allowed_provenance = {
        *required_provenance,
        "verified_at",
        "value_verification_status",
    }
    if not required_provenance.issubset(provenance) or not set(provenance).issubset(
        allowed_provenance
    ):
        raise SyncError("snapshot manifest provenance fields are invalid")
    if provenance.get("acquisition_mode") == "SYNTHETIC_FIXTURE" and provenance.get("evidence_status") != "SYNTHETIC_UNVERIFIED":
        raise SyncError("a synthetic fixture cannot be marked as official evidence")
    if provenance.get("acquisition_mode") == "LIVE_API" and provenance.get("evidence_status") != "OFFICIAL_API_VERIFIED":
        raise SyncError("a live API snapshot must pass official evidence validation")
    if not isinstance(manifest["companies"], list) or not manifest["companies"]:
        raise SyncError("snapshot manifest must contain at least one company")
    if not isinstance(manifest["filings"], list) or not manifest["filings"]:
        raise SyncError("snapshot manifest must contain at least one filing")
    if not isinstance(manifest["files"], list) or not manifest["files"]:
        raise SyncError("snapshot manifest must contain at least one file")
    if (
        not isinstance(manifest["limitations"], list)
        or not manifest["limitations"]
        or any(
            not isinstance(item, str) or not item.strip()
            for item in manifest["limitations"]
        )
    ):
        raise SyncError("snapshot manifest limitations are invalid")
    required_filing_fields = {
        "corp_code",
        "rcept_no",
        "report_name",
        "business_year",
        "reprt_code",
        "fs_div",
        "period_start",
        "period_end",
        "filing_date",
        "source_url",
    }
    for filing in manifest["filings"]:
        if not isinstance(filing, dict) or not required_filing_fields.issubset(filing):
            raise SyncError("snapshot manifest filing fields are invalid")
        report_code = str(filing.get("reprt_code", ""))
        if report_code not in SUPPORTED_REPORT_CODES or filing.get("fs_div") != "CFS":
            raise SyncError("snapshot manifest filing scope is invalid")
        try:
            business_year = int(filing["business_year"])
            expected_end = f"{business_year}-{REPORT_DEFINITIONS[report_code]['period_end']}"
        except (KeyError, TypeError, ValueError):
            raise SyncError("snapshot manifest filing period is invalid") from None
        if (
            filing.get("period_start") != f"{business_year}-01-01"
            or filing.get("period_end") != expected_end
            or not str(filing.get("source_url", "")).startswith("https://dart.fss.or.kr/")
        ):
            raise SyncError("snapshot manifest filing period or source is invalid")
    for file_item in manifest["files"]:
        required_file_fields = {"path", "media_type", "sha256", "size_bytes", "origin"}
        allowed_file_fields = {*required_file_fields, "api_endpoint"}
        if not isinstance(file_item, dict) or not required_file_fields.issubset(
            file_item
        ) or not set(file_item).issubset(allowed_file_fields):
            raise SyncError("snapshot manifest file fields are invalid")
        origin = file_item.get("origin")
        if origin not in {"OPENDART_API", "SYNTHETIC_LOCAL", "LOCAL_DERIVATION"}:
            raise SyncError("snapshot manifest contains an invalid file origin")
        if (
            provenance.get("acquisition_mode") == "SYNTHETIC_FIXTURE"
            and origin != "SYNTHETIC_LOCAL"
        ):
            raise SyncError("a synthetic snapshot contains a non-synthetic file origin")
        if (
            provenance.get("acquisition_mode") == "LIVE_API"
            and origin == "SYNTHETIC_LOCAL"
        ):
            raise SyncError("a live snapshot contains a synthetic file origin")
        if origin == "LOCAL_DERIVATION" and (
            not str(file_item.get("path", "")).startswith("normalized/")
            or "api_endpoint" in file_item
        ):
            raise SyncError("a local derivative has invalid manifest provenance")
        if (
            file_item.get("path") == "normalized/source_chunks.json"
            and origin != "LOCAL_DERIVATION"
        ):
            raise SyncError("source chunks must be declared as a local derivative")
        if not isinstance(file_item.get("media_type"), str) or not file_item[
            "media_type"
        ].strip():
            raise SyncError("snapshot manifest contains an invalid media type")
        if (
            isinstance(file_item.get("size_bytes"), bool)
            or not isinstance(file_item.get("size_bytes"), int)
            or file_item["size_bytes"] < 0
        ):
            raise SyncError("snapshot manifest contains an invalid file size")
        if "api_endpoint" in file_item and file_item["api_endpoint"] not in {
            "list.json",
            "fnlttSinglAcntAll.json",
            "document.xml",
        }:
            raise SyncError("snapshot manifest contains an invalid API endpoint")
        if not re.fullmatch(r"[0-9a-f]{64}", str(file_item.get("sha256", ""))):
            raise SyncError("snapshot manifest contains an invalid file digest")
        if str(file_item.get("path", "")).startswith(("/", "\\")) or ".." in PurePosixPath(str(file_item.get("path", "")).replace("\\", "/")).parts:
            raise SyncError("snapshot manifest contains an unsafe file path")


def _write_bytes(root: Path, relative_path: str, payload: bytes) -> None:
    destination = root / PurePosixPath(relative_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)


def _verify_existing_snapshot(path: Path, expected_manifest: Mapping[str, Any]) -> None:
    manifest_path = path / "manifest.json"
    try:
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise SyncError("existing snapshot is missing a valid manifest") from None
    validate_manifest_shape(existing)
    if existing["snapshot_id"] != expected_manifest["snapshot_id"]:
        raise SyncError("existing snapshot ID does not match the expected snapshot")
    recalculated = build_snapshot_id(
        existing["companies"],
        existing["filings"],
        existing["files"],
        provenance=existing["provenance"],
        limitations=existing["limitations"],
    )
    if recalculated != existing["snapshot_id"]:
        raise SyncError("existing snapshot manifest is not content-addressed correctly")
    for file_item in existing["files"]:
        target = path / PurePosixPath(file_item["path"])
        if not target.is_file() or sha256_bytes(target.read_bytes()) != file_item["sha256"]:
            raise SyncError("existing snapshot failed immutable file verification")


def sync_company(
    client: DartClient,
    company: CompanySpec,
    business_year: int,
    output_root: Path,
    expected_receipt: str | None = None,
    acquired_at: str | None = None,
    report_code: str = ANNUAL_REPORT_CODE,
) -> Path:
    """Acquire, validate, and atomically publish one company snapshot."""

    _validate_year(business_year)
    if report_code not in SUPPORTED_REPORT_CODES:
        raise SyncError("unsupported OpenDART report code")
    if expected_receipt:
        expected_receipt = _validate_receipt(expected_receipt)
    begin_date, end_date = _filing_window(business_year, report_code)
    list_payload = client.get_json(
        "list.json",
        {
            "corp_code": company.corp_code,
            "bgn_de": begin_date,
            "end_de": end_date,
            "pblntf_ty": "A",
            "page_count": "100",
        },
    )
    selected = select_filing(
        list_payload,
        company,
        business_year,
        report_code,
        expected_receipt,
    )
    receipt = selected["rcept_no"]
    financial_payload = client.get_json(
        "fnlttSinglAcntAll.json",
        {
            "corp_code": company.corp_code,
            "bsns_year": str(business_year),
            "reprt_code": report_code,
            "fs_div": CONSOLIDATED_FS_DIV,
        },
    )
    financial_fact = extract_operating_profit(
        financial_payload,
        company,
        business_year,
        receipt,
        report_code,
    )
    document_archive = client.get_bytes("document.xml", {"rcept_no": receipt})
    document_index = inspect_document_archive(document_archive, company, business_year)

    raw_list = canonical_json_bytes(list_payload)
    raw_financial = canonical_json_bytes(financial_payload)
    normalized_fact = canonical_json_bytes(financial_fact)
    normalized_document_index = canonical_json_bytes(
        {"rcept_no": receipt, "files": document_index}
    )
    file_payloads: list[tuple[str, bytes, str, str | None]] = [
        ("raw/list.json", raw_list, "OPENDART_API", "list.json"),
        (
            "raw/fnlttSinglAcntAll.json",
            raw_financial,
            "OPENDART_API",
            "fnlttSinglAcntAll.json",
        ),
        ("raw/document.zip", document_archive, "OPENDART_API", "document.xml"),
        ("normalized/financial_fact.json", normalized_fact, "OPENDART_API", None),
        (
            "normalized/document_index.json",
            normalized_document_index,
            "OPENDART_API",
            None,
        ),
    ]
    manifest_files = [
        _manifest_file(path, payload, origin, endpoint)
        for path, payload, origin, endpoint in file_payloads
    ]
    companies = [
        {
            "corp_code": company.corp_code,
            "corp_name": company.corp_name,
            "stock_code": company.stock_code,
        }
    ]
    definition = REPORT_DEFINITIONS[report_code]
    filings = [
        {
            "corp_code": company.corp_code,
            "rcept_no": receipt,
            "report_name": str(selected.get("report_nm", "")),
            "business_year": business_year,
            "reprt_code": report_code,
            "fs_div": CONSOLIDATED_FS_DIV,
            "period_start": f"{business_year}-01-01",
            "period_end": f"{business_year}-{definition['period_end']}",
            "filing_date": selected["filing_date"],
            "source_url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt}",
        }
    ]
    provenance = {
        "source_system": "OpenDART",
        "acquisition_mode": "LIVE_API",
        "evidence_status": "OFFICIAL_API_VERIFIED",
        "acquired_at": acquired_at or datetime.now(timezone.utc).isoformat(),
    }
    limitations = [
        "JSON API responses are canonically serialized before hashing.",
        "The document.xml response is stored as its original ZIP archive.",
        "Only consolidated cumulative operating profit is normalized for the requested report period.",
    ]
    snapshot_id = build_snapshot_id(
        companies,
        filings,
        manifest_files,
        provenance=provenance,
        limitations=limitations,
    )
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "dataset": DATASET_NAME,
        "immutable": True,
        "provenance": provenance,
        "companies": companies,
        "filings": filings,
        "files": manifest_files,
        "limitations": limitations,
    }
    validate_manifest_shape(manifest)

    output_root.mkdir(parents=True, exist_ok=True)
    target = output_root / snapshot_id
    if target.exists():
        _verify_existing_snapshot(target, manifest)
        return target

    temporary = Path(tempfile.mkdtemp(prefix=".opendart-sync-", dir=output_root))
    try:
        for relative_path, payload, _origin, _endpoint in file_payloads:
            _write_bytes(temporary, relative_path, payload)
        _write_bytes(temporary, "manifest.json", canonical_json_bytes(manifest))
        os.replace(temporary, target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return target


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create validated immutable OpenDART snapshots"
    )
    parser.add_argument(
        "--company",
        choices=[*COMPANIES.keys(), "all"],
        default="all",
        help="Company to synchronize (default: all)",
    )
    parser.add_argument("--business-year", type=int, default=2025)
    parser.add_argument(
        "--report-code",
        choices=["11013", "11012", "11014", "11011"],
        default=ANNUAL_REPORT_CODE,
        help="OpenDART report code: 11013=Q1, 11012=H1, 11014=Q3, 11011=annual",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("data/snapshots"),
        help="private, gitignored snapshot directory",
    )
    parser.add_argument(
        "--expected-rcept-no",
        help="strict receipt override; valid only when one company is selected",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.company == "all" and args.expected_rcept_no:
        print("error: --expected-rcept-no requires a single --company", file=sys.stderr)
        return 2
    api_key = os.environ.get("OPENDART_API_KEY", "")
    try:
        client = OpenDartClient(api_key)
        selected_companies = (
            list(COMPANIES.values())
            if args.company == "all"
            else [COMPANIES[args.company]]
        )
        for company in selected_companies:
            expected = args.expected_rcept_no
            if not expected and args.report_code == ANNUAL_REPORT_CODE:
                expected = company.expected_receipts.get(args.business_year)
            target = sync_company(
                client=client,
                company=company,
                business_year=args.business_year,
                output_root=args.output_root,
                expected_receipt=expected,
                report_code=args.report_code,
            )
            print(
                f"synced {company.corp_name} "
                f"{_period_key(args.business_year, args.report_code)}: "
                f"snapshot={target.name} path={target}"
            )
    except SyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
