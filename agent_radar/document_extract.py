"""Offline, bounded document-bytes-to-text seam for the source worker.

No archive recursion, filesystem paths, provider calls, OCR or external resources.
A failed/empty extraction is never evidence that the source reports no document.
The subprocess deadline bounds native PDF/XML parser hangs, but is not an OS
security sandbox: deploy untrusted documents in a network-disabled container.
"""
from __future__ import annotations

import io
import math
import multiprocessing
import re
import zipfile
from dataclasses import dataclass
from pathlib import PurePath
from xml.etree import ElementTree

MAX_DOCUMENT_BYTES = 12_000_000
MAX_DOCX_MEMBER_BYTES = 2_000_000
MAX_DOCX_TOTAL_BYTES = 16_000_000
MAX_DOCX_MEMBERS = 256
MAX_PAGES = 100
MAX_TEXT_CHARS = 200_000
DEFAULT_TIMEOUT_SECONDS = 8.0


@dataclass(frozen=True)
class ExtractionResult:
    """Only `extracted` has publishable text; all other statuses represent gaps."""

    status: str  # extracted | empty | unsupported | invalid | too_large | timeout | failed
    text: str = ""
    reason: str | None = None


class _InvalidDocument(Exception):
    pass


class _TooLarge(Exception):
    pass


def _normalize(text: str) -> str:
    return "\n".join(line for raw in text.splitlines() if (line := " ".join(raw.split())))


def _docx_text(data: bytes) -> str:
    if not data.startswith(b"PK\x03\x04"):
        raise _InvalidDocument("format_mismatch")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_DOCX_MEMBERS:
                raise _TooLarge("docx_member_limit")
            names: set[str] = set()
            total = 0
            parts: list[zipfile.ZipInfo] = []
            for info in infos:
                name = info.filename
                # Reject paths even though this implementation never writes ZIP members.
                if (not name or "\\" in name or name.startswith("/") or
                    any(part in ("", ".", "..") for part in name.rstrip("/").split("/")) or
                    ":" in name or "\x00" in name or name in names or
                    ((info.external_attr >> 16) & 0o170000) == 0o120000 or
                    info.flag_bits & 1):
                    raise _InvalidDocument("unsafe_docx_member")
                names.add(name)
                if name.lower().endswith((".zip", ".7z", ".rar")) or name.startswith("word/embeddings/"):
                    raise _InvalidDocument("embedded_archive_not_allowed")
                if info.file_size > MAX_DOCX_MEMBER_BYTES:
                    raise _TooLarge("docx_member_too_large")
                total += info.file_size
                if total > MAX_DOCX_TOTAL_BYTES:
                    raise _TooLarge("docx_total_too_large")
                if info.file_size > max(info.compress_size, 1) * 200:
                    raise _TooLarge("docx_compression_ratio")
                if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise _InvalidDocument("unsupported_docx_compression")
                if name == "word/document.xml" or re.fullmatch(r"word/(?:header\d+|footer\d+|footnotes|endnotes|comments)\.xml", name):
                    parts.append(info)
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                raise _InvalidDocument("not_docx")
            lines: list[str] = []
            length = 0
            for info in sorted(parts, key=lambda item: (item.filename != "word/document.xml", item.filename)):
                # ZipInfo metadata checks precede read; bound the actual stream too,
                # rather than trusting forged metadata or zipfile.read allocation.
                with archive.open(info) as stream:
                    xml = stream.read(MAX_DOCX_MEMBER_BYTES + 1)
                    if len(xml) > MAX_DOCX_MEMBER_BYTES or stream.read(1):
                        raise _TooLarge("docx_member_too_large")
                if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", xml, re.IGNORECASE):
                    raise _InvalidDocument("xml_dtd_not_allowed")
                root = ElementTree.fromstring(xml)
                for paragraph in root.iter():
                    if paragraph.tag.rsplit("}", 1)[-1] != "p":
                        continue
                    fragments = []
                    for node in paragraph.iter():
                        tag = node.tag.rsplit("}", 1)[-1]
                        if tag == "t" and node.text:
                            fragments.append(node.text)
                        elif tag == "tab":
                            fragments.append(" ")
                        elif tag in ("br", "cr"):
                            fragments.append("\n")
                    line = _normalize("".join(fragments))
                    if line:
                        length += len(line)
                        if length > MAX_TEXT_CHARS:
                            raise _TooLarge("text_too_large")
                        lines.append(line)
            return _normalize("\n".join(lines))
    except (zipfile.BadZipFile, RuntimeError, EOFError, ElementTree.ParseError, OSError, ValueError) as exc:
        raise _InvalidDocument("invalid_docx") from exc


def _pdf_text(data: bytes) -> str:
    if not data.startswith(b"%PDF-"):
        raise _InvalidDocument("format_mismatch")
    try:
        import fitz
        with fitz.open(stream=data, filetype="pdf") as document:
            if document.needs_pass:
                raise _InvalidDocument("encrypted_pdf")
            if document.page_count > MAX_PAGES:
                raise _TooLarge("pdf_page_limit")
            parts: list[str] = []
            length = 0
            for page in document:
                text = page.get_text("text")
                length += len(text)
                if length > MAX_TEXT_CHARS:
                    raise _TooLarge("text_too_large")
                parts.append(text)
            return _normalize("\n".join(parts))
    except (RuntimeError, ValueError, OSError) as exc:
        raise _InvalidDocument("invalid_pdf") from exc


def _parse(data: bytes, suffix: str) -> ExtractionResult:
    try:
        if suffix == ".pdf":
            text = _pdf_text(data)
        elif suffix == ".docx":
            text = _docx_text(data)
        else:
            if data.startswith((b"%PDF-", b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08",
                                b"Rar!\x1a\x07", b"7z\xbc\xaf\x27\x1c", b"\xd0\xcf\x11\xe0")) or b"\x00" in data:
                raise _InvalidDocument("format_mismatch")
            text = data.decode("utf-8-sig")
            if any(ord(char) < 32 and char not in "\n\r\t\f" for char in text):
                raise _InvalidDocument("binary_text")
            text = _normalize(text)
        if len(text) > MAX_TEXT_CHARS:
            raise _TooLarge("text_too_large")
        return ExtractionResult("extracted", text) if text else ExtractionResult("empty", reason="no_extractable_text")
    except UnicodeError:
        return ExtractionResult("invalid", reason="invalid_utf8")
    except _InvalidDocument as exc:
        return ExtractionResult("invalid", reason=str(exc))
    except _TooLarge as exc:
        return ExtractionResult("too_large", reason=str(exc))
    except Exception:
        # Do not expose parser exceptions (which can contain document data).
        return ExtractionResult("failed", reason="parser_failed")


def _worker(conn, data: bytes, suffix: str) -> None:
    try:
        # Linux production defense in depth in addition to the parent deadline.
        # Windows lacks resource limits; use a container-level memory cap there.
        try:
            import resource
        except ImportError:
            pass
        else:
            resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
        conn.send(_parse(data, suffix))
    finally:
        conn.close()


def extract_document_text(data: bytes, filename: str, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> ExtractionResult:
    """Parse bounded bytes in a killable child; return a gap reason on every failure.

    The caller alone establishes that the source listed a document. Never map any
    non-`extracted` result to `source_reports_none` or publish truncated text.
    """
    if not isinstance(filename, str) or not filename or filename in (".", "..") or filename != PurePath(filename).name or "\\" in filename or "\x00" in filename:
        return ExtractionResult("invalid", reason="invalid_filename")
    suffix = PurePath(filename).suffix.lower()
    if suffix not in {".pdf", ".docx", ".txt"}:
        return ExtractionResult("unsupported", reason="unsupported_extension")
    if not isinstance(data, bytes):
        return ExtractionResult("invalid", reason="expected_bytes")
    if len(data) > MAX_DOCUMENT_BYTES:
        return ExtractionResult("too_large", reason="document_too_large")
    if not data:
        return ExtractionResult("empty", reason="no_extractable_text") if suffix == ".txt" else ExtractionResult("invalid", reason="empty_document")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30:
        raise ValueError("invalid extraction deadline")
    # spawn is safe across platforms and avoids inheriting network clients or
    # open source-worker descriptors from a forked parent. No document temp files.
    ctx = multiprocessing.get_context("spawn")
    receive, send = ctx.Pipe(duplex=False)
    process = ctx.Process(target=_worker, args=(send, data, suffix), daemon=True)
    try:
        process.start()
        send.close()
        if not receive.poll(timeout_seconds):
            return ExtractionResult("timeout", reason="extraction_deadline")
        try:
            result = receive.recv()
        except (EOFError, OSError):
            return ExtractionResult("failed", reason="worker_exited")
        return result if isinstance(result, ExtractionResult) else ExtractionResult("failed", reason="invalid_worker_result")
    except (OSError, RuntimeError):
        return ExtractionResult("failed", reason="worker_start_failed")
    finally:
        if process.is_alive():
            process.terminate()
        if process.pid is not None:
            process.join(timeout=1)
            if process.is_alive():
                process.kill()
                process.join(timeout=1)
        receive.close()
        send.close()
