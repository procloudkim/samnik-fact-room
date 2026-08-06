#!/usr/bin/env python3
"""Prepare deterministic OpenDART narrative chunks in a new snapshot.

The command is deliberately credential-free. It consumes one already
validated ``LIVE_API`` snapshot, extracts only supported markup files from the
declared ``raw/document.zip``, and atomically publishes a new content-addressed
snapshot containing local E5 passage embeddings. The input snapshot is never
modified.
"""

from __future__ import annotations

import argparse
import copy
import io
import json
import math
import os
import re
import shutil
import sys
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Protocol, Sequence

if __package__:
    from .load_snapshot import LoadError, validate_live_snapshot, validate_source_chunks
    from .load_snapshot_v2 import validate_snapshot_v2
    from .load_snapshot_v2 import _validate_source_chunks_v2
    from .sync import (
        CompanySpec,
        SUPPORTED_DOCUMENT_SUFFIXES,
        SyncError,
        _decode_document_text,
        _manifest_file,
        _verify_existing_snapshot,
        _write_bytes,
        build_snapshot_id,
        build_source_chunk_id,
        canonical_json_bytes,
        inspect_document_archive,
        sha256_bytes,
        validate_manifest_shape,
    )
else:
    from load_snapshot import (  # type: ignore[no-redef]
        LoadError,
        validate_live_snapshot,
        validate_source_chunks,
    )
    from load_snapshot_v2 import (  # type: ignore[no-redef]
        validate_snapshot_v2,
        _validate_source_chunks_v2,
    )
    from sync import (  # type: ignore[no-redef]
        CompanySpec,
        SUPPORTED_DOCUMENT_SUFFIXES,
        SyncError,
        _decode_document_text,
        _manifest_file,
        _verify_existing_snapshot,
        _write_bytes,
        build_snapshot_id,
        build_source_chunk_id,
        canonical_json_bytes,
        inspect_document_archive,
        sha256_bytes,
        validate_manifest_shape,
    )


EMBEDDING_MODEL = "intfloat/multilingual-e5-small"
EMBEDDING_DIMENSIONS = 384
EMBEDDING_INPUT_PREFIX = "passage: "
CHUNKING_ALGORITHM = "fixed_unicode_chars_v1"
DEFAULT_MAX_CHARS = 1_200
DEFAULT_OVERLAP_CHARS = 200
MAX_SOURCE_CHUNKS = 20_000


class PreparationError(RuntimeError):
    """A safe, user-displayable local preparation error."""


class PassageEmbedder(Protocol):
    """Injectable passage embedding boundary used by offline tests."""

    model_name: str

    def embed_passages(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...


@dataclass(frozen=True)
class DocumentSection:
    document_path: str
    section_path: str
    text: str


@dataclass(frozen=True)
class ArchiveExtraction:
    sections: tuple[DocumentSection, ...]
    covered_document_paths: tuple[str, ...]
    supported_document_paths: tuple[str, ...]
    covered_document_files: int
    total_supported_document_files: int


@dataclass(frozen=True)
class ChunkDraft:
    section_path: str
    chunk_no: int
    content: str


def _normalize_text(value: str) -> str:
    # HTMLParser already resolves character references, while the sync
    # fallback decoder does the same. Avoid a second unescape that could turn
    # a literal ``&lt;`` in the filing into a structural-looking ``<``.
    normalized = unicodedata.normalize("NFKC", value)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[\t\f\v ]+", " ", line).strip() for line in normalized.split("\n")]
    return "\n".join(line for line in lines if line)


def _normalize_inline(value: str) -> str:
    return re.sub(r"\s+", " ", _normalize_text(value)).strip()


def _decode_markup(payload: bytes) -> str:
    for encoding in ("utf-8", "cp949", "euc-kr"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    return payload.decode("utf-8", errors="replace")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise PreparationError("snapshot manifest is invalid")
    if not isinstance(value, dict):
        raise PreparationError("snapshot manifest must be an object")
    return value


def _load_input_snapshot(
    snapshot_dir: Path,
) -> tuple[dict[str, Any], dict[str, Path], str]:
    manifest = _read_json(snapshot_dir / "manifest.json")
    schema_version = str(manifest.get("schema_version", ""))
    if schema_version not in {"1.0", "2.0"}:
        raise PreparationError("snapshot schema version is unsupported")
    if schema_version == "1.0":
        return validate_live_snapshot(snapshot_dir) + (schema_version,)
    _, _ = validate_snapshot_v2(snapshot_dir)
    files: dict[str, Path] = {}
    for item in manifest["files"]:
        if not isinstance(item, dict):
            raise PreparationError("snapshot manifest file list is invalid")
        relative = str(item.get("path"))
        if not relative:
            raise PreparationError("snapshot manifest has an invalid file path")
        file_path = snapshot_dir / relative
        if not file_path.is_file():
            raise PreparationError(f"snapshot file is missing: {relative}")
        files[relative] = file_path
    return manifest, files, schema_version


def _rebuild_snapshot_id(
    manifest: Mapping[str, Any],
    enriched_files: Sequence[Mapping[str, Any]],
    schema_version: str,
) -> str:
    if schema_version == "1.0":
        return build_snapshot_id(
            manifest["companies"],
            manifest["filings"],
            enriched_files,
            provenance=manifest["provenance"],
            limitations=manifest["limitations"],
        )
    if schema_version == "2.0":
        import importlib
        if __package__:
            snapshot_v2_module = importlib.import_module(".snapshot_v2", package=__package__)
        else:
            snapshot_v2_module = importlib.import_module("snapshot_v2")
        descriptor = {
            key: value
            for key, value in manifest.items()
            if key != "snapshot_id"
        }
        descriptor["files"] = list(enriched_files)
        return snapshot_v2_module._snapshot_id(descriptor)
    raise PreparationError(f"unsupported snapshot schema version: {schema_version}")


def _verify_existing_snapshot_for_schema(
    path: Path,
    manifest: Mapping[str, Any],
    schema_version: str,
) -> None:
    if schema_version == "1.0":
        _verify_existing_snapshot(path, manifest)
        return
    if schema_version == "2.0":
        existing_manifest, _ = validate_snapshot_v2(path)
        if existing_manifest["snapshot_id"] != manifest["snapshot_id"]:
            raise PreparationError("existing snapshot ID does not match")
        return
    raise PreparationError(f"unsupported snapshot schema version: {schema_version}")


def _validate_enriched_snapshot(
    manifest: Mapping[str, Any],
    schema_version: str,
) -> None:
    if schema_version == "1.0":
        validate_manifest_shape(manifest)
        return
    if schema_version == "2.0":
        import importlib
        if __package__:
            snapshot_v2_module = importlib.import_module(
                ".snapshot_v2",
                package=__package__,
            )
        else:
            snapshot_v2_module = importlib.import_module("snapshot_v2")
        snapshot_v2_module.validate_v2_manifest(manifest)
        return
    raise PreparationError(f"unsupported snapshot schema version: {schema_version}")


class _SectionParser(HTMLParser):
    """Lenient XML/HTML text parser with deterministic heading provenance."""

    _BLOCK_TAGS = {
        "address",
        "article",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "li",
        "p",
        "section",
        "section-1",
        "section-2",
        "section-3",
        "section-4",
        "section-5",
        "section-6",
    }
    _CELL_TAGS = {"td", "th"}
    _SKIP_TAGS = {"script", "style"}
    _HEADING_RE = re.compile(r"^(?:h([1-6])|title(?:-([1-6]))?)$")
    _SECTION_RE = re.compile(r"^section-([1-6])$")

    def __init__(self, document_path: str) -> None:
        super().__init__(convert_charrefs=True)
        self.document_path = document_path
        self._headings: list[str] = []
        self._body: list[str] = []
        self._heading_parts: list[str] | None = None
        self._heading_level = 1
        self._section_levels: list[int] = []
        self._table_depth = 0
        self._table_rows: list[list[str]] = []
        self._current_row: list[str] | None = None
        self._current_cell: list[str] | None = None
        self._table_has_spans = False
        self._skip_depth = 0
        self.sections: list[DocumentSection] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        tag = tag.casefold()
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "table":
            if self._table_depth == 0:
                self._table_rows = []
                self._current_row = None
                self._current_cell = None
                self._table_has_spans = False
            self._table_depth += 1
            return
        if self._table_depth:
            if tag == "tr":
                self._finish_table_row()
                self._current_row = []
            elif tag in self._CELL_TAGS:
                self._finish_table_cell()
                if self._current_row is None:
                    self._current_row = []
                self._current_cell = []
                for name, value in attrs:
                    if name.casefold() in {"rowspan", "colspan"} and str(value or "1") != "1":
                        self._table_has_spans = True
            elif self._current_cell is not None and tag in self._BLOCK_TAGS:
                self._current_cell.append(" ")
            return
        section_match = self._SECTION_RE.fullmatch(tag)
        if section_match:
            self._section_levels.append(int(section_match.group(1)))
        heading_match = self._HEADING_RE.fullmatch(tag)
        if heading_match:
            self._flush_body()
            explicit_level = heading_match.group(1) or heading_match.group(2)
            self._heading_level = (
                int(explicit_level)
                if explicit_level
                else (self._section_levels[-1] if self._section_levels else 1)
            )
            self._heading_parts = []
        elif tag in self._BLOCK_TAGS:
            self._body.append("\n")

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in self._SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if self._table_depth:
            if tag in self._CELL_TAGS:
                self._finish_table_cell()
            elif tag == "tr":
                self._finish_table_row()
            elif tag == "table":
                self._table_depth -= 1
                if self._table_depth == 0:
                    self._finish_table_cell()
                    self._finish_table_row()
                    self._append_rendered_table()
            elif self._current_cell is not None and tag in self._BLOCK_TAGS:
                self._current_cell.append(" ")
            return
        if self._HEADING_RE.fullmatch(tag) and self._heading_parts is not None:
            heading = _normalize_inline(" ".join(self._heading_parts))
            if heading:
                level = max(1, min(6, self._heading_level))
                self._headings = self._headings[: level - 1]
                self._headings.append(heading)
            self._heading_parts = None
        elif tag in self._BLOCK_TAGS:
            self._body.append("\n")
        if self._SECTION_RE.fullmatch(tag) and self._section_levels:
            self._section_levels.pop()

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._table_depth:
            if self._current_cell is not None:
                self._current_cell.append(data)
        elif self._heading_parts is not None:
            self._heading_parts.append(data)
        else:
            self._body.append(data)

    def unknown_decl(self, data: str) -> None:
        # XML CDATA is visible filing text. HTMLParser deliberately delegates
        # unknown declarations, so preserve CDATA while ignoring other DTD
        # declarations (and therefore never resolving external entities).
        if data.casefold().startswith("cdata["):
            self.handle_data(data[6:])

    def close(self) -> None:
        super().close()
        if self._table_depth:
            self._table_depth = 0
            self._finish_table_cell()
            self._finish_table_row()
            self._append_rendered_table()
        if self._heading_parts is not None:
            heading = _normalize_inline(" ".join(self._heading_parts))
            if heading:
                self._headings = self._headings[: self._heading_level - 1]
                self._headings.append(heading)
            self._heading_parts = None
        self._flush_body()

    def _finish_table_cell(self) -> None:
        if self._current_cell is None:
            return
        if self._current_row is None:
            self._current_row = []
        self._current_row.append(_normalize_inline(" ".join(self._current_cell)))
        self._current_cell = None

    def _finish_table_row(self) -> None:
        self._finish_table_cell()
        if self._current_row is not None and any(self._current_row):
            self._table_rows.append(self._current_row)
        self._current_row = None

    def _append_rendered_table(self) -> None:
        if not self._table_rows:
            return
        original_rows = [" | ".join(row) for row in self._table_rows]
        mapped_rows = self._canonical_table_mappings(self._table_rows)
        self._body.append("\n" + "\n".join([*original_rows, *mapped_rows]) + "\n")
        self._table_rows = []

    def _canonical_table_mappings(self, rows: Sequence[Sequence[str]]) -> list[str]:
        """Map simple header/value tables without guessing across spans."""

        if self._table_has_spans or len(rows) < 2 or len(rows[0]) < 2:
            return []
        header = list(rows[0])
        width = len(header)
        if not header[0] or any(not cell for cell in header[1:]):
            return []
        cue = _normalize_inline(header[0]).casefold()
        segment_cues = {"segment", "division", "부문", "사업부", "사업부문"}
        known_segments = {"ds", "dx"}
        if cue in segment_cues or any(
            cell.casefold() in known_segments for cell in header[1:]
        ):
            dimension = "부문"
        elif cue in {"year", "연도"}:
            dimension = "연도"
        elif cue in {"period", "기간"}:
            dimension = "기간"
        else:
            # Unknown table semantics are preserved in the original rows but
            # not rewritten into potentially false header/value assertions.
            return []
        mapped: list[str] = []
        for row in rows[1:]:
            if len(row) != width or not row[0]:
                continue
            metric = row[0]
            for column in range(1, width):
                if row[column]:
                    mapped.append(
                        f"{dimension}={header[column]} | {metric}={row[column]}"
                    )
        return mapped

    def _flush_body(self) -> None:
        body = _normalize_text("".join(self._body))
        self._body.clear()
        if not body:
            return
        path_parts = [self.document_path, *self._headings]
        section_path = " > ".join(part for part in path_parts if part)
        self.sections.append(
            DocumentSection(
                document_path=self.document_path,
                section_path=section_path,
                text=body,
            )
        )


def extract_markup_sections(document_path: str, payload: bytes) -> list[DocumentSection]:
    """Extract normalized text and heading paths from one XML/HTML document."""

    parser = _SectionParser(document_path)
    try:
        parser.feed(_decode_markup(payload))
        parser.close()
    except (UnicodeError, ValueError):
        raise PreparationError("filing markup could not be parsed safely") from None
    if parser.sections:
        return parser.sections
    fallback = _normalize_text(_decode_document_text(payload))
    if not fallback:
        return []
    return [
        DocumentSection(
            document_path=document_path,
            section_path=document_path,
            text=fallback,
        )
    ]


def extract_archive_sections(
    archive_bytes: bytes,
    company: CompanySpec,
    business_year: int,
) -> ArchiveExtraction:
    """Validate the archive with the acquisition limits, then read markup only."""

    # This enforces the existing path, symlink, file-count, uncompressed-size,
    # company-identity, and year-identity invariants before any RAG extraction.
    inspect_document_archive(archive_bytes, company, business_year)
    sections: list[DocumentSection] = []
    seen_paths: set[str] = set()
    supported_document_paths: list[str] = []
    covered_document_paths: list[str] = []
    covered_document_files = 0
    total_supported_document_files = 0
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            infos = sorted(
                (info for info in archive.infolist() if not info.is_dir()),
                key=lambda info: info.filename.replace("\\", "/"),
            )
            for info in infos:
                normalized_path = info.filename.replace("\\", "/").lstrip("/")
                if not normalized_path:
                    raise PreparationError(
                        "OpenDART document archive contains an invalid path"
                    )
                if normalized_path in seen_paths:
                    raise PreparationError(
                        "OpenDART document archive contains a duplicate path"
                    )
                seen_paths.add(normalized_path)
                if (
                    PurePosixPath(normalized_path).suffix.casefold()
                    not in SUPPORTED_DOCUMENT_SUFFIXES
                ):
                    continue
                total_supported_document_files += 1
                supported_document_paths.append(normalized_path)
                sections.extend(extract_markup_sections(normalized_path, archive.read(info)))
                covered_document_files += 1
                covered_document_paths.append(normalized_path)
    except PreparationError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile):
        raise PreparationError("OpenDART document archive could not be read") from None
    if not sections:
        raise PreparationError("OpenDART document archive has no readable XML/HTML text")
    return ArchiveExtraction(
        sections=tuple(sections),
        covered_document_paths=tuple(sorted(covered_document_paths)),
        supported_document_paths=tuple(sorted(supported_document_paths)),
        covered_document_files=covered_document_files,
        total_supported_document_files=total_supported_document_files,
    )


def chunk_sections(
    sections: Sequence[DocumentSection],
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> list[ChunkDraft]:
    """Split normalized Unicode text with an exact deterministic overlap."""

    if (
        isinstance(max_chars, bool)
        or not isinstance(max_chars, int)
        or max_chars < 32
        or isinstance(overlap_chars, bool)
        or not isinstance(overlap_chars, int)
        or overlap_chars < 0
        or overlap_chars >= max_chars
    ):
        raise PreparationError("chunk size and overlap are invalid")
    chunks: list[ChunkDraft] = []
    next_number: dict[str, int] = {}
    for section in sections:
        content = _normalize_text(section.text)
        if not content:
            continue
        start = 0
        while start < len(content):
            end = min(start + max_chars, len(content))
            chunk_no = next_number.get(section.section_path, 0)
            chunks.append(
                ChunkDraft(
                    section_path=section.section_path,
                    chunk_no=chunk_no,
                    content=content[start:end],
                )
            )
            next_number[section.section_path] = chunk_no + 1
            if len(chunks) > MAX_SOURCE_CHUNKS:
                raise PreparationError("filing produced too many narrative chunks")
            if end == len(content):
                break
            start = end - overlap_chars
    if not chunks:
        raise PreparationError("filing did not produce any narrative chunks")
    return chunks


def normalize_embedding(values: Sequence[float]) -> list[float]:
    """Validate and L2-normalize one 384-dimensional embedding."""

    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise PreparationError("embedding provider returned an invalid vector")
    if len(values) != EMBEDDING_DIMENSIONS:
        raise PreparationError("embedding provider must return exactly 384 values")
    converted: list[float] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise PreparationError("embedding provider returned a non-numeric value")
        converted_value = float(value)
        if not math.isfinite(converted_value):
            raise PreparationError("embedding provider returned a non-finite value")
        converted.append(converted_value)
    scale = max(abs(value) for value in converted)
    if scale == 0.0:
        raise PreparationError("embedding provider returned a zero vector")
    scaled_norm = math.sqrt(math.fsum((value / scale) ** 2 for value in converted))
    norm = scale * scaled_norm
    return [value / norm for value in converted]


class SentenceTransformerEmbedder:
    """Lazy local adapter for multilingual-e5-small passage embeddings."""

    model_name = EMBEDDING_MODEL

    def __init__(
        self,
        *,
        device: str = "cpu",
        batch_size: int = 32,
        local_files_only: bool = False,
    ) -> None:
        if not device.strip():
            raise PreparationError("embedding device is missing")
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            raise PreparationError("embedding batch size is invalid")
        self._device = device
        self._batch_size = batch_size
        self._local_files_only = local_files_only
        self._model: Any | None = None

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            raise PreparationError(
                "sentence-transformers is required for narrative preparation"
            ) from None
        try:
            model = SentenceTransformer(
                self.model_name,
                device=self._device,
                local_files_only=self._local_files_only,
            )
            dimension = model.get_sentence_embedding_dimension()
        except Exception as exc:
            raise PreparationError(
                f"embedding model could not be loaded ({type(exc).__name__})"
            ) from None
        if dimension != EMBEDDING_DIMENSIONS:
            raise PreparationError("embedding model dimension is not 384")
        self._model = model
        return model

    def embed_passages(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        model = self._load()
        prefixed = [f"{EMBEDDING_INPUT_PREFIX}{text}" for text in texts]
        try:
            values = model.encode(
                prefixed,
                batch_size=self._batch_size,
                convert_to_numpy=True,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
        except Exception as exc:
            raise PreparationError(
                f"embedding generation failed ({type(exc).__name__})"
            ) from None
        return values.tolist() if hasattr(values, "tolist") else values


def build_source_chunks_payload(
    *,
    source_snapshot_id: str,
    filing: Mapping[str, Any],
    drafts: Sequence[ChunkDraft],
    embedder: PassageEmbedder,
    max_chars: int,
    overlap_chars: int,
    covered_document_paths: Sequence[str],
    supported_document_paths: Sequence[str],
    covered_document_files: int,
    total_supported_document_files: int,
) -> dict[str, Any]:
    """Embed chunk drafts and build the normalized loader contract."""

    if (
        isinstance(covered_document_files, bool)
        or not isinstance(covered_document_files, int)
        or isinstance(total_supported_document_files, bool)
        or not isinstance(total_supported_document_files, int)
        or total_supported_document_files < 1
        or covered_document_files != total_supported_document_files
        or covered_document_files != len(covered_document_paths)
        or total_supported_document_files != len(supported_document_paths)
        or list(covered_document_paths) != list(supported_document_paths)
    ):
        raise PreparationError("FULL_TEXT narrative coverage counts are invalid")
    if embedder.model_name != EMBEDDING_MODEL:
        raise PreparationError(
            "narrative preparation requires intfloat/multilingual-e5-small"
        )
    embeddings = embedder.embed_passages([draft.content for draft in drafts])
    if len(embeddings) != len(drafts):
        raise PreparationError("embedding provider returned the wrong vector count")
    chunks: list[dict[str, Any]] = []
    for draft, raw_embedding in zip(drafts, embeddings, strict=True):
        content_hash = sha256_bytes(draft.content.encode("utf-8"))
        normalized_embedding = normalize_embedding(raw_embedding)
        embedding_hash = sha256_bytes(canonical_json_bytes(normalized_embedding))
        chunk_id = build_source_chunk_id(
            source_snapshot_id=source_snapshot_id,
            rcept_no=str(filing["rcept_no"]),
            corp_code=str(filing["corp_code"]),
            business_year=int(filing["business_year"]),
            section_path=draft.section_path,
            chunk_no=draft.chunk_no,
            content_sha256=content_hash,
            embedding_model=EMBEDDING_MODEL,
            embedding_sha256=embedding_hash,
        )
        chunks.append(
            {
                "chunk_id": chunk_id,
                "rcept_no": str(filing["rcept_no"]),
                "corp_code": str(filing["corp_code"]),
                "business_year": int(filing["business_year"]),
                "section_path": draft.section_path,
                "chunk_no": draft.chunk_no,
                "content": draft.content,
                "content_sha256": content_hash,
                "embedding": normalized_embedding,
                "embedding_sha256": embedding_hash,
                "source_url": str(filing["source_url"]),
                "evidence_status": "OFFICIAL_API_VERIFIED",
            }
        )
    return {
        "schema_version": "1.0",
        "source_snapshot_id": source_snapshot_id,
        "coverage_status": "FULL_TEXT",
        "covered_document_paths": list(covered_document_paths),
        "supported_document_paths": list(supported_document_paths),
        "covered_document_files": covered_document_files,
        "total_supported_document_files": total_supported_document_files,
        "embedding_model": EMBEDDING_MODEL,
        "embedding_dimensions": EMBEDDING_DIMENSIONS,
        "embedding_normalized": True,
        "embedding_input_prefix": EMBEDDING_INPUT_PREFIX,
        "chunking": {
            "algorithm": CHUNKING_ALGORITHM,
            "max_chars": max_chars,
            "overlap_chars": overlap_chars,
        },
        "chunks": chunks,
    }


def _single_filing_context(
    manifest: Mapping[str, Any],
) -> tuple[CompanySpec, dict[str, Any]]:
    companies = manifest.get("companies")
    filings = manifest.get("filings")
    if (
        not isinstance(companies, list)
        or len(companies) != 1
        or not isinstance(filings, list)
        or len(filings) != 1
    ):
        raise PreparationError("narrative preparation requires one company and filing")
    company_record = companies[0]
    filing = dict(filings[0])
    if company_record.get("corp_code") != filing.get("corp_code"):
        raise PreparationError("snapshot company and filing do not match")
    company = CompanySpec(
        slug=str(company_record["stock_code"]),
        corp_code=str(company_record["corp_code"]),
        corp_name=str(company_record["corp_name"]),
        stock_code=str(company_record["stock_code"]),
        expected_receipts={},
    )
    return company, filing


def prepare_narrative_snapshot(
    snapshot_dir: Path,
    *,
    embedder: PassageEmbedder,
    output_root: Path | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> Path:
    """Create a new enriched snapshot without mutating the validated input."""

    input_path = snapshot_dir.resolve()
    destination_root = (output_root or input_path.parent).resolve()
    if destination_root == input_path or input_path in destination_root.parents:
        raise PreparationError("output root must not be inside the input snapshot")
    try:
        manifest, verified_files, schema_version = _load_input_snapshot(input_path)
    except LoadError as exc:
        raise PreparationError(str(exc)) from None
    except PreparationError as exc:
        raise
    except Exception as exc:
        raise PreparationError(str(exc)) from None
    if "normalized/source_chunks.json" in verified_files:
        raise PreparationError("input snapshot already contains narrative chunks")
    archive_path = verified_files.get("raw/document.zip")
    if archive_path is None:
        raise PreparationError("snapshot has no declared raw/document.zip")
    company, filing = _single_filing_context(manifest)
    archive_bytes = archive_path.read_bytes()
    try:
        extraction = extract_archive_sections(
            archive_bytes, company, int(filing["business_year"])
        )
    except SyncError as exc:
        raise PreparationError(str(exc)) from None
    drafts = chunk_sections(
        extraction.sections,
        max_chars=max_chars,
        overlap_chars=overlap_chars,
    )
    payload = build_source_chunks_payload(
        source_snapshot_id=str(manifest["snapshot_id"]),
        filing=filing,
        drafts=drafts,
        embedder=embedder,
        max_chars=max_chars,
        overlap_chars=overlap_chars,
        covered_document_paths=extraction.covered_document_paths,
        supported_document_paths=extraction.supported_document_paths,
        covered_document_files=extraction.covered_document_files,
        total_supported_document_files=extraction.total_supported_document_files,
    )
    chunks_bytes = canonical_json_bytes(payload)
    new_file = _manifest_file(
        "normalized/source_chunks.json",
        chunks_bytes,
        "LOCAL_DERIVATION",
    )
    enriched_manifest = copy.deepcopy(manifest)
    enriched_files = [dict(item) for item in manifest["files"]]
    enriched_files.append(new_file)
    enriched_manifest["files"] = enriched_files
    enriched_manifest["snapshot_id"] = _rebuild_snapshot_id(
        enriched_manifest,
        enriched_files,
        schema_version,
    )
    try:
        _validate_enriched_snapshot(enriched_manifest, schema_version)
        if schema_version == "1.0":
            validate_source_chunks(
                payload,
                enriched_manifest,
                document_archive=archive_bytes,
            )
        else:
            _validate_source_chunks_v2(payload, enriched_manifest)
    except (LoadError, SyncError, Exception) as exc:
        raise PreparationError(str(exc)) from None

    destination_root.mkdir(parents=True, exist_ok=True)
    target = destination_root / enriched_manifest["snapshot_id"]
    if target.exists():
        try:
            _verify_existing_snapshot_for_schema(target, enriched_manifest, schema_version)
            if schema_version == "1.0":
                validate_live_snapshot(target)
            else:
                validate_snapshot_v2(target)
        except (LoadError, SyncError) as exc:
            raise PreparationError(str(exc)) from None
        return target

    temporary = Path(
        tempfile.mkdtemp(prefix=".opendart-narrative-", dir=destination_root)
    )
    try:
        for file_item in manifest["files"]:
            relative_path = str(file_item["path"])
            _write_bytes(
                temporary,
                relative_path,
                verified_files[relative_path].read_bytes(),
            )
        _write_bytes(temporary, "normalized/source_chunks.json", chunks_bytes)
        _write_bytes(
            temporary,
            "manifest.json",
            canonical_json_bytes(enriched_manifest),
        )
        os.replace(temporary, target)
    except PreparationError:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    except (OSError, SyncError) as exc:
        shutil.rmtree(temporary, ignore_errors=True)
        if isinstance(exc, SyncError):
            raise PreparationError(str(exc)) from None
        raise PreparationError(
            f"enriched snapshot could not be published ({type(exc).__name__})"
        ) from None
    return target


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Add local multilingual-e5-small narrative chunks to a new immutable "
            "OpenDART snapshot"
        )
    )
    parser.add_argument("snapshot_dir", type=Path)
    parser.add_argument(
        "--output-root",
        type=Path,
        help="snapshot parent directory (default: input snapshot parent)",
    )
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    parser.add_argument("--overlap-chars", type=int, default=DEFAULT_OVERLAP_CHARS)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument(
        "--local-files-only",
        action="store_true",
        help="fail instead of downloading the public embedding model if uncached",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        embedder = SentenceTransformerEmbedder(
            device=args.device,
            batch_size=args.batch_size,
            local_files_only=args.local_files_only,
        )
        target = prepare_narrative_snapshot(
            args.snapshot_dir,
            embedder=embedder,
            output_root=args.output_root,
            max_chars=args.max_chars,
            overlap_chars=args.overlap_chars,
        )
    except PreparationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"prepared narrative snapshot={target.name} path={target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
