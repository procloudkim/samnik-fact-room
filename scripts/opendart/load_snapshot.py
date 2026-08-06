#!/usr/bin/env python3
"""Load one validated LIVE_API snapshot into the canonical PostgreSQL schema."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

if __package__:
    from .sync import (
        SyncError,
        CompanySpec,
        build_source_chunk_id,
        build_snapshot_id,
        canonical_json_bytes,
        sha256_bytes,
        validate_manifest_shape,
    )
else:
    from sync import (  # type: ignore[no-redef]
        SyncError,
        CompanySpec,
        build_source_chunk_id,
        build_snapshot_id,
        canonical_json_bytes,
        sha256_bytes,
        validate_manifest_shape,
    )


class LoadError(RuntimeError):
    """A credential-safe snapshot loading error."""


ConnectFactory = Callable[[str], Any]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise LoadError(f"invalid JSON file in snapshot: {path.name}") from None
    if not isinstance(value, dict):
        raise LoadError(f"snapshot JSON must be an object: {path.name}")
    return value


def validate_live_snapshot(snapshot_dir: Path) -> tuple[dict[str, Any], dict[str, Path]]:
    """Verify official provenance, content address, and every declared file hash."""

    snapshot_dir = snapshot_dir.resolve()
    manifest = _read_json(snapshot_dir / "manifest.json")
    try:
        validate_manifest_shape(manifest)
    except SyncError as exc:
        raise LoadError(str(exc)) from None
    provenance = manifest["provenance"]
    if (
        provenance.get("acquisition_mode") != "LIVE_API"
        or provenance.get("evidence_status") != "OFFICIAL_API_VERIFIED"
    ):
        raise LoadError(
            "snapshot is not OFFICIAL_API_VERIFIED LIVE_API evidence; synthetic import refused"
        )
    expected_id = build_snapshot_id(
        manifest["companies"],
        manifest["filings"],
        manifest["files"],
        provenance=manifest["provenance"],
        limitations=manifest["limitations"],
    )
    if manifest["snapshot_id"] != expected_id:
        raise LoadError("snapshot manifest content address is invalid")
    if snapshot_dir.name != expected_id:
        raise LoadError("snapshot directory name must equal the snapshot ID")

    verified_files: dict[str, Path] = {}
    for file_item in manifest["files"]:
        relative = str(file_item.get("path", "")).replace("\\", "/")
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts:
            raise LoadError("snapshot manifest contains an unsafe path")
        target = snapshot_dir / path
        if not target.is_file():
            raise LoadError(f"snapshot file is missing: {relative}")
        payload = target.read_bytes()
        if len(payload) != file_item.get("size_bytes"):
            raise LoadError(f"snapshot file size mismatch: {relative}")
        if sha256_bytes(payload) != file_item.get("sha256"):
            raise LoadError(f"snapshot file hash mismatch: {relative}")
        verified_files[relative] = target
    return manifest, verified_files


def validate_financial_fact(
    fact: Mapping[str, Any], manifest: Mapping[str, Any]
) -> dict[str, Any]:
    required = {
        "evidence_id",
        "corp_code",
        "rcept_no",
        "business_year",
        "metric_key",
        "account_id",
        "account_nm",
        "sj_div",
        "fs_div",
        "period_start",
        "period_end",
        "value_scope",
        "raw_amount",
        "amount_numeric",
        "currency",
        "unit_scale",
        "source_field",
        "evidence_status",
        "source_url",
    }
    missing = sorted(required - set(fact))
    if missing:
        raise LoadError(f"normalized financial fact is missing fields: {', '.join(missing)}")
    if fact["evidence_status"] != "OFFICIAL_API_VERIFIED":
        raise LoadError("normalized financial fact is not official API evidence")
    if fact["metric_key"] != "operating_profit":
        raise LoadError("normalized financial fact is outside the M0 metric scope")
    try:
        matching_filings = [
            item
            for item in manifest["filings"]
            if (
                str(item["corp_code"]),
                str(item["rcept_no"]),
                int(item["business_year"]),
            )
            == (fact["corp_code"], fact["rcept_no"], fact["business_year"])
        ]
    except (KeyError, TypeError, ValueError):
        raise LoadError("snapshot manifest filings are invalid") from None
    if len(matching_filings) != 1:
        raise LoadError("normalized financial fact does not match its manifest filing")
    filing = matching_filings[0]
    report_code = str(filing["reprt_code"])
    suffixes = {"11011": "", "11013": "Q1", "11012": "H1", "11014": "Q3"}
    if report_code not in suffixes:
        raise LoadError("normalized financial fact report code is unsupported")
    expected_scope = (
        "CONSOLIDATED_ANNUAL"
        if report_code == "11011"
        else "CONSOLIDATED_CUMULATIVE"
    )
    if fact["fs_div"] != "CFS" or fact["value_scope"] != expected_scope:
        raise LoadError("normalized financial fact is outside the supported CFS scope")
    if (
        fact["period_start"] != filing["period_start"]
        or fact["period_end"] != filing["period_end"]
    ):
        raise LoadError("normalized financial fact period does not match its filing")
    period_key = f"{fact['business_year']}{suffixes[report_code]}"
    expected_evidence_id = (
        f"fact:{_company_slug(fact['corp_code'])}:{period_key}:operating_profit"
    )
    if fact["evidence_id"] != expected_evidence_id:
        raise LoadError("normalized financial fact evidence_id is not stable")
    try:
        Decimal(str(fact["amount_numeric"]))
    except InvalidOperation:
        raise LoadError("normalized financial fact amount is invalid") from None
    if fact["currency"] != "KRW" or int(fact["unit_scale"]) <= 0:
        raise LoadError("normalized financial fact currency or unit scale is invalid")
    if not str(fact["source_url"]).startswith("https://dart.fss.or.kr/"):
        raise LoadError("normalized financial fact source URL is not DART")
    return dict(fact)


def _company_slug(corp_code: str) -> str:
    slugs = {"00126380": "samsung", "00164779": "sk-hynix"}
    try:
        return slugs[str(corp_code)]
    except KeyError:
        raise LoadError("company is outside the M0 allowlist") from None


def validate_source_chunks(
    payload: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    document_archive: bytes | None = None,
) -> list[dict[str, Any]]:
    """Validate the optional normalized/source_chunks.json import contract."""

    top_level_fields = {
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
    if set(payload) != top_level_fields:
        raise LoadError("source chunk payload fields do not match schema version 1.0")
    if payload.get("schema_version") != "1.0":
        raise LoadError("source chunk schema version is invalid")
    embedding_model = str(payload.get("embedding_model", "")).strip()
    if embedding_model != "intfloat/multilingual-e5-small":
        raise LoadError("source chunks require intfloat/multilingual-e5-small")
    if payload.get("embedding_dimensions") != 384:
        raise LoadError("source chunk embedding dimension declaration is invalid")
    if payload.get("embedding_normalized") is not True:
        raise LoadError("source chunk embeddings must be declared L2-normalized")
    if payload.get("embedding_input_prefix") != "passage: ":
        raise LoadError("source chunk embedding prefix is invalid")
    source_snapshot_id = str(payload.get("source_snapshot_id", ""))
    if not re.fullmatch(r"[0-9a-f]{64}", source_snapshot_id):
        raise LoadError("source chunk parent snapshot ID is invalid")
    try:
        source_chunk_entries = [
            item
            for item in manifest["files"]
            if item.get("path") == "normalized/source_chunks.json"
        ]
        parent_files = [
            item
            for item in manifest["files"]
            if item.get("path") != "normalized/source_chunks.json"
        ]
        expected_parent_id = build_snapshot_id(
            manifest["companies"],
            manifest["filings"],
            parent_files,
            provenance=manifest["provenance"],
            limitations=manifest["limitations"],
        )
    except (KeyError, TypeError, ValueError):
        raise LoadError("source chunk parent snapshot descriptor is invalid") from None
    if len(source_chunk_entries) != 1 or source_snapshot_id != expected_parent_id:
        raise LoadError("source chunk parent snapshot ID does not match its manifest")
    coverage_status = payload.get("coverage_status")
    covered_paths = payload.get("covered_document_paths")
    supported_paths = payload.get("supported_document_paths")
    covered_files = payload.get("covered_document_files")
    total_files = payload.get("total_supported_document_files")
    if (
        coverage_status not in {"NOT_BUILT", "PARTIAL_TEXT", "FULL_TEXT"}
        or isinstance(covered_files, bool)
        or not isinstance(covered_files, int)
        or covered_files < 0
        or isinstance(total_files, bool)
        or not isinstance(total_files, int)
        or total_files < 0
        or covered_files > total_files
        or not isinstance(covered_paths, list)
        or not isinstance(supported_paths, list)
        or any(not isinstance(path, str) or not path for path in covered_paths)
        or any(not isinstance(path, str) or not path for path in supported_paths)
        or covered_paths != sorted(set(covered_paths))
        or supported_paths != sorted(set(supported_paths))
        or covered_files != len(covered_paths)
        or total_files != len(supported_paths)
        or not set(covered_paths).issubset(supported_paths)
    ):
        raise LoadError("source chunk coverage declaration is invalid")
    if coverage_status == "FULL_TEXT" and not (
        total_files > 0
        and covered_files == total_files
        and covered_paths == supported_paths
    ):
        raise LoadError("FULL_TEXT source coverage counts do not match")
    if coverage_status == "PARTIAL_TEXT" and not (
        total_files > 0 and 0 < covered_files < total_files
    ):
        raise LoadError("PARTIAL_TEXT source coverage counts do not match")
    if coverage_status == "NOT_BUILT":
        raise LoadError("a source chunks file cannot declare NOT_BUILT coverage")
    chunking = payload.get("chunking")
    if not isinstance(chunking, dict) or set(chunking) != {
        "algorithm",
        "max_chars",
        "overlap_chars",
    }:
        raise LoadError("source chunking declaration is invalid")
    max_chars = chunking.get("max_chars")
    overlap_chars = chunking.get("overlap_chars")
    if (
        chunking.get("algorithm") != "fixed_unicode_chars_v1"
        or isinstance(max_chars, bool)
        or not isinstance(max_chars, int)
        or max_chars < 32
        or isinstance(overlap_chars, bool)
        or not isinstance(overlap_chars, int)
        or overlap_chars < 0
        or overlap_chars >= max_chars
    ):
        raise LoadError("source chunking parameters are invalid")
    raw_chunks = payload.get("chunks")
    if not isinstance(raw_chunks, list) or not raw_chunks:
        raise LoadError("source chunks payload has no non-empty chunks list")
    filing_keys = {
        (item["corp_code"], item["rcept_no"], item["business_year"])
        for item in manifest["filings"]
    }
    validated: list[dict[str, Any]] = []
    seen_chunk_ids: set[str] = set()
    seen_positions: set[tuple[str, str, int]] = set()
    for raw_chunk in raw_chunks:
        if not isinstance(raw_chunk, dict):
            raise LoadError("source chunks payload contains a non-object chunk")
        chunk = dict(raw_chunk)
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
        if set(chunk) != required:
            raise LoadError("source chunk fields do not match schema version 1.0")
        if chunk["evidence_status"] != "OFFICIAL_API_VERIFIED":
            raise LoadError("source chunk is not official API evidence")
        corp_code = chunk["corp_code"]
        rcept_no = chunk["rcept_no"]
        business_year = chunk["business_year"]
        section_path = chunk["section_path"]
        chunk_no = chunk["chunk_no"]
        if (
            not isinstance(corp_code, str)
            or not re.fullmatch(r"[0-9]{8}", corp_code)
            or not isinstance(rcept_no, str)
            or not re.fullmatch(r"[0-9]{14}", rcept_no)
            or isinstance(business_year, bool)
            or not isinstance(business_year, int)
            or not 1999 <= business_year <= 2100
            or not isinstance(section_path, str)
            or not section_path.strip()
            or isinstance(chunk_no, bool)
            or not isinstance(chunk_no, int)
            or chunk_no < 0
        ):
            raise LoadError("source chunk filing metadata is invalid")
        if (corp_code, rcept_no, business_year) not in filing_keys:
            raise LoadError("source chunk does not match its manifest filing")
        content = chunk["content"]
        if not isinstance(content, str) or not content:
            raise LoadError("source chunk content is empty")
        content_hash = sha256_bytes(content.encode("utf-8"))
        if chunk["content_sha256"] != content_hash:
            raise LoadError("source chunk content hash is invalid")
        embedding = chunk["embedding"]
        if (
            not isinstance(embedding, list)
            or len(embedding) != 384
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                for value in embedding
            )
        ):
            raise LoadError("source chunk embedding must contain 384 finite numbers")
        values = [float(value) for value in embedding]
        if any(abs(value) > 1.000001 for value in values):
            raise LoadError("source chunk embedding must be L2-normalized")
        norm = math.sqrt(math.fsum(value * value for value in values))
        if not math.isclose(norm, 1.0, rel_tol=1e-6, abs_tol=1e-6):
            raise LoadError("source chunk embedding must be L2-normalized")
        embedding_hash = sha256_bytes(canonical_json_bytes(values))
        expected_chunk_id = build_source_chunk_id(
            source_snapshot_id=source_snapshot_id,
            rcept_no=rcept_no,
            corp_code=corp_code,
            business_year=business_year,
            section_path=section_path,
            chunk_no=chunk_no,
            content_sha256=content_hash,
            embedding_model=embedding_model,
            embedding_sha256=embedding_hash,
        )
        if (
            chunk["embedding_sha256"] != embedding_hash
            or chunk["chunk_id"] != expected_chunk_id
        ):
            raise LoadError("source chunk embedding hash or provenance ID is invalid")
        if not isinstance(chunk["source_url"], str) or not chunk[
            "source_url"
        ].startswith("https://dart.fss.or.kr/"):
            raise LoadError("source chunk source URL is not DART")
        chunk_id = str(chunk["chunk_id"])
        position = (
            rcept_no,
            section_path,
            chunk_no,
        )
        if chunk_id in seen_chunk_ids or position in seen_positions:
            raise LoadError("source chunks contain a duplicate ID or section position")
        seen_chunk_ids.add(chunk_id)
        seen_positions.add(position)
        chunk["embedding_model"] = embedding_model
        validated.append(chunk)

    if document_archive is None:
        raise LoadError("raw document archive is required to verify source chunks")
    if __package__:
        from .prepare_narrative import (
            PreparationError,
            chunk_sections,
            extract_archive_sections,
        )
    else:
        from prepare_narrative import (  # type: ignore[no-redef]
            PreparationError,
            chunk_sections,
            extract_archive_sections,
        )
    try:
        companies = manifest["companies"]
        filings = manifest["filings"]
        if len(companies) != 1 or len(filings) != 1:
            raise LoadError("source chunk archive verification requires one filing")
        company_item = companies[0]
        filing_item = filings[0]
        company = CompanySpec(
            slug=str(company_item["stock_code"]),
            corp_code=str(company_item["corp_code"]),
            corp_name=str(company_item["corp_name"]),
            stock_code=str(company_item["stock_code"]),
            expected_receipts={},
        )
        if company.corp_code != str(filing_item["corp_code"]):
            raise LoadError("source chunk company and filing do not match")
        extraction = extract_archive_sections(
            document_archive,
            company,
            int(filing_item["business_year"]),
        )
        expected_drafts = chunk_sections(
            extraction.sections,
            max_chars=max_chars,
            overlap_chars=overlap_chars,
        )
    except LoadError:
        raise
    except (KeyError, TypeError, ValueError, PreparationError, SyncError) as exc:
        raise LoadError(f"source chunk archive verification failed: {exc}") from None
    if (
        list(extraction.covered_document_paths) != covered_paths
        or list(extraction.supported_document_paths) != supported_paths
        or extraction.covered_document_files != covered_files
        or extraction.total_supported_document_files != total_files
    ):
        raise LoadError("source chunk coverage does not match raw document archive")
    expected_projection = [
        (draft.section_path, draft.chunk_no, draft.content)
        for draft in expected_drafts
    ]
    actual_projection = [
        (chunk["section_path"], chunk["chunk_no"], chunk["content"])
        for chunk in validated
    ]
    if actual_projection != expected_projection:
        raise LoadError("source chunks do not match deterministic archive chunking")
    return validated


def _vector_literal(values: Sequence[int | float]) -> str:
    return "[" + ",".join(format(float(value), ".17g") for value in values) + "]"


def _default_connect(database_url: str) -> Any:
    try:
        import psycopg
    except ImportError:
        raise LoadError(
            "psycopg is required; run with the project environment or `uv run --with psycopg[binary]`"
        ) from None
    return psycopg.connect(database_url)


def load_snapshot(
    snapshot_dir: Path,
    database_url: str,
    connect_factory: ConnectFactory | None = None,
) -> dict[str, int | str]:
    """Load one verified snapshot in a single database transaction."""

    if not database_url.strip():
        raise LoadError("DATABASE_URL is missing")
    manifest, files = validate_live_snapshot(snapshot_dir)
    fact_path = files.get("normalized/financial_fact.json")
    document_path = files.get("raw/document.zip")
    if fact_path is None or document_path is None:
        raise LoadError("snapshot lacks normalized financial fact or raw document archive")
    fact = validate_financial_fact(_read_json(fact_path), manifest)
    document_bytes = document_path.read_bytes()
    chunks_path = files.get("normalized/source_chunks.json")
    if chunks_path is not None:
        chunks_payload = _read_json(chunks_path)
        chunks = validate_source_chunks(
            chunks_payload,
            manifest,
            document_archive=document_bytes,
        )
        narrative_corpus_status = str(chunks_payload["coverage_status"])
        narrative_covered_files = int(chunks_payload["covered_document_files"])
        narrative_total_files = int(
            chunks_payload["total_supported_document_files"]
        )
    else:
        chunks = []
        narrative_corpus_status = "NOT_BUILT"
        narrative_covered_files = 0
        narrative_total_files = 0
    document_sha256 = sha256_bytes(document_bytes)
    snapshot_id = manifest["snapshot_id"]
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
                        str(Path(snapshot_dir).resolve() / "manifest.json"),
                        json.dumps(manifest, ensure_ascii=False),
                        narrative_corpus_status,
                        narrative_covered_files,
                        narrative_total_files,
                    ),
                )
                companies = {item["corp_code"]: item for item in manifest["companies"]}
                for company in companies.values():
                    cursor.execute(
                        """
                        INSERT INTO company (corp_code, stock_code, corp_name)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (corp_code) DO NOTHING
                        """,
                        (company["corp_code"], company["stock_code"], company["corp_name"]),
                    )
                for filing in manifest["filings"]:
                    cursor.execute(
                        """
                        INSERT INTO filing (
                            rcept_no, snapshot_id, corp_code, report_name,
                            business_year, reprt_code, fs_div, period_start,
                            period_end, filing_date, source_url, document_sha256,
                            evidence_status
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
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
                            filing["fs_div"],
                            filing["period_start"],
                            filing["period_end"],
                            filing["filing_date"],
                            filing["source_url"],
                            document_sha256,
                        ),
                    )
                cursor.execute(
                    """
                    INSERT INTO financial_fact (
                        evidence_id, snapshot_id, rcept_no, corp_code,
                        business_year, metric_key, account_id, account_nm,
                        sj_div, fs_div, period_start, period_end, value_scope,
                        raw_amount, amount_numeric, currency, source_field,
                        unit_scale, source_url, evidence_status
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                              %s, %s, %s, %s, %s, %s, %s, %s, %s,
                              'OFFICIAL_API_VERIFIED')
                    ON CONFLICT (evidence_id) DO NOTHING
                    """,
                    (
                        fact["evidence_id"], snapshot_id, fact["rcept_no"],
                        fact["corp_code"], fact["business_year"], fact["metric_key"],
                        fact["account_id"], fact["account_nm"], fact["sj_div"],
                        fact["fs_div"], fact["period_start"], fact["period_end"],
                        fact["value_scope"], fact["raw_amount"], fact["amount_numeric"],
                        fact["currency"], fact["source_field"], fact["unit_scale"],
                        fact["source_url"],
                    ),
                )
                for chunk in chunks:
                    cursor.execute(
                        """
                        INSERT INTO source_chunk (
                            chunk_id, snapshot_id, rcept_no, corp_code,
                            business_year, section_path, chunk_no, content,
                            content_sha256, embedding_model, embedding,
                            source_url, evidence_status
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                                  %s, %s::vector, %s, 'OFFICIAL_API_VERIFIED')
                        ON CONFLICT (chunk_id) DO NOTHING
                        """,
                        (
                            chunk["chunk_id"], snapshot_id, chunk["rcept_no"],
                            chunk["corp_code"], chunk["business_year"],
                            chunk["section_path"], chunk["chunk_no"], chunk["content"],
                            chunk["content_sha256"], chunk["embedding_model"],
                            _vector_literal(chunk["embedding"]), chunk["source_url"],
                        ),
                    )
    except LoadError:
        raise
    except Exception as exc:
        # Database exceptions may contain a URL or password. Return type only.
        raise LoadError(f"database import failed ({type(exc).__name__})") from None
    finally:
        if connection is not None and hasattr(connection, "close"):
            connection.close()
    return {
        "snapshot_id": snapshot_id,
        "companies": len(manifest["companies"]),
        "filings": len(manifest["filings"]),
        "financial_facts": 1,
        "source_chunks": len(chunks),
        "narrative_corpus_status": narrative_corpus_status,
        "narrative_covered_files": narrative_covered_files,
        "narrative_total_files": narrative_total_files,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Load one OFFICIAL_API_VERIFIED snapshot into PostgreSQL"
    )
    parser.add_argument("snapshot_dir", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        try:
            from scripts.opendart.snapshot_v2 import load_dotenv_key

            database_url = load_dotenv_key(Path(".env"), "DATABASE_URL") or ""
        except ImportError:
            database_url = ""
    try:
        try:
            manifest_version = _read_json(args.snapshot_dir / "manifest.json").get(
                "schema_version"
            )
        except LoadError:
            manifest_version = None
        if manifest_version == "2.0":
            from scripts.opendart.load_snapshot_v2 import load_snapshot_v2

            result = load_snapshot_v2(args.snapshot_dir, database_url)
        else:
            result = load_snapshot(args.snapshot_dir, database_url)
    except (LoadError, Exception) as exc:
        if exc.__class__.__name__ not in {"LoadError", "LoadV2Error"}:
            raise
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if "financial_statement_lines" in result:
        print(
            f"loaded snapshot={result['snapshot_id']} "
            f"statement_lines={result['financial_statement_lines']}"
        )
    else:
        print(
            f"loaded snapshot={result['snapshot_id']} facts={result['financial_facts']} "
            f"chunks={result['source_chunks']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
