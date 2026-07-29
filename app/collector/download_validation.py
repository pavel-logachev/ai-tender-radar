from __future__ import annotations

from pathlib import Path


PDF_SIGNATURE = b"%PDF-"
ZIP_SIGNATURE = b"PK"
OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
RAR4_SIGNATURE = b"Rar!\x1a\x07\x00"
RAR5_SIGNATURE = b"Rar!\x1a\x07\x01\x00"
SEVEN_Z_SIGNATURE = b"7z\xbc\xaf\x27\x1c"

ZIP_BASED_EXTENSIONS = {".docx", ".xlsx", ".zip"}
OLE_BASED_EXTENSIONS = {".doc", ".xls"}
EXPECTED_SIGNATURES_BY_EXTENSION = {
    ".pdf": {"pdf"},
    **{ext: {"zip"} for ext in ZIP_BASED_EXTENSIONS},
    **{ext: {"ole"} for ext in OLE_BASED_EXTENSIONS},
    ".rar": {"rar"},
    ".7z": {"7z"},
}
VALID_DOWNLOAD_SIGNATURES = {"pdf", "zip", "ole", "rar", "7z"}
EXTENSION_BY_DETECTED_FILE_TYPE = {
    "pdf": ".pdf",
    "zip": ".zip",
    "ole": ".doc",
    "rar": ".rar",
    "7z": ".7z",
}


def looks_like_html_response(content: bytes) -> bool:
    sample = content[:2048].decode("utf-8", errors="ignore")
    sample = sample.replace("\x00", "").lstrip("\ufeff \t\r\n").lower()

    html_markers = (
        "<!doctype html",
        "<html",
        "<head",
        "<body",
        "<title",
        "<meta",
        "<script",
    )

    return any(sample.startswith(marker) for marker in html_markers)


def detect_downloaded_file_type(content: bytes) -> str | None:
    if looks_like_html_response(content):
        return "html"

    if content.startswith(PDF_SIGNATURE):
        return "pdf"

    if content.startswith(ZIP_SIGNATURE):
        return "zip"

    if content.startswith(OLE_SIGNATURE):
        return "ole"

    if content.startswith(RAR5_SIGNATURE) or content.startswith(RAR4_SIGNATURE):
        return "rar"

    if content.startswith(SEVEN_Z_SIGNATURE):
        return "7z"

    return None


def extension_for_detected_file_type(detected_type: str | None) -> str:
    return EXTENSION_BY_DETECTED_FILE_TYPE.get(detected_type or "", "")


def validate_downloaded_document(
    content: bytes,
    filename: str,
    content_type: str | None,
) -> tuple[bool, str | None, str]:
    detected_type = detect_downloaded_file_type(content)

    if detected_type == "html":
        return False, detected_type, "html_response"

    suffix = Path(filename).suffix.lower()
    expected_types = EXPECTED_SIGNATURES_BY_EXTENSION.get(suffix)

    if expected_types:
        if detected_type in expected_types:
            return True, detected_type, "ok"

        expected = "/".join(sorted(expected_types))
        actual = detected_type or "unknown"
        return False, detected_type, f"signature_mismatch_expected_{expected}_got_{actual}"

    if detected_type in VALID_DOWNLOAD_SIGNATURES:
        return True, detected_type, "ok"

    content_type_hint = (content_type or "").split(";")[0].strip().lower() or "none"
    return (
        False,
        detected_type,
        f"unsupported_or_unknown_signature_content_type_{content_type_hint}",
    )
