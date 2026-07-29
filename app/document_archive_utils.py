from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path, PurePosixPath
from typing import Iterable


MAX_ARCHIVE_FILES = 200
MAX_ARCHIVE_TOTAL_BYTES = 250 * 1024 * 1024
MAX_ARCHIVE_DEPTH = 3

SUPPORTED_ARCHIVE_MEMBER_SUFFIXES = {
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".pdf",
    ".zip",
    ".rar",
    ".7z",
}


@dataclass(frozen=True)
class ArchiveMemberCandidate:
    name: str
    size: int = 0
    is_dir: bool = False


@dataclass(frozen=True)
class SafeArchiveMember:
    name: str
    safe_path: Path
    size: int = 0


def safe_archive_member_path(member_name: str) -> Path | None:
    normalized = str(member_name or "").replace("\\", "/")
    member_path = PurePosixPath(normalized)

    if member_path.is_absolute():
        return None

    parts = [part for part in member_path.parts if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return None

    if any(":" in part for part in parts):
        return None

    return Path(*parts)


def is_safe_child_path(root: Path, candidate: Path) -> bool:
    root_resolved = root.resolve()
    candidate_resolved = candidate.resolve()

    try:
        candidate_resolved.relative_to(root_resolved)
    except ValueError:
        return False

    return True


def is_supported_archive_member(member_path: Path) -> bool:
    return member_path.suffix.lower() in SUPPORTED_ARCHIVE_MEMBER_SUFFIXES


def select_safe_archive_members(
    candidates: Iterable[ArchiveMemberCandidate],
    *,
    archive_path: Path | None = None,
    max_files: int = MAX_ARCHIVE_FILES,
    max_total_size: int = MAX_ARCHIVE_TOTAL_BYTES,
    logger: logging.Logger | None = None,
) -> list[SafeArchiveMember]:
    selected: list[SafeArchiveMember] = []
    total_size = 0

    for candidate in candidates:
        if candidate.is_dir:
            continue

        safe_path = safe_archive_member_path(candidate.name)
        if safe_path is None:
            log_archive_skip(logger, archive_path, candidate.name, "unsafe_path")
            continue

        if not is_supported_archive_member(safe_path):
            continue

        size = max(int(candidate.size or 0), 0)
        if len(selected) >= max_files:
            log_archive_skip(logger, archive_path, candidate.name, "file_count_limit")
            continue

        if total_size + size > max_total_size:
            log_archive_skip(logger, archive_path, candidate.name, "total_size_limit")
            continue

        selected.append(
            SafeArchiveMember(
                name=candidate.name,
                safe_path=safe_path,
                size=size,
            )
        )
        total_size += size

    return selected


def log_archive_skip(
    logger: logging.Logger | None,
    archive_path: Path | None,
    member_name: str,
    reason: str,
) -> None:
    if logger is None:
        return

    logger.warning(
        "Archive member skipped: archive=%s member=%r reason=%s",
        archive_path,
        member_name,
        reason,
    )
