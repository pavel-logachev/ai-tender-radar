from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import zipfile

from app.collector.download_validation import (
    detect_downloaded_file_type,
    validate_downloaded_document,
)
from app.document_archive_utils import is_safe_child_path


APP_DIR = Path("/app")
SIGNATURE_READ_BYTES = 4096

EXPECTED_TYPE_BY_SUFFIX = {
    ".pdf": "pdf",
    ".docx": "zip",
    ".xlsx": "zip",
    ".zip": "zip",
    ".doc": "ole",
    ".xls": "ole",
    ".rar": "rar",
    ".7z": "7z",
}

PROBLEM_BY_SUFFIX = {
    ".pdf": "invalid_pdf_like",
    ".docx": "invalid_docx_like",
    ".xlsx": "invalid_xlsx_like",
    ".zip": "invalid_zip_like",
    ".doc": "invalid_doc_like",
    ".xls": "invalid_xls_like",
    ".rar": "invalid_rar_like",
    ".7z": "invalid_7z_like",
}

PROBLEM_CODES = {
    "html_response",
    "invalid_docx_like",
    "invalid_xlsx_like",
    "invalid_zip_like",
    "invalid_pdf_like",
    "invalid_doc_like",
    "invalid_xls_like",
    "invalid_rar_like",
    "invalid_7z_like",
    "missing_file_on_disk",
    "zero_bytes",
    "not_regular_file",
    "unsafe_storage_path",
    "unsupported_or_unknown_signature",
}


@dataclass(frozen=True)
class RevalidationIssue:
    problem: str
    detected_type: str | None
    reason: str


@dataclass(frozen=True)
class RevalidationResult:
    row: dict
    file_path: Path | None
    issue: RevalidationIssue | None
    action: str
    details: str = ""


def classify_document_bytes(
    filename: str | None,
    content: bytes,
    content_type: str | None = None,
) -> RevalidationIssue | None:
    detected_type = detect_downloaded_file_type(content)

    if detected_type == "html":
        return RevalidationIssue(
            problem="html_response",
            detected_type=detected_type,
            reason="html_response",
        )

    suffix = Path(filename or "").suffix.lower()
    expected_type = EXPECTED_TYPE_BY_SUFFIX.get(suffix)
    if expected_type and detected_type != expected_type:
        return RevalidationIssue(
            problem=PROBLEM_BY_SUFFIX[suffix],
            detected_type=detected_type,
            reason=f"expected_{expected_type}_signature",
        )

    is_valid, detected_type, reason = validate_downloaded_document(
        content,
        filename or "",
        content_type,
    )
    if is_valid:
        return None

    return RevalidationIssue(
        problem="unsupported_or_unknown_signature",
        detected_type=detected_type,
        reason=reason,
    )


def resolve_document_path(app_dir: Path, storage_path: str | None) -> tuple[Path | None, str | None]:
    if not storage_path:
        return None, "missing_file_on_disk"

    relative_path = Path(storage_path)
    if relative_path.is_absolute():
        return relative_path, "unsafe_storage_path"

    file_path = app_dir / relative_path
    if not is_safe_child_path(app_dir, file_path):
        return file_path, "unsafe_storage_path"

    return file_path, None


def classify_document_file(row: dict, app_dir: Path = APP_DIR) -> tuple[Path | None, RevalidationIssue | None]:
    file_path, path_problem = resolve_document_path(app_dir, row.get("storage_path"))
    if path_problem:
        return file_path, RevalidationIssue(path_problem, None, path_problem)

    if file_path is None or not file_path.exists():
        return file_path, RevalidationIssue(
            "missing_file_on_disk",
            None,
            "missing_file_on_disk",
        )

    if not file_path.is_file():
        return file_path, RevalidationIssue("not_regular_file", None, "not_regular_file")

    size_bytes = file_path.stat().st_size
    if size_bytes <= 0:
        return file_path, RevalidationIssue("zero_bytes", None, "zero_bytes")

    with file_path.open("rb") as fh:
        content_head = fh.read(SIGNATURE_READ_BYTES)

    issue = classify_document_bytes(
        row.get("filename") or file_path.name,
        content_head,
        row.get("mime_type"),
    )
    if issue:
        return file_path, issue

    suffix = Path(row.get("filename") or file_path.name).suffix.lower()
    if suffix in {".docx", ".xlsx", ".zip"} and not zipfile.is_zipfile(file_path):
        return file_path, RevalidationIssue(
            PROBLEM_BY_SUFFIX[suffix],
            "zip",
            "zip_container_invalid",
        )

    return file_path, None


def connect_db():
    import psycopg
    from psycopg.rows import dict_row

    from app.config import settings

    return psycopg.connect(settings.database_url, row_factory=dict_row)


def fetch_document_rows(
    *,
    external_id: str | None = None,
    tender_id: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    filters = []
    params: list[object] = []

    if external_id:
        filters.append("t.external_id = %s")
        params.append(external_id)

    if tender_id:
        filters.append("d.tender_id = %s")
        params.append(tender_id)

    where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
    limit_sql = "LIMIT %s" if limit else ""
    if limit:
        params.append(limit)

    query = f"""
        SELECT
            d.id AS document_id,
            d.tender_id,
            d.filename,
            d.url,
            d.mime_type,
            d.size_bytes,
            d.storage_path,
            t.external_id
        FROM documents d
        LEFT JOIN tenders t ON t.id = d.tender_id
        {where_sql}
        ORDER BY d.created_at DESC
        {limit_sql};
    """

    with connect_db() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchall()


def delete_document_record(document_id: str) -> None:
    with connect_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM documents WHERE id = %s;", (document_id,))
        conn.commit()


def remove_invalid_document(row: dict, file_path: Path | None) -> str:
    if file_path and file_path.exists() and file_path.is_file():
        file_path.unlink()

    delete_document_record(str(row["document_id"]))
    return "removed_file_and_db_record"


def cleanup_invalid_documents(
    *,
    apply: bool = False,
    external_id: str | None = None,
    tender_id: str | None = None,
    limit: int | None = None,
    problems: set[str] | None = None,
    app_dir: Path = APP_DIR,
    show_valid: bool = False,
) -> list[RevalidationResult]:
    rows = fetch_document_rows(
        external_id=external_id,
        tender_id=tender_id,
        limit=limit,
    )
    results = []

    for row in rows:
        file_path, issue = classify_document_file(row, app_dir=app_dir)

        if issue is None:
            if show_valid:
                results.append(RevalidationResult(row, file_path, None, "keep"))
            continue

        if problems and issue.problem not in problems:
            results.append(
                RevalidationResult(
                    row,
                    file_path,
                    issue,
                    "skip",
                    "problem_filter",
                )
            )
            continue

        if not apply:
            results.append(RevalidationResult(row, file_path, issue, "skip", "dry_run"))
            continue

        try:
            details = remove_invalid_document(row, file_path)
            results.append(RevalidationResult(row, file_path, issue, "remove", details))
        except Exception as exc:
            results.append(RevalidationResult(row, file_path, issue, "skip", str(exc)))

    return results


def format_value(value: object) -> str:
    if value is None:
        return "-"

    text = str(value).replace("\r", " ").replace("\n", " ")
    if not text:
        return "-"

    if any(ch.isspace() for ch in text):
        return repr(text)

    return text


def format_revalidation_result(result: RevalidationResult) -> str:
    row = result.row
    issue = result.issue
    parts = {
        "action": result.action,
        "details": result.details,
        "external_id": row.get("external_id"),
        "tender_id": row.get("tender_id"),
        "document_id": row.get("document_id"),
        "filename": row.get("filename"),
        "path": result.file_path,
        "detected_type": issue.detected_type if issue else "valid",
        "problem": issue.problem if issue else "-",
        "reason": issue.reason if issue else "-",
    }
    return " ".join(f"{key}={format_value(value)}" for key, value in parts.items())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Revalidate and optionally clean downloaded document files"
    )
    parser.add_argument("--apply", action="store_true", help="Delete invalid files and DB rows")
    parser.add_argument("--external-id", help="Only check one tender external_id")
    parser.add_argument("--tender-id", help="Only check one tender UUID")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--problem",
        action="append",
        choices=sorted(PROBLEM_CODES),
        help="Only act on this problem type; can be repeated",
    )
    parser.add_argument("--app-dir", default=str(APP_DIR))
    parser.add_argument("--show-valid", action="store_true")
    args = parser.parse_args()

    results = cleanup_invalid_documents(
        apply=args.apply,
        external_id=args.external_id,
        tender_id=args.tender_id,
        limit=args.limit,
        problems=set(args.problem or []),
        app_dir=Path(args.app_dir),
        show_valid=args.show_valid,
    )

    removed = 0
    skipped = 0
    kept = 0
    invalid = 0

    for result in results:
        if result.issue:
            invalid += 1
        if result.action == "remove":
            removed += 1
        elif result.action == "keep":
            kept += 1
        else:
            skipped += 1

        print(format_revalidation_result(result))

    mode = "apply" if args.apply else "dry-run"
    print(
        "summary "
        f"mode={mode} "
        f"reported={len(results)} "
        f"invalid={invalid} "
        f"removed={removed} "
        f"skipped={skipped} "
        f"kept={kept}"
    )


if __name__ == "__main__":
    main()
