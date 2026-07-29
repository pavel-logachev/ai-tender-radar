"""Source-neutral adapter contract and a bounded local file implementation."""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.platform.contracts import CanonicalSourceRecord, SCHEMA_VERSION
from app.platform.versioning import sha256_text


MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_RECORDS = 5_000
MAX_COLUMNS = 100
MAX_CELL_CHARS = 200_000
SUPPORTED_SUFFIXES = {".csv", ".json", ".jsonl"}
KNOWN_FIELDS = {
    "schema_version",
    "source",
    "external_id",
    "title",
    "customer_name",
    "initial_price",
    "currency",
    "law",
    "region",
    "published_at",
    "deadline_at",
    "url",
    "raw",
}


class SourceCursor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    next_offset: int = Field(ge=0)


class SourcePage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    records: tuple[CanonicalSourceRecord, ...]
    next_cursor: SourceCursor | None
    source_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SourceAdapter(Protocol):
    def fetch(
        self,
        *,
        cursor: SourceCursor | None = None,
        limit: int = 500,
    ) -> SourcePage: ...


class SourceAdapterError(ValueError):
    pass


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _required_text(value: Any, *, field_name: str) -> str:
    text = _optional_text(value)
    if text is None:
        raise SourceAdapterError(f"{field_name} is required")
    return text


def _parse_decimal(value: Any) -> Decimal | None:
    text = _optional_text(value)
    if text is None:
        return None
    normalized = text.replace(" ", "")
    if "," in normalized and "." not in normalized:
        normalized = normalized.replace(",", ".")
    try:
        return Decimal(normalized)
    except InvalidOperation as exc:
        raise SourceAdapterError("initial_price must be a decimal number") from exc


def _parse_datetime(value: Any, *, field_name: str) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise SourceAdapterError(f"{field_name} must be an ISO 8601 datetime") from exc
    if parsed.tzinfo is None:
        raise SourceAdapterError(f"{field_name} must include a timezone")
    return parsed


def _parse_raw(value: Any, fallback: dict[str, Any]) -> dict[str, Any]:
    if value is None or value == "":
        return fallback
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise SourceAdapterError("raw must be a JSON object") from exc
        if isinstance(parsed, dict):
            return parsed
    raise SourceAdapterError("raw must be a JSON object")


def canonical_record_from_mapping(
    row: Mapping[str, Any],
    *,
    default_source: str,
) -> CanonicalSourceRecord:
    if len(row) > MAX_COLUMNS:
        raise SourceAdapterError(f"record has more than {MAX_COLUMNS} fields")
    for key, value in row.items():
        if len(str(key)) > 200:
            raise SourceAdapterError("field name exceeds 200 characters")
        if isinstance(value, str) and len(value) > MAX_CELL_CHARS:
            raise SourceAdapterError(f"field {key!r} exceeds {MAX_CELL_CHARS} characters")

    unknown = {str(key): value for key, value in row.items() if str(key) not in KNOWN_FIELDS}
    raw = _parse_raw(row.get("raw"), unknown)
    schema_version = _optional_text(row.get("schema_version")) or SCHEMA_VERSION
    if schema_version != SCHEMA_VERSION:
        raise SourceAdapterError(f"unsupported schema_version: {schema_version}")

    return CanonicalSourceRecord(
        schema_version=SCHEMA_VERSION,
        source=_required_text(row.get("source") or default_source, field_name="source").lower(),
        external_id=_required_text(row.get("external_id"), field_name="external_id"),
        title=_required_text(row.get("title"), field_name="title"),
        customer_name=_optional_text(row.get("customer_name")),
        initial_price=_parse_decimal(row.get("initial_price")),
        currency=(_optional_text(row.get("currency")) or "RUB").upper(),
        law=_optional_text(row.get("law")),
        region=_optional_text(row.get("region")),
        published_at=_parse_datetime(row.get("published_at"), field_name="published_at"),
        deadline_at=_parse_datetime(row.get("deadline_at"), field_name="deadline_at"),
        url=_optional_text(row.get("url")),
        raw=raw,
    )


class FileSourceAdapter:
    """Read a stable, size-bounded CSV, JSON, or JSONL source snapshot."""

    def __init__(
        self,
        path: str | Path,
        *,
        allowed_root: str | Path,
        default_source: str = "file_import",
        max_file_bytes: int = MAX_FILE_BYTES,
        max_records: int = MAX_RECORDS,
    ) -> None:
        self.allowed_root = Path(allowed_root).expanduser().resolve()
        self.path = Path(path).expanduser().resolve()
        if not self.path.is_relative_to(self.allowed_root):
            raise SourceAdapterError("source file is outside allowed_root")
        self.default_source = default_source
        self.max_file_bytes = max_file_bytes
        self.max_records = max_records

    def fetch(
        self,
        *,
        cursor: SourceCursor | None = None,
        limit: int = 500,
    ) -> SourcePage:
        if limit < 1 or limit > self.max_records:
            raise SourceAdapterError(f"limit must be between 1 and {self.max_records}")
        text = self._read_text()
        snapshot_hash = sha256_text(text)
        offset = 0 if cursor is None else cursor.next_offset
        if cursor is not None and cursor.file_sha256 != snapshot_hash:
            raise SourceAdapterError("source snapshot changed; cursor cannot be replayed")

        rows = list(self._iter_rows(text))
        if len(rows) > self.max_records:
            raise SourceAdapterError(f"source contains more than {self.max_records} records")
        if offset > len(rows):
            raise SourceAdapterError("cursor offset is outside the source snapshot")

        selected = rows[offset : offset + limit]
        records: list[CanonicalSourceRecord] = []
        for index, row in enumerate(selected, start=offset + 1):
            try:
                records.append(
                    canonical_record_from_mapping(row, default_source=self.default_source)
                )
            except (SourceAdapterError, ValueError) as exc:
                raise SourceAdapterError(f"record {index}: {exc}") from exc

        next_offset = offset + len(selected)
        next_cursor = (
            SourceCursor(file_sha256=snapshot_hash, next_offset=next_offset)
            if next_offset < len(rows)
            else None
        )
        return SourcePage(
            records=tuple(records),
            next_cursor=next_cursor,
            source_snapshot_sha256=snapshot_hash,
        )

    def _read_text(self) -> str:
        if not self.path.exists() or not self.path.is_file():
            raise SourceAdapterError("source file does not exist or is not a regular file")
        if self.path.suffix.lower() not in SUPPORTED_SUFFIXES:
            raise SourceAdapterError(
                f"unsupported source format: {self.path.suffix.lower() or '<none>'}"
            )
        size = self.path.stat().st_size
        if size > self.max_file_bytes:
            raise SourceAdapterError(f"source file exceeds {self.max_file_bytes} bytes")
        try:
            text = self.path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            raise SourceAdapterError("source file must be UTF-8 encoded") from exc
        if len(text.encode("utf-8")) > self.max_file_bytes:
            raise SourceAdapterError(f"source file exceeds {self.max_file_bytes} bytes")
        return text

    def _iter_rows(self, text: str) -> Iterable[Mapping[str, Any]]:
        suffix = self.path.suffix.lower()
        if suffix == ".csv":
            reader = csv.DictReader(text.splitlines())
            if not reader.fieldnames:
                raise SourceAdapterError("CSV header is required")
            if len(reader.fieldnames) > MAX_COLUMNS:
                raise SourceAdapterError(f"CSV has more than {MAX_COLUMNS} columns")
            yield from reader
            return

        if suffix == ".jsonl":
            for line_number, line in enumerate(text.splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise SourceAdapterError(f"invalid JSON on line {line_number}") from exc
                if not isinstance(value, dict):
                    raise SourceAdapterError(f"JSONL line {line_number} must be an object")
                yield value
            return

        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SourceAdapterError("invalid JSON source file") from exc
        if not isinstance(payload, dict):
            raise SourceAdapterError("JSON source must be an object with schema_version and records")
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise SourceAdapterError(
                f"unsupported schema_version: {payload.get('schema_version')!r}"
            )
        records = payload.get("records")
        if not isinstance(records, list):
            raise SourceAdapterError("JSON source records must be an array")
        for index, value in enumerate(records, start=1):
            if not isinstance(value, dict):
                raise SourceAdapterError(f"JSON record {index} must be an object")
            yield value
