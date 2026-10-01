"""Offline bytes-to-evidence packages; deploy only in a network-disabled worker.

Archives are never extracted to filesystem paths. ZIP streams and 7zip stdout
are bounded independently of declared sizes. The worker must also enforce
cgroup memory/CPU/pids, a wall deadline and a small private scratch filesystem.
No macros, spreadsheet formulas or external references are executed/fetched.
"""
from __future__ import annotations

import io
import re
import stat
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from xml.etree import ElementTree
from agent_radar.document_extract import ExtractionResult
import unicodedata
import zipfile
from pathlib import PurePosixPath

from agent_radar.document_extract import (
    MAX_DOCUMENT_BYTES, MAX_TEXT_CHARS, MAX_DOCX_MEMBER_BYTES,
    MAX_DOCX_TOTAL_BYTES, extract_document_text,
)

MAX_EXPANDED_BYTES = 24_000_000
MAX_ARCHIVE_MEMBERS = 80
MAX_DOCUMENTS = 20
MAX_TOTAL_TEXT_CHARS = 600_000
MAX_RATIO = 200
ARCHIVE_SUFFIXES = {'.zip', '.7z', '.rar'}


class _Gap(Exception):
    pass


def _safe_name(name: str) -> str:
    if (not isinstance(name, str) or not name or len(name) > 900 or
            '\\' in name or ':' in name or name.startswith(('/', '-', '@')) or
            any(ord(c) < 32 or ord(c) == 127 or c in '*?' for c in name)):
        raise _Gap('unsafe_member_path')
    parts = name.rstrip('/').split('/')
    if any(p in ('', '.', '..') for p in parts):
        raise _Gap('unsafe_member_path')
    return name


def _zip_inventory(archive: zipfile.ZipFile, *, container: bool = False):
    infos = archive.infolist()
    if len(infos) > (256 if container else MAX_ARCHIVE_MEMBERS):
        raise _Gap('archive_member_limit')
    seen = set()
    total = 0
    for info in infos:
        name = _safe_name(info.filename)
        key = unicodedata.normalize('NFC', name.rstrip('/')).casefold()
        if key in seen:
            raise _Gap('duplicate_member')
        seen.add(key)
        mode = info.external_attr >> 16
        kind = stat.S_IFMT(mode)
        if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise _Gap('archive_link_or_special_member')
        if info.flag_bits & 1:
            raise _Gap('encrypted_archive')
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise _Gap('unsupported_archive_compression')
        if info.file_size < 0 or info.file_size > MAX_DOCUMENT_BYTES:
            raise _Gap('archive_member_too_large')
        total += info.file_size
        if total > MAX_EXPANDED_BYTES:
            raise _Gap('archive_expansion_limit')
        if info.file_size > max(info.compress_size, 1) * MAX_RATIO:
            raise _Gap('archive_compression_ratio')
    return infos


def _read_zip(archive, info, remaining):
    cap = min(MAX_DOCUMENT_BYTES, remaining, info.file_size)
    with archive.open(info) as stream:
        data = stream.read(cap + 1)
        if len(data) > cap or stream.read(1):
            raise _Gap('archive_expansion_limit')
    if len(data) != info.file_size:
        raise _Gap('archive_size_mismatch')
    return data


def _run_bounded(args, cap, deadline):
    """Drain both child pipes with hard byte/deadline limits; no shell/temp capture."""
    if time.monotonic() >= deadline:
        raise _Gap('package_deadline')
    process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, shell=False)
    output = bytearray()
    errors = bytearray()
    exceeded = threading.Event()
    def drain(stream, target, limit):
        try:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                if len(target) + len(chunk) > limit:
                    exceeded.set()
                    process.kill()
                    break
                target.extend(chunk)
        except (OSError, ValueError):
            exceeded.set()
    threads = [threading.Thread(target=drain, args=(process.stdout, output, cap), daemon=True),
               threading.Thread(target=drain, args=(process.stderr, errors, 65536), daemon=True)]
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        process.wait(timeout=max(0.001, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        process.wait(timeout=2)
    finally:
        for thread in threads:
            thread.join(timeout=2)
        process.stdout.close()
        process.stderr.close()
    if timed_out:
        raise _Gap('package_deadline')
    if exceeded.is_set() or any(t.is_alive() for t in threads):
        raise _Gap('subprocess_output_limit')
    if process.returncode:
        raise _Gap('document_backend_failed')
    return bytes(output)


def _seven_inventory(listing, packed_size):
    try:
        text = listing.decode('utf-8')
    except UnicodeError:
        raise _Gap('invalid_7zip_inventory') from None
    if '\n----------\n' not in text.replace('\r\n', '\n'):
        raise _Gap('invalid_7zip_inventory')
    text = text.replace('\r\n', '\n').split('\n----------\n', 1)[1]
    entries = []
    seen = set()
    total = 0
    for block in text.strip().split('\n\n'):
        if not block.strip():
            continue
        fields = {}
        for line in block.splitlines():
            if ' = ' not in line:
                raise _Gap('invalid_7zip_inventory')
            key, value = line.split(' = ', 1)
            if key in fields:
                raise _Gap('invalid_7zip_inventory')
            fields[key] = value
        name = _safe_name(fields.get('Path', ''))
        # Brackets/@ at any position are selectors in some 7zip versions.
        if any(c in name for c in '[]@'):
            raise _Gap('unsafe_member_selector')
        canonical = unicodedata.normalize('NFC', name.rstrip('/')).casefold()
        if canonical in seen:
            raise _Gap('duplicate_member')
        seen.add(canonical)
        if fields.get('Encrypted') == '+':
            raise _Gap('encrypted_archive')
        if any(k in fields for k in ('Symbolic Link', 'Hard Link', 'Link')) or 'l' in fields.get('Attributes', '').lower():
            raise _Gap('archive_link_or_special_member')
        raw = fields.get('Size', '')
        if not raw.isdecimal():
            raise _Gap('invalid_7zip_inventory')
        size = int(raw)
        if size > MAX_DOCUMENT_BYTES:
            raise _Gap('archive_member_too_large')
        total += size
        if total > MAX_EXPANDED_BYTES:
            raise _Gap('archive_expansion_limit')
        is_dir = fields.get('Folder') == '+' or 'D' in fields.get('Attributes', '').split(' ', 1)[0]
        entries.append((name, size, is_dir))
        if len(entries) > MAX_ARCHIVE_MEMBERS:
            raise _Gap('archive_member_limit')
    if total > max(packed_size, 1) * MAX_RATIO:
        raise _Gap('archive_compression_ratio')
    return entries


def _seven_package(data, filename, collector):
    tool = shutil.which('7zz') or shutil.which('7z')
    if not tool:
        raise _Gap('7zip_not_installed')
    if not data.startswith(b'7z\xbc\xaf\x27\x1c'):
        raise _Gap('format_mismatch')
    deadline = min(collector.deadline, time.monotonic() + 15)
    with tempfile.TemporaryDirectory(prefix='radar-package-') as scratch:
        path = Path(scratch) / 'input.7z'
        path.write_bytes(data)
        listing = _run_bounded([tool, 'l', '-slt', '-sccUTF-8', '--', str(path)], 256000, deadline)
        entries = _seven_inventory(listing, len(data))
        remaining = MAX_EXPANDED_BYTES
        for name, size, is_dir in entries:
            if is_dir:
                continue
            title = f'{filename} / {name}'
            suffix = PurePosixPath(name).suffix.lower()
            if suffix in ARCHIVE_SUFFIXES or suffix not in {'.pdf', '.docx', '.xlsx', '.txt', '.doc'}:
                collector.gap(title, 'nested_archive_unsupported' if suffix in ARCHIVE_SUFFIXES else 'unsupported_extension')
                continue
            try:
                member = _run_bounded([tool, 'x', '-so', '-spd', '-bd', '-y', '--', str(path), name],
                                      min(size, MAX_DOCUMENT_BYTES, remaining), deadline)
                if len(member) != size:
                    raise _Gap('archive_size_mismatch')
                remaining -= len(member)
                collector.add(member, title, name)
            except _Gap as exc:
                collector.gap(title, str(exc))
                if str(exc) in {'package_deadline', 'subprocess_output_limit'}:
                    raise
        if not any(not entry[2] for entry in entries):
            collector.gap(filename, 'empty_archive')


def _legacy_doc(data, deadline):
    if not data.startswith(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'):
        raise _Gap('format_mismatch')
    tool = shutil.which('antiword')
    if not tool:
        raise _Gap('antiword_not_installed')
    with tempfile.TemporaryDirectory(prefix='radar-doc-') as scratch:
        path = Path(scratch) / 'input.doc'
        path.write_bytes(data)
        raw = _run_bounded([tool, '-m', 'UTF-8.txt', str(path)], MAX_TEXT_CHARS * 4, min(deadline, time.monotonic() + 15))
        try:
            text = raw.decode('utf-8')
        except UnicodeError:
            raise _Gap('invalid_doc_encoding') from None
        text = '\n'.join(' '.join(line.split()) for line in text.splitlines() if line.strip())
        if len(text) > MAX_TEXT_CHARS:
            raise _Gap('text_too_large')
        return text


def _xml(data):
    # UTF-16/32 XML cannot bypass the byte-level DTD guard.
    if b'\x00' in data or re.search(br'<!\s*(?:DOCTYPE|ENTITY)\b', data, re.I):
        raise _Gap('xml_dtd_not_allowed')
    return ElementTree.fromstring(data)


def _xlsx_text(data):
    if not data.startswith(b'PK\x03\x04'):
        raise _Gap('format_mismatch')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = _zip_inventory(archive, container=True)
        by_name = {i.filename: i for i in infos}
        if not {'[Content_Types].xml', 'xl/workbook.xml'} <= set(by_name):
            raise _Gap('not_xlsx')
        if any(n.startswith('xl/embeddings/') or n.lower().endswith(('vbaproject.bin', '.zip', '.7z', '.rar')) for n in by_name):
            raise _Gap('embedded_content_not_allowed')
        remaining = MAX_EXPANDED_BYTES
        roots = {}
        for info in infos:
            if info.filename.endswith('.xml'):
                raw = _read_zip(archive, info, remaining)
                remaining -= len(raw)
                roots[info.filename] = _xml(raw)
        local = lambda node: node.tag.rsplit('}', 1)[-1]
        shared = []
        for root in [roots.get('xl/sharedStrings.xml')]:
            if root is not None:
                for node in root:
                    if local(node) == 'si':
                        shared.append(''.join(n.text or '' for n in node.iter() if local(n) == 't'))
        parts = []
        size = 0
        cells = rows = meaningful_cells = 0
        def add(value):
            nonlocal size
            value = ' '.join(value.split())
            if value:
                size += len(value) + 1
                if size > MAX_TEXT_CHARS:
                    raise _Gap('text_too_large')
                parts.append(value)
        sheets = sorted(n for n in roots if re.fullmatch(r'xl/worksheets/sheet[0-9]+\.xml', n))
        if len(sheets) > 50:
            raise _Gap('xlsx_sheet_limit')
        for name in sheets:
            add('Sheet: ' + name.rsplit('/', 1)[-1])
            for node in roots[name].iter():
                if local(node) == 'row':
                    rows += 1
                    if rows > 10000:
                        raise _Gap('xlsx_row_limit')
                if local(node) != 'c':
                    continue
                cells += 1
                if cells > 50000:
                    raise _Gap('xlsx_cell_limit')
                coordinate = node.get('r', '')
                if coordinate and not re.fullmatch(r'[A-Z]{1,3}[1-9][0-9]{0,6}', coordinate):
                    raise _Gap('invalid_xlsx_coordinate')
                children = {local(c): c for c in node}
                value = children.get('v')
                raw = value.text or '' if value is not None else ''
                if node.get('t') == 's':
                    if not raw.isdecimal() or int(raw) >= len(shared):
                        raise _Gap('invalid_xlsx_shared_string')
                    raw = shared[int(raw)]
                elif node.get('t') == 'inlineStr':
                    raw = ''.join(c.text or '' for c in node.iter() if local(c) == 't')
                if raw.strip():
                    meaningful_cells += 1
                    add(f'{coordinate}: {raw}')
                formula = children.get('f')
                if formula is not None and (formula.text or '').strip():
                    meaningful_cells += 1
                    add(f'{coordinate}: formula (not evaluated): {formula.text or ""}')
        # A sheet heading alone is not evidence of cell contents.
        if not meaningful_cells:
            return ''
        return '\n'.join(parts)


def _docx_without_embedded_objects(data):
    """Validate the entire container before omitting unread, non-text objects.

    Return a rebuilt in-memory document plus ignored member names. The existing
    spawned parser remains responsible for parsing Word XML and text limits.
    Rebuilding cannot launder an oversized/unsafe/encrypted original container.
    """
    if not data.startswith(b'PK\x03\x04'):
        raise _Gap('format_mismatch')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = _zip_inventory(archive, container=True)
        names = {info.filename for info in infos}
        if not {'[Content_Types].xml', 'word/document.xml'} <= names:
            raise _Gap('not_docx')
        if sum(info.file_size for info in infos) > MAX_DOCX_TOTAL_BYTES:
            raise _Gap('docx_total_too_large')
        if any(info.file_size > MAX_DOCX_MEMBER_BYTES for info in infos):
            raise _Gap('docx_member_too_large')
        ignored = [info.filename for info in infos if not info.is_dir() and (
            info.filename.startswith('word/embeddings/') or
            info.filename.lower().endswith(tuple(ARCHIVE_SUFFIXES)) or
            info.filename.lower().endswith('vbaproject.bin'))]
        if not ignored:
            return data, []
        output = io.BytesIO()
        remaining = MAX_DOCX_TOTAL_BYTES
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED) as clean:
            for info in infos:
                if info.is_dir() or info.filename in ignored:
                    continue
                member = _read_zip(archive, info, remaining)
                remaining -= len(member)
                clean.writestr(info.filename, member)
                if output.tell() > MAX_DOCUMENT_BYTES:
                    raise _Gap('document_too_large')
        rebuilt = output.getvalue()
        if len(rebuilt) > MAX_DOCUMENT_BYTES:
            raise _Gap('document_too_large')
        return rebuilt, ignored


class _Collector:
    def __init__(self):
        self.documents = []
        self.gaps = []
        self.chars = 0
        self.deadline = time.monotonic() + 30

    def gap(self, title, reason):
        self.gaps.append({'title': title, 'reason': reason})

    def add(self, data, title, name):
        if time.monotonic() >= self.deadline:
            self.gap(title, 'package_deadline')
            return
        if len(self.documents) >= MAX_DOCUMENTS:
            self.gap(title, 'document_count_limit')
            return
        if PurePosixPath(name).suffix.lower() in ARCHIVE_SUFFIXES:
            self.gap(title, 'nested_archive_unsupported')
            return
        if PurePosixPath(name).suffix.lower() in {'.xlsx', '.doc'}:
            try:
                text = _xlsx_text(data) if PurePosixPath(name).suffix.lower() == '.xlsx' else _legacy_doc(data, self.deadline)
                result = ExtractionResult('extracted', text) if text else ExtractionResult('empty', reason='no_extractable_text')
            except _Gap as exc:
                result = ExtractionResult('invalid', reason=str(exc))
            except (ElementTree.ParseError, zipfile.BadZipFile, ValueError, OSError):
                result = ExtractionResult('invalid', reason='invalid_xlsx')
        else:
            if PurePosixPath(name).suffix.lower() == '.docx':
                try:
                    data, ignored = _docx_without_embedded_objects(data)
                except _Gap as exc:
                    self.gap(title, str(exc))
                    return
                except (zipfile.BadZipFile, ValueError, OSError):
                    self.gap(title, 'invalid_docx')
                    return
                for member_name in ignored:
                    self.gap(f'{title} / {member_name}', 'embedded_object_not_read')
            result = extract_document_text(data, PurePosixPath(name).name,
                                           timeout_seconds=max(0.001, min(8, self.deadline - time.monotonic())))
        if time.monotonic() > self.deadline:
            self.gap(title, 'package_deadline')
            return
        if result.status != 'extracted':
            self.gap(title, result.reason or result.status)
        elif len(result.text) > MAX_TEXT_CHARS or self.chars + len(result.text) > MAX_TOTAL_TEXT_CHARS:
            self.gap(title, 'package_text_limit')
        else:
            self.documents.append({'title': title, 'text': result.text})
            self.chars += len(result.text)

    def result(self):
        return {'documents': self.documents, 'gaps': self.gaps,
                'status': ('retrieved_partial' if self.gaps else 'available') if self.documents else 'extraction_failed'}


def extract_package(data: bytes, filename: str) -> dict:
    """Return only extracted literal evidence and explicit failure/partial gaps."""
    collector = _Collector()
    label = filename if isinstance(filename, str) and filename else 'document'
    label = ''.join(c for c in label[:900] if ord(c) >= 32 and ord(c) != 127) or 'document'
    try:
        _safe_name(filename)
        if '/' in filename:
            raise _Gap('invalid_filename')
        if not isinstance(data, bytes):
            raise _Gap('expected_bytes')
        if len(data) > MAX_DOCUMENT_BYTES:
            raise _Gap('document_too_large')
        if not data:
            raise _Gap('empty_document')
        suffix = PurePosixPath(filename).suffix.lower()
        if suffix == '.7z':
            _seven_package(data, filename, collector)
        elif suffix == '.zip':
            if not data.startswith((b'PK\x03\x04', b'PK\x05\x06')):
                raise _Gap('format_mismatch')
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                infos = _zip_inventory(archive)
                remaining = MAX_EXPANDED_BYTES
                for info in infos:
                    if info.is_dir():
                        continue
                    title = f'{filename} / {info.filename}'
                    member = _read_zip(archive, info, remaining)
                    remaining -= len(member)
                    collector.add(member, title, info.filename)
                if not any(not info.is_dir() for info in infos):
                    collector.gap(filename, 'empty_archive')
        else:
            collector.add(data, filename, filename)
    except _Gap as exc:
        collector.gap(label, str(exc))
    except (zipfile.BadZipFile, EOFError, RuntimeError, ValueError, OSError):
        collector.gap(label, 'invalid_document_package')
    return collector.result()
