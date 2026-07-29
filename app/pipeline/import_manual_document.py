from __future__ import annotations

import argparse
import json
import mimetypes
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote


APP_DIR = Path("/app")
DATA_DIR = APP_DIR / "data" / "documents"
MANUAL_IMPORT_EVENT = "manual_document_imported"
MANUAL_DOCUMENT_URL_PREFIX = "manual://document"


@dataclass(frozen=True)
class ManualDocumentImportResult:
    tender_id: str
    external_id: str
    document_id: str
    filename: str
    storage_path: str
    extracted_chars: int
    extraction_error: str | None = None


def connect_db(*, row_factory=None):
    import psycopg

    from app.config import settings

    kwargs = {"row_factory": row_factory} if row_factory is not None else {}
    return psycopg.connect(settings.database_url, **kwargs)


def dict_row_factory():
    from psycopg.rows import dict_row

    return dict_row


def safe_manual_filename(value: str | None, fallback: str) -> str:
    value = value or fallback
    value = unquote(value)
    value = value.replace("\n", " ").replace("\r", " ").strip()
    value = re.sub(r'[\\/:*?"<>|]+', "_", value)
    value = re.sub(r"\s+", " ", value)
    return value[:180] or fallback


def find_tender_by_external_id(external_id: str) -> dict:
    row_factory = dict_row_factory()
    with connect_db(row_factory=row_factory) as conn:
        with conn.cursor(row_factory=row_factory) as cur:
            cur.execute(
                """
                SELECT id, external_id, title
                FROM tenders
                WHERE external_id = %s
                LIMIT 1;
                """,
                (external_id,),
            )
            tender = cur.fetchone()

    if not tender:
        raise RuntimeError(f"Tender not found: external_id={external_id}")

    return dict(tender)


def manual_document_url(external_id: str, filename: str) -> str:
    return f"{MANUAL_DOCUMENT_URL_PREFIX}/{quote(external_id)}/{quote(filename)}"


def manual_document_filename(external_id: str, source_path: Path, title: str | None) -> str:
    source_suffix = source_path.suffix
    base = title or source_path.name
    filename = safe_manual_filename(
        f"manual_{external_id}_{base}",
        f"manual_{external_id}{source_suffix or '_document'}",
    )

    if not Path(filename).suffix and source_suffix:
        filename = safe_manual_filename(
            f"{filename}{source_suffix}",
            f"manual_{external_id}{source_suffix}",
        )

    return filename


def copy_manual_document_file(
    *,
    external_id: str,
    file_path: Path,
    title: str | None,
) -> tuple[Path, str, str | None, int]:
    source_path = file_path.expanduser().resolve(strict=True)
    if not source_path.is_file():
        raise RuntimeError(f"Manual document is not a file: {source_path}")

    filename = manual_document_filename(external_id, source_path, title)
    target_dir = DATA_DIR / f"zakupki360_{external_id}"
    target_dir.mkdir(parents=True, exist_ok=True)

    target_path = target_dir / filename
    shutil.copy2(source_path, target_path)

    mime_type = mimetypes.guess_type(filename)[0]
    size_bytes = target_path.stat().st_size
    return target_path, filename, mime_type, size_bytes


def upsert_manual_document_record(
    *,
    tender_id: str,
    document_url: str,
    filename: str,
    mime_type: str | None,
    size_bytes: int,
    storage_path: str,
) -> str:
    row_factory = dict_row_factory()
    with connect_db(row_factory=row_factory) as conn:
        with conn.cursor(row_factory=row_factory) as cur:
            cur.execute(
                """
                SELECT id
                FROM documents
                WHERE tender_id = %s
                  AND url = %s
                LIMIT 1;
                """,
                (tender_id, document_url),
            )
            existing = cur.fetchone()

            if existing:
                cur.execute(
                    """
                    UPDATE documents
                    SET
                        filename = %s,
                        mime_type = %s,
                        size_bytes = %s,
                        storage_path = %s,
                        extracted_text = NULL
                    WHERE id = %s
                    RETURNING id;
                    """,
                    (
                        filename,
                        mime_type,
                        size_bytes,
                        storage_path,
                        existing["id"],
                    ),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO documents (
                        tender_id,
                        filename,
                        url,
                        mime_type,
                        size_bytes,
                        storage_path
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id;
                    """,
                    (
                        tender_id,
                        filename,
                        document_url,
                        mime_type,
                        size_bytes,
                        storage_path,
                    ),
                )

            document = cur.fetchone()
        conn.commit()

    if not document:
        raise RuntimeError("Manual document row was not created")

    return str(document["id"])


def save_manual_extracted_text(document_id: str, text: str) -> None:
    with connect_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE documents
                SET extracted_text = %s
                WHERE id = %s;
                """,
                (text, document_id),
            )
        conn.commit()


def extract_manual_document_text(path: Path) -> tuple[str, str | None]:
    try:
        from app.document_text_extractor import clean_text, extract_file

        return clean_text(extract_file(path)), None
    except Exception as exc:
        return "", str(exc)


def record_manual_document_imported_event(
    *,
    tender_id: str,
    external_id: str,
    document_id: str,
    title: str | None,
    filename: str,
    storage_path: str,
    extracted_chars: int,
    extraction_error: str | None,
) -> None:
    payload = {
        "external_id": external_id,
        "document_id": document_id,
        "title": title,
        "filename": filename,
        "storage_path": storage_path,
        "extracted_chars": extracted_chars,
        "extraction_error": extraction_error,
    }

    with connect_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO processing_events (tender_id, event_type, message)
                VALUES (%s, %s, %s);
                """,
                (
                    tender_id,
                    MANUAL_IMPORT_EVENT,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True)[:2000],
                ),
            )
        conn.commit()


def import_manual_document(
    *,
    external_id: str,
    file_path: Path,
    title: str | None = None,
) -> ManualDocumentImportResult:
    tender = find_tender_by_external_id(external_id)
    tender_id = str(tender["id"])

    target_path, filename, mime_type, size_bytes = copy_manual_document_file(
        external_id=external_id,
        file_path=file_path,
        title=title,
    )
    storage_path = str(target_path.relative_to(APP_DIR))
    document_url = manual_document_url(external_id, filename)

    document_id = upsert_manual_document_record(
        tender_id=tender_id,
        document_url=document_url,
        filename=filename,
        mime_type=mime_type,
        size_bytes=size_bytes,
        storage_path=storage_path,
    )

    extracted_text, extraction_error = extract_manual_document_text(target_path)
    save_manual_extracted_text(document_id, extracted_text)
    record_manual_document_imported_event(
        tender_id=tender_id,
        external_id=external_id,
        document_id=document_id,
        title=title,
        filename=filename,
        storage_path=storage_path,
        extracted_chars=len(extracted_text),
        extraction_error=extraction_error,
    )

    return ManualDocumentImportResult(
        tender_id=tender_id,
        external_id=external_id,
        document_id=document_id,
        filename=filename,
        storage_path=storage_path,
        extracted_chars=len(extracted_text),
        extraction_error=extraction_error,
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.pipeline.import_manual_document")
    parser.add_argument("--external-id", required=True)
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--title")
    args = parser.parse_args()

    result = import_manual_document(
        external_id=args.external_id,
        file_path=args.file,
        title=args.title,
    )

    print(f"Manual document imported: external_id={result.external_id}")
    print(f"document_id={result.document_id}")
    print(f"filename={result.filename}")
    print(f"storage_path={result.storage_path}")
    print(f"extracted_chars={result.extracted_chars}")
    if result.extraction_error:
        print(f"extraction_error={result.extraction_error}")


if __name__ == "__main__":
    main()
