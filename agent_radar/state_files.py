"""Private state on a local filesystem: owner-only roots, atomic writes, one writer at a time."""
from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def _sync_directory(directory: Path) -> None:
    if os.name == "posix":
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _atomic_text(target: Path, value: str) -> None:
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise ValueError("operator state must be a regular file")
    fd, name = tempfile.mkstemp(prefix=".state-", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
        _sync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _private_root(root: Path) -> None:
    if not root.is_dir() or root.is_symlink():
        raise ValueError("private bundle root must exist as a real directory")
    if os.name == "posix" and root.stat().st_mode & 0o077:
        raise ValueError("private bundle root must not be accessible to group or others")


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    """Nonblocking OS lock; never unlink lock inode (inode replacement breaks locking)."""
    if path.is_symlink():
        raise ValueError("operator lock must not be a symlink")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except PermissionError:
                raise BlockingIOError("operator job already holds exclusive lock") from None
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "nt":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
