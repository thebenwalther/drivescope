"""Tests with real files, which fio's synthetic jobs can't stand in for."""

from __future__ import annotations

import mmap
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import safety
from .fio import Cancelled

SMALL_FILE_LIMIT = 64 * 1024
MAX_SMALL_FILES = 5000
MIN_SMALL_FILES = 100  # fewer than this says nothing about small-file speed
COPY_BLOCK = 8 * 1024 * 1024
MIN_BIG_FILE = 1024 * 1024 * 1024


@dataclass(frozen=True)
class VolumeFiles:
    largest_path: str | None
    largest_size: int
    small_files: tuple[str, ...]


def survey_volume(mountpoint: str, cancelled: Callable[[], bool] = lambda: False) -> VolumeFiles:
    """Find the largest file and a spread of small ones, for read-only tests."""
    largest_path, largest_size = None, 0
    small: list[str] = []
    stack = [mountpoint]
    while stack:
        if cancelled():
            raise Cancelled()
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    size = entry.stat(follow_symlinks=False).st_size
                    if size > largest_size:
                        largest_path, largest_size = entry.path, size
                    if 0 < size < SMALL_FILE_LIMIT and len(small) < MAX_SMALL_FILES:
                        small.append(entry.path)
            except OSError:
                continue
    return VolumeFiles(largest_path, largest_size, tuple(small))


@dataclass(frozen=True)
class SmallFileResult:
    files: int
    total_bytes: int
    seconds: float
    settled_seconds: float  # including the flush to the drive, for writes

    @property
    def files_per_s(self) -> float:
        return self.files / self.seconds if self.seconds else 0.0

    @property
    def bps(self) -> float:
        return self.total_bytes / self.settled_seconds if self.settled_seconds else 0.0


def read_small_files(paths: tuple[str, ...], cancelled: Callable[[], bool]) -> SmallFileResult:
    """Read each file once with the cache bypassed, as a batch of small files."""
    buffer = mmap.mmap(-1, SMALL_FILE_LIMIT)
    files = total = 0
    start = time.perf_counter()
    try:
        for path in paths:
            if cancelled():
                raise Cancelled()
            try:
                # O_NOATIME, so reading doesn't update the file's "last opened" date.
                fd = os.open(path, os.O_RDONLY | os.O_DIRECT | os.O_NOFOLLOW | os.O_NOATIME)
            except OSError:
                continue
            try:
                total += os.readv(fd, [buffer])
                files += 1
            except OSError:
                pass
            finally:
                os.close(fd)
    finally:
        buffer.close()
    elapsed = time.perf_counter() - start
    return SmallFileResult(files, total, elapsed, elapsed)


def write_small_files(
    test_dir: safety.TestDir, count: int, file_size: int, cancelled: Callable[[], bool], max_seconds: float | None = None,
) -> SmallFileResult:
    """Write a batch of files, timing both when they appear done and when they really are.

    Some drives manage only a few files a second, so max_seconds can end the batch
    early. At least MIN_SMALL_FILES are always written, so the rate means something.
    """
    block = os.urandom(file_size)
    start = time.perf_counter()
    written = 0
    for i in range(count):
        if cancelled():
            raise Cancelled()
        if max_seconds is not None and i >= MIN_SMALL_FILES and time.perf_counter() - start >= max_seconds:
            break
        path = safety.approve_write_path(test_dir.file(f"small-{i:06d}.bin"))
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, block)
        finally:
            os.close(fd)
        written += 1
    apparent = time.perf_counter() - start
    os.syncfs(test_dir.fd) if hasattr(os, "syncfs") else _syncfs(test_dir.fd)
    return SmallFileResult(written, written * file_size, apparent, time.perf_counter() - start)


def _syncfs(fd: int) -> None:
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    if libc.syncfs(fd) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


@dataclass(frozen=True)
class CopyResult:
    total_bytes: int
    apparent_bps: float  # what a progress bar shows, before the cache drains
    on_disk_bps: float  # after every byte has reached the drive
    source_bps: float  # how fast the source can be read alone


def copy_source_path() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "drivescope" / "copy-source.bin"


def real_copy(
    test_dir: safety.TestDir,
    total_bytes: int,
    cancelled: Callable[[], bool],
    source: Path | None = None,
    on_progress: Callable[[float, float], None] | None = None,
) -> CopyResult:
    """Copy a real file from the internal drive through the page cache, as a file manager would."""
    source = source or copy_source_path()
    source.parent.mkdir(parents=True, exist_ok=True)
    dest = safety.approve_write_path(test_dir.file("copy-target.bin"))
    try:
        # Random data, so transparent compression on either side can't flatter the result.
        with open(source, "wb") as out:
            written = 0
            while written < total_bytes:
                if cancelled():
                    raise Cancelled()
                block = min(COPY_BLOCK, total_bytes - written)
                out.write(os.urandom(block))
                written += block
            out.flush()
            os.fsync(out.fileno())

        source_fd = os.open(source, os.O_RDONLY)
        os.posix_fadvise(source_fd, 0, 0, os.POSIX_FADV_DONTNEED)  # read it from the drive, not RAM
        start = time.perf_counter()
        while os.read(source_fd, COPY_BLOCK):
            if cancelled():
                os.close(source_fd)
                raise Cancelled()
        source_seconds = time.perf_counter() - start
        os.posix_fadvise(source_fd, 0, 0, os.POSIX_FADV_DONTNEED)
        os.lseek(source_fd, 0, os.SEEK_SET)

        dest_fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        copied = 0
        start = last = time.perf_counter()
        last_bytes = 0
        try:
            while True:
                if cancelled():
                    raise Cancelled()
                chunk = os.read(source_fd, COPY_BLOCK)
                if not chunk:
                    break
                os.write(dest_fd, chunk)
                copied += len(chunk)
                now = time.perf_counter()
                if on_progress and now - last >= 1:
                    on_progress(now - start, (copied - last_bytes) / (now - last))
                    last, last_bytes = now, copied
            apparent_seconds = time.perf_counter() - start
            os.fsync(dest_fd)
            settled_seconds = time.perf_counter() - start
        finally:
            os.close(dest_fd)
            os.close(source_fd)
        return CopyResult(
            total_bytes=copied,
            apparent_bps=copied / apparent_seconds,
            on_disk_bps=copied / settled_seconds,
            source_bps=written / source_seconds,
        )
    finally:
        source.unlink(missing_ok=True)
