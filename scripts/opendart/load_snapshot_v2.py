#!/usr/bin/env python3
"""Validate and transactionally load an OpenDART schema-v2 snapshot."""

from __future__ import annotations

from datetime import datetime, timezone
import io
import json
import math
import re
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from scripts.opendart.snapshot_v2 import (
    AMOUNT_FIELDS,
    _snapshot_id,
    validate_v2_manifest,
)
from scripts.opendart.sync import (
    SUPPORTED_DOCUMENT_SUFFIXES,
    build_source_chunk_id,
    canonical_json_bytes,
    sha256_bytes,
)


class LoadV2Error(RuntimeError):
    pass


def _json_lines(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError
            rows.append(item)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise LoadV2Error("statement JSONL is invalid") from None
    return rows


def _normalize_text(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    return "\n".join(
        re.sub(r"\s+", " ", line).strip() for line in value.split("\n") if line.strip()
    )


def _vector_literal(values: list[float]) -> str:
    return "[" + ",".join(format(float(value), ".17g") for value in values) + "]"


def _safe_float(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LoadV2Error("embedding contains a non-numeric value")
    converted = float(value)
    if not math.isfinite(converted):
        raise LoadV2Error("embedding contains a non-finite value")
    return converted


def _validate_source_chunks_v2(
    source_payload: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> list[dict[str, Any]]:
    required_fields = {
        "schema_version",
        "source_snapshot_id",
        "coverage_status",
        "covered_document_paths",
        "supported_document_paths",
        "covered_document_files",
        "total_supported_document_files",
        "embedding_model",
        "embedding_dimensions",
        "embedding_normalized",
        "embedding_input_prefix",
        "chunking",
        "chunks",
    }
    if set(source_payload) != required_fields:
        raise LoadV2Error("source chunks contract fields are invalid")
    if source_payload.get("schema_version") != "1.0":
        raise LoadV2Error("source chunks schema version is invalid")
    if source_payload.get("embedding_model") != "intfloat/multilingual-e5-small":
        raise LoadV2Error("source chunks embedding model is invalid")
    if source_payload.get("embedding_dimensions") != 384:
        raise LoadV2Error("source chunks embedding dimensions are invalid")
    if source_payload.get("embedding_normalized") is not True:
        raise LoadV2Error("source chunks embedding normalization is required")
    if source_payload.get("embedding_input_prefix") != "passage: ":
        raise LoadV2Error("source chunks embedding prefix is invalid")

    coverage_status = str(source_payload.get("coverage_status", ""))
    if coverage_status not in {"FULL_TEXT", "PARTIAL_TEXT", "SOURCE_NOT_AVAILABLE"}:
        raise LoadV2Error("source chunks coverage status is invalid")

    covered = source_payload.get("covered_document_paths")
    supported = source_payload.get("supported_document_paths")
    if not isinstance(covered, list) or not isinstance(supported, list):
        raise LoadV2Error("source chunk coverage paths are invalid")
    if (
        any(not isinstance(path, str) or not path for path in covered)
        or any(not isinstance(path, str) or not path for path in supported)
    ):
        raise LoadV2Error("source chunk coverage paths are invalid")

    covered_count = int(source_payload.get("covered_document_files", -1))
    total_count = int(source_payload.get("total_supported_document_files", -1))
    if not (0 <= covered_count <= total_count):
        raise LoadV2Error("source chunk coverage counts are invalid")
    if covered_count != len(covered) or total_count != len(supported):
        raise LoadV2Error("source chunk coverage counts are inconsistent")
    if coverage_status == "FULL_TEXT" and (not covered or covered != supported):
        raise LoadV2Error("FULL_TEXT coverage paths are incomplete")
    if coverage_status == "PARTIAL_TEXT" and (not covered or not supported or covered == supported):
        raise LoadV2Error("PARTIAL_TEXT coverage paths are inconsistent")
    if coverage_status == "SOURCE_NOT_AVAILABLE" and covered_count != 0:
        raise LoadV2Error("SOURCE_NOT_AVAILABLE chunk coverage must have zero processed files")

    chunking = source_payload.get("chunking")
    if not isinstance(chunking, dict):
        raise LoadV2Error("source chunking metadata is invalid")
    if set(chunking) != {"algorithm", "max_chars", "overlap_chars"}:
        raise LoadV2Error("source chunking metadata is invalid")
    if chunking.get("algorithm") != "fixed_unicode_chars_v1":
        raise LoadV2Error("source chunking algorithm is invalid")
    max_chars = chunking.get("max_chars")
    overlap_chars = chunking.get("overlap_chars")
    if (
        isinstance(max_chars, bool)
        or not isinstance(max_chars, int)
        or max_chars < 32
        or isinstance(overlap_chars, bool)
        or not isinstance(overlap_chars, int)
        or overlap_chars < 0
        or overlap_chars >= max_chars
    ):
        raise LoadV2Error("source chunking parameters are invalid")

    filings = manifest["filings"]
    if len(filings) != 1:
        raise LoadV2Error("snapshot has no filing")
    filing = filings[0]
    raw_chunks = source_payload.get("chunks")
    if not isinstance(raw_chunks, list) or not raw_chunks:
        raise LoadV2Error("source chunks payload has no chunks")

    descriptor = {
        "schema_version": manifest["schema_version"],
        "dataset": manifest["dataset"],
        "immutable": manifest["immutable"],
        "provenance": manifest["provenance"],
        "companies": manifest["companies"],
        "filings": manifest["filings"],
        "revisions": manifest["revisions"],
        "coverage": manifest["coverage"],
        "files": [
            item
            for item in manifest["files"]
            if str(item.get("path")) != "normalized/source_chunks.json"
        ],
        "limitations": manifest["limitations"],
    }
    expected_parent = _snapshot_id(descriptor)
    if str(source_payload.get("source_snapshot_id", "")) != expected_parent:
        raise LoadV2Error("source chunk parent snapshot id does not match")

    validated: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()
    for raw in raw_chunks:
        if not isinstance(raw, dict):
            raise LoadV2Error("source chunk payload contains invalid entry")
        required = {
            "chunk_id",
            "rcept_no",
            "corp_code",
            "business_year",
            "section_path",
            "chunk_no",
            "content",
            "content_sha256",
            "embedding",
            "embedding_sha256",
            "source_url",
            "evidence_status",
        }
        if set(raw) != required:
            raise LoadV2Error("source chunk fields are invalid")
        if raw["evidence_status"] != "OFFICIAL_API_VERIFIED":
            raise LoadV2Error("source chunk evidence is not official")
        if str(raw["rcept_no"]) != filing["rcept_no"] or str(raw["corp_code"]) != filing["corp_code"]:
            raise LoadV2Error("source chunk filing does not match snapshot")
        if not isinstance(raw["business_year"], int) or int(raw["business_year"]) != filing["business_year"]:
            raise LoadV2Error("source chunk business year is invalid")
        if not isinstance(raw["chunk_no"], int) or raw["chunk_no"] < 0:
            raise LoadV2Error("source chunk sequence is invalid")
        if not isinstance(raw["section_path"], str) or not str(raw["section_path"]).strip():
            raise LoadV2Error("source chunk section path is invalid")

        source_url = str(raw.get("source_url", ""))
        if not source_url.startswith("https://dart.fss.or.kr/"):
            raise LoadV2Error("source chunk source URL is invalid")

        content = str(raw["content"])
        if not content.strip():
            raise LoadV2Error("source chunk content is empty")

        content_sha256 = sha256_bytes(content.encode("utf-8"))
        if str(raw["content_sha256"]) != content_sha256:
            raise LoadV2Error("source chunk content hash is invalid")

        raw_embedding = raw["embedding"]
        if not isinstance(raw_embedding, list) or len(raw_embedding) != 384:
            raise LoadV2Error("source chunk embedding is not 384 dimensions")
        embedding = [_safe_float(value) for value in raw_embedding]
        norm = math.sqrt(sum(value * value for value in embedding))
        if not math.isclose(norm, 1.0, rel_tol=1e-6, abs_tol=1e-6):
            raise LoadV2Error("source chunk embedding is not normalized")
        embedding_hash = sha256_bytes(canonical_json_bytes(embedding))
        if str(raw["embedding_sha256"]) != embedding_hash:
            raise LoadV2Error("source chunk embedding hash is invalid")

        chunk_id = build_source_chunk_id(
            source_snapshot_id=str(source_payload["source_snapshot_id"]),
            rcept_no=str(raw["rcept_no"]),
            corp_code=str(raw["corp_code"]),
            business_year=int(raw["business_year"]),
            section_path=str(raw["section_path"]),
            chunk_no=int(raw["chunk_no"]),
            content_sha256=content_sha256,
            embedding_model=str(source_payload["embedding_model"]),
            embedding_sha256=embedding_hash,
        )
        if str(raw["chunk_id"]) != chunk_id:
            raise LoadV2Error("source chunk id is invalid")

        key = (str(raw["rcept_no"]), str(raw["section_path"]), int(raw["chunk_no"]))
        if key in seen:
            raise LoadV2Error("source chunks are duplicated by position")
        seen.add(key)

        validated.append(
            {
                "chunk_id": str(raw["chunk_id"]),
                "rcept_no": str(raw["rcept_no"]),
                "corp_code": str(raw["corp_code"]),
                "business_year": int(raw["business_year"]),
                "section_path": str(raw["section_path"]),
                "chunk_no": int(raw["chunk_no"]),
                "content": _normalize_text(content),
                "content_sha256": content_sha256,
                "embedding": embedding,
                "source_url": source_url,
            }
        )

    return validated


def validate_snapshot_v2(snapshot_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise LoadV2Error("snapshot v2 manifest is invalid") from None

    try:
        validate_v2_manifest(manifest)
    except Exception as exc:
        raise LoadV2Error(str(exc)) from None

    if snapshot_dir.name != manifest["snapshot_id"]:
        raise LoadV2Error("snapshot v2 directory name does not match snapshot ID")

    for item in manifest["files"]:
        relative = str(item.get("path", ""))
        normalized = PurePosixPath(relative)
        if normalized.is_absolute() or ".." in normalized.parts:
            raise LoadV2Error("snapshot v2 has unsafe file path")
        target = snapshot_dir / normalized
        if not target.is_file():
            raise LoadV2Error("snapshot v2 file is missing")
        if sha256_bytes(target.read_bytes()) != item["sha256"]:
            raise LoadV2Error("snapshot v2 file hash mismatch")

    lines_path = snapshot_dir / "normalized/financial_statement_lines.jsonl"
    if not lines_path.is_file():
        raise LoadV2Error("snapshot v2 has no normalized statement JSONL")
    lines = _json_lines(lines_path)

    filing = manifest["filings"][0]
    expected = sum(int(item.get("loaded_row_count", 0)) for item in manifest["coverage"])
    if len(lines) != expected:
        raise LoadV2Error("snapshot v2 line count does not match coverage")

    seen: set[tuple[str, str]] = set()
    for row in lines:
        if row.get("rcept_no") != filing["rcept_no"]:
            raise LoadV2Error("snapshot v2 has mismatched filing")
        if row.get("fs_div") not in {"CFS", "OFS"}:
            raise LoadV2Error("snapshot v2 has unsupported fs_div")
        if row.get("sj_div") not in {"BS", "IS", "CIS", "CF", "SCE"}:
            raise LoadV2Error("snapshot v2 has unsupported statement scope")
        if not re.fullmatch(r"[0-9a-f]{64}", str(row.get("row_hash", ""))):
            raise LoadV2Error("snapshot v2 has invalid row hash")
        key = (str(row["fs_div"]), str(row["row_hash"]))
        if key in seen:
            raise LoadV2Error("snapshot v2 has duplicate statement hash")
        seen.add(key)
        if str(row.get("currency", "")) != "KRW":
            raise LoadV2Error("snapshot v2 has invalid statement currency")
        for field in AMOUNT_FIELDS:
            value = row.get(field)
            if value is not None and not isinstance(value, int):
                raise LoadV2Error("snapshot v2 statement amount must be numeric or null")
        for field in ("thstrm_nm", "frmtrm_nm", "frmtrm_q_nm", "bfefrmtrm_nm"):
            if field in row and row[field] is not None and not isinstance(row[field], str):
                raise LoadV2Error("snapshot v2 statement descriptor is invalid")

    return manifest, lines


def _default_connect(database_url: str):
    try:
        import psycopg
    except ImportError:
        raise LoadV2Error("psycopg is required for schema-v2 loading") from None
    return psycopg.connect(database_url)


def _document_file_paths(document_archive: bytes) -> list[str]:
    if not zipfile.is_zipfile(io.BytesIO(document_archive)):
        raise LoadV2Error("snapshot v2 document archive is invalid")
    with zipfile.ZipFile(io.BytesIO(document_archive)) as archive:
        paths: list[str] = []
        for info in sorted(archive.infolist(), key=lambda item: item.filename.replace("\\", "/")):
            if info.is_dir():
                continue
            path = info.filename.replace("\\", "/")
            if path in paths:
                raise LoadV2Error("snapshot v2 document archive contains duplicate paths")
            if Path(path).suffix.casefold() in SUPPORTED_DOCUMENT_SUFFIXES:
                paths.append(path)
    return paths


def _coverage_for_v2(
    manifest: Mapping[str, Any],
    snapshot_dir: Path,
    document_path: Path | None = None,
) -> tuple[str, int, int]:
    chunks_file = next(
        (f for f in manifest["files"] if f["path"] == "normalized/source_chunks.json"),
        None,
    )
    if chunks_file is not None:
        payload = json.loads((snapshot_dir / chunks_file["path"]).read_text(encoding="utf-8"))
        status = str(payload.get("coverage_status"))
        if status in {"FULL_TEXT", "PARTIAL_TEXT", "SOURCE_NOT_AVAILABLE"}:
            return (
                status,
                int(payload.get("covered_document_files", 0)),
                int(payload.get("total_supported_document_files", 0)),
            )
    if document_path is None:
        return "SOURCE_NOT_AVAILABLE", 0, 0
    try:
        total = len(_document_file_paths(document_path.read_bytes()))
        return ("PARTIAL_TEXT" if total else "SOURCE_NOT_AVAILABLE", 0, total)
    except LoadV2Error:
        return "SOURCE_NOT_AVAILABLE", 0, 0


def _snapshot_narrative_status(source_status: str) -> str:
    if source_status in {"FULL_TEXT", "PARTIAL_TEXT", "SOURCE_NOT_AVAILABLE"}:
        return source_status
    return "SOURCE_NOT_AVAILABLE"


def _source_chunk_insert_columns(has_revision_fk: bool) -> tuple[str, str]:
    if has_revision_fk:
        columns = (
            "chunk_id, snapshot_id, filing_revision_id, rcept_no, corp_code, "
            "business_year, section_path, chunk_no, content, content_sha256, "
            "embedding_model, embedding, source_url, evidence_status"
        )
        values = (
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s, %s)"
        )
    else:
        columns = (
            "chunk_id, snapshot_id, rcept_no, corp_code, business_year, "
            "section_path, chunk_no, content, content_sha256, embedding_model, "
            "embedding, source_url, evidence_status"
        )
        values = (
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s, %s)"
        )
    return columns, values


def load_snapshot_v2(
    snapshot_dir: Path,
    database_url: str,
    connect_factory: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    if not database_url.strip():
        raise LoadV2Error("DATABASE_URL is missing")

    manifest, lines = validate_snapshot_v2(snapshot_dir)
    snapshot_id = manifest["snapshot_id"]
    filing = manifest["filings"][0]

    files = {item["path"]: item for item in manifest["files"]}
    document_file = files.get("raw/document.zip")
    source_chunks_file = files.get("normalized/source_chunks.json")

    chunks: list[dict[str, Any]] = []
    coverage_status = "SOURCE_NOT_AVAILABLE"
    processed_file_count = 0
    retrievable_file_count = 0

    if document_file is not None:
        coverage_status, processed_file_count, retrievable_file_count = _coverage_for_v2(
            manifest,
            snapshot_dir,
            snapshot_dir / document_file["path"],
        )

    if source_chunks_file is not None:
        try:
            source_payload = json.loads(
                (snapshot_dir / source_chunks_file["path"]).read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LoadV2Error("source chunk payload is invalid") from exc
        chunks = _validate_source_chunks_v2(source_payload, manifest)
        coverage_status = str(source_payload["coverage_status"])
        processed_file_count = int(source_payload.get("covered_document_files", 0))
        retrievable_file_count = int(source_payload.get("total_supported_document_files", 0))

    filing_document_sha256 = (
        document_file["sha256"] if document_file is not None else sha256_bytes(b"")
    )

    connection = None
    try:
        connection = (connect_factory or _default_connect)(database_url)
        with connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO snapshot_import (
                        snapshot_id, manifest_schema_version, source_system,
                        acquisition_mode, evidence_status, acquired_at,
                        manifest_path, manifest_json, narrative_corpus_status,
                        narrative_covered_files, narrative_total_files
                    ) VALUES (%s, %s, 'OpenDART', 'LIVE_API',
                              'OFFICIAL_API_VERIFIED', %s, %s, %s::jsonb,
                              %s, %s, %s)
                    ON CONFLICT (snapshot_id) DO NOTHING
                    """,
                    (
                        snapshot_id,
                        manifest["schema_version"],
                        manifest["provenance"]["acquired_at"],
                        str((snapshot_dir / "manifest.json").resolve()),
                        json.dumps(manifest, ensure_ascii=False),
                        _snapshot_narrative_status(coverage_status),
                        processed_file_count,
                        retrievable_file_count,
                    ),
                )

                for company in manifest["companies"]:
                    cursor.execute(
                        """
                        INSERT INTO company (corp_code, stock_code, corp_name)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (corp_code) DO NOTHING
                        """,
                        (company["corp_code"], company["stock_code"], company["corp_name"]),
                    )

                cursor.execute(
                    """
                    INSERT INTO filing (
                        rcept_no, snapshot_id, corp_code, report_name,
                        business_year, reprt_code, fs_div, period_start,
                        period_end, filing_date, source_url, document_sha256,
                        evidence_status
                    ) VALUES (%s, %s, %s, %s, %s, %s, 'CFS', %s, %s,
                              %s, %s, %s, 'OFFICIAL_API_VERIFIED')
                    ON CONFLICT (snapshot_id, rcept_no) DO NOTHING
                    """,
                    (
                        filing["rcept_no"],
                        snapshot_id,
                        filing["corp_code"],
                        filing["report_name"],
                        filing["business_year"],
                        filing["reprt_code"],
                        filing["period_start"],
                        filing["period_end"],
                        filing["filing_date"],
                        filing["source_url"],
                        filing_document_sha256,
                    ),
                )

                for revision in manifest["revisions"]:
                    cursor.execute(
                        """
                        INSERT INTO filing_revision (
                            snapshot_id, rcept_no, corp_code, business_year,
                            reprt_code, filing_date, correction_kind,
                            selected_latest, source_url, revision_json
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                        ON CONFLICT (snapshot_id, rcept_no) DO NOTHING
                        """,
                        (
                            snapshot_id,
                            revision["rcept_no"],
                            filing["corp_code"],
                            filing["business_year"],
                            filing["reprt_code"],
                            revision["filing_date"],
                            revision["correction_kind"],
                            bool(revision["selected_latest"]),
                            revision["source_url"],
                            json.dumps(revision, ensure_ascii=False),
                        ),
                    )

                cursor.execute(
                    """
                    SELECT filing_revision_id FROM filing_revision
                    WHERE snapshot_id = %s AND rcept_no = %s
                    """,
                    (snapshot_id, filing["rcept_no"]),
                )
                selected_revision_row = cursor.fetchone()
                if selected_revision_row is None:
                    raise LoadV2Error("selected filing revision was not loaded")
                selected_revision_id = selected_revision_row[0]

                columns = [
                    "snapshot_id", "filing_revision_id", "rcept_no", "corp_code",
                    "business_year", "reprt_code", "fs_div", "sj_div", "account_id",
                    "account_nm", "account_detail", "ord", "currency", "thstrm_nm",
                    "thstrm_amount_raw", "thstrm_amount", "thstrm_add_amount_raw",
                    "thstrm_add_amount", "frmtrm_nm", "frmtrm_amount_raw", "frmtrm_amount",
                    "frmtrm_q_nm", "frmtrm_q_amount_raw", "frmtrm_q_amount",
                    "frmtrm_add_amount_raw", "frmtrm_add_amount", "bfefrmtrm_nm",
                    "bfefrmtrm_amount_raw", "bfefrmtrm_amount", "raw_json", "row_hash",
                ]
                placeholders = ", ".join(["%s"] * len(columns))
                statement_sql = (
                    f"INSERT INTO financial_statement_line ({', '.join(columns)}) "
                    f"VALUES ({placeholders}) ON CONFLICT (snapshot_id, rcept_no, fs_div, row_hash) DO NOTHING"
                )
                for row in lines:
                    cursor.execute(
                        statement_sql,
                        (
                            snapshot_id,
                            selected_revision_id,
                            row["rcept_no"],
                            row["corp_code"],
                            row["business_year"],
                            row["reprt_code"],
                            row["fs_div"],
                            row["sj_div"],
                            row["account_id"],
                            row["account_nm"],
                            row.get("account_detail"),
                            row["ord"],
                            row.get("currency", "KRW"),
                            row.get("thstrm_nm"),
                            row.get("thstrm_amount_raw"),
                            row.get("thstrm_amount"),
                            row.get("thstrm_add_amount_raw"),
                            row.get("thstrm_add_amount"),
                            row.get("frmtrm_nm"),
                            row.get("frmtrm_amount_raw"),
                            row.get("frmtrm_amount"),
                            row.get("frmtrm_q_nm"),
                            row.get("frmtrm_q_amount_raw"),
                            row.get("frmtrm_q_amount"),
                            row.get("frmtrm_add_amount_raw"),
                            row.get("frmtrm_add_amount"),
                            row.get("bfefrmtrm_nm"),
                            row.get("bfefrmtrm_amount_raw"),
                            row.get("bfefrmtrm_amount"),
                            json.dumps(row, ensure_ascii=False),
                            row["row_hash"],
                        ),
                    )

                for state in manifest["coverage"]:
                    cursor.execute(
                        """
                        INSERT INTO data_coverage (
                            snapshot_id, corp_code, business_year, reprt_code,
                            fs_div, status, api_status_code, rcept_no,
                            source_row_count, loaded_row_count, checked_at, detail_json
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, '{}'::jsonb)
                        ON CONFLICT (snapshot_id, corp_code, business_year, reprt_code, fs_div, checked_at)
                        DO NOTHING
                        """,
                        (
                            snapshot_id,
                            filing["corp_code"],
                            filing["business_year"],
                            filing["reprt_code"],
                            state["fs_div"],
                            state["status"],
                            state.get("api_status_code"),
                            filing["rcept_no"],
                            state.get("source_row_count", 0),
                            state.get("loaded_row_count", 0),
                            manifest["provenance"]["acquired_at"],
                        ),
                    )

                cursor.execute(
                    "SELECT 1 FROM information_schema.columns WHERE table_name = 'source_chunk' AND column_name = 'filing_revision_id'"
                )
                has_source_chunk_revision_fk = cursor.fetchone() is not None
                chunk_columns, chunk_values = _source_chunk_insert_columns(
                    has_source_chunk_revision_fk
                )
                cursor.execute(
                    """
                    INSERT INTO narrative_coverage (
                        snapshot_id, filing_revision_id, status,
                        retrievable_file_count, processed_file_count, chunk_count,
                        checked_at, detail_json
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, '{}'::jsonb)
                    ON CONFLICT (snapshot_id, filing_revision_id) DO NOTHING
                    """,
                    (
                        snapshot_id,
                        selected_revision_id,
                        coverage_status,
                        retrievable_file_count,
                        processed_file_count,
                        len(chunks),
                        manifest["provenance"]["acquired_at"],
                    ),
                )
                for row in chunks:
                    if has_source_chunk_revision_fk:
                        sql = (
                            f"INSERT INTO source_chunk ({chunk_columns}) VALUES {chunk_values} "
                            "ON CONFLICT (chunk_id) DO NOTHING"
                        )
                        args = (
                            row["chunk_id"],
                            snapshot_id,
                            selected_revision_id,
                            row["rcept_no"],
                            row["corp_code"],
                            row["business_year"],
                            row["section_path"],
                            row["chunk_no"],
                            row["content"],
                            row["content_sha256"],
                            "intfloat/multilingual-e5-small",
                            _vector_literal(row["embedding"]),
                            row["source_url"],
                            "OFFICIAL_API_VERIFIED",
                        )
                    else:
                        sql = (
                            f"INSERT INTO source_chunk ({chunk_columns}) VALUES {chunk_values} "
                            "ON CONFLICT (chunk_id) DO NOTHING"
                        )
                        args = (
                            row["chunk_id"],
                            snapshot_id,
                            row["rcept_no"],
                            row["corp_code"],
                            row["business_year"],
                            row["section_path"],
                            row["chunk_no"],
                            row["content"],
                            row["content_sha256"],
                            "intfloat/multilingual-e5-small",
                            _vector_literal(row["embedding"]),
                            row["source_url"],
                            "OFFICIAL_API_VERIFIED",
                        )
                    cursor.execute(sql, args)
    except LoadV2Error:
        raise
    except Exception as exc:
        raise LoadV2Error(f"database import failed ({type(exc).__name__})") from None
    finally:
        if connection is not None and hasattr(connection, "close"):
            connection.close()

    return {
        "snapshot_id": snapshot_id,
        "financial_statement_lines": len(lines),
        "coverage": len(manifest["coverage"]),
        "chunks": len(chunks),
        "source_snapshot_id": manifest["snapshot_id"],
    }


def load_no_filing_coverage(
    *,
    database_url: str,
    corp_code: str,
    business_year: int,
    report_code: str,
    status: str,
    api_status_code: str | None = None,
    checked_at: str | None = None,
    connect_factory: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    if not database_url.strip():
        raise LoadV2Error("DATABASE_URL is missing")
    if status not in {"NOT_FILED", "NOT_YET_FILED", "SOURCE_NOT_AVAILABLE"}:
        raise LoadV2Error("no filing coverage status is invalid")
    if report_code not in {"11011", "11013", "11012", "11014"}:
        raise LoadV2Error("report code is invalid")
    if not re.fullmatch(r"[0-9]{8}", str(corp_code)):
        raise LoadV2Error("corp code is invalid")
    if not isinstance(business_year, int) or business_year < 2015:
        raise LoadV2Error("business year is invalid")

    timestamp = checked_at or datetime.now(timezone.utc).isoformat()

    connection = None
    try:
        connection = (connect_factory or _default_connect)(database_url)
        with connection:
            with connection.cursor() as cursor:
                for fs_div in ("CFS", "OFS"):
                    cursor.execute(
                        """
                        INSERT INTO data_coverage (
                            snapshot_id, corp_code, business_year, reprt_code,
                            fs_div, status, api_status_code, rcept_no,
                            source_row_count, loaded_row_count, checked_at, detail_json
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, '{}'::jsonb)
                        ON CONFLICT (snapshot_id, corp_code, business_year, reprt_code, fs_div, checked_at)
                        DO NOTHING
                        """,
                        (
                            None,
                            corp_code,
                            business_year,
                            report_code,
                            fs_div,
                            status,
                            api_status_code if status == "SOURCE_NOT_AVAILABLE" else None,
                            None,
                            0,
                            0,
                            timestamp,
                        ),
                    )
    except Exception as exc:
        raise LoadV2Error(f"database import failed ({type(exc).__name__})") from None
    finally:
        if connection is not None and hasattr(connection, "close"):
            connection.close()

    return {
        "snapshot_id": None,
        "coverage": 2,
        "status": status,
        "corp_code": corp_code,
        "business_year": business_year,
        "report_code": report_code,
    }
