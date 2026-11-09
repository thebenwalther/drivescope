"""The only code allowed to approve a write target.

DriveScope benchmarks real drives, so a bug here could destroy someone's data.
The rules:

* Writes only ever go to regular files inside a `.drivescope-test-<uuid>/`
  directory, created by this module, directly under a mountpoint.
* The filesystem must be mounted read-write.
* The filesystem must not back /, /home or /boot.
* Block devices are never a valid target, whatever path reaches them.
* The volume's top folder must not let other accounts rename what's in it.
* Writes reach the test directory through a file descriptor held open from the
  moment it's created, never by its name. Renaming or replacing the directory
  during a run can't send a write anywhere else.

Everything that builds a write job has to pass through approve_write_path().
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from .mounts import Mount, mount_at, read_mounts

TEST_DIR_PREFIX = ".drivescope-test-"
SYSTEM_MOUNTPOINTS = ("/", "/home", "/boot")
_TEST_DIR_RE = re.compile(rf"^{re.escape(TEST_DIR_PREFIX)}[0-9a-f]{{32}}$")
# The only shape a write path can take: a plain file name inside an open test
# directory, reached through its descriptor. A child process given the same
# descriptor resolves it to the same directory.
_FD_ROOT = "/proc/self/fd"
_ANCHORED_RE = re.compile(rf"^{re.escape(_FD_ROOT)}/(\d+)/([A-Za-z0-9][A-Za-z0-9._-]*)$")


class UnsafeTarget(Exception):
    """A write was requested somewhere it must never go. str(e) is user-facing."""


@dataclass(eq=False)
class TestDir:
    """A test directory held open for the length of a run."""

    __test__ = False  # not a unittest case, despite the name

    mountpoint: str
    name: str
    fd: int
    device: int  # st_dev of the volume's top folder, which a rename can't leave

    @property
    def path(self) -> str:
        """Where it was created. For messages only: never write through this."""
        return os.path.join(self.mountpoint, self.name)

    def file(self, name: str) -> str:
        """The write path for a file in this directory. Approve it before use."""
        path = f"{_FD_ROOT}/{self.fd}/{name}"
        if not _ANCHORED_RE.match(path):
            raise UnsafeTarget(f"Refusing an unusual test file name: {name}")
        return path


_open_dirs: dict[int, TestDir] = {}
_open_lock = threading.Lock()


def open_test_dir_fds() -> tuple[int, ...]:
    """Descriptors a write job's process must inherit to reach its test directory."""
    with _open_lock:
        return tuple(_open_dirs)


def _system_sources(mounts: list[Mount]) -> set[str]:
    # Compare by source device, not path: btrfs subvolumes put / and /home on
    # the same device under different mountpoints.
    sources = set()
    for mp in SYSTEM_MOUNTPOINTS:
        mount = mount_at(mp, mounts)
        if mount is not None:
            sources.add(mount.source)
    return sources


def write_block_reason(mountpoint: str | None, mounts: list[Mount] | None = None) -> str | None:
    """Why write tests can't run on this volume, or None if they can."""
    if not mountpoint:
        return "This volume isn't mounted."
    mounts = read_mounts() if mounts is None else mounts
    mount = mount_at(mountpoint, mounts)
    if mount is None:
        return "This volume isn't mounted."
    if mount.read_only:
        return "This volume is mounted read-only, so nothing can be written to it."
    if mount.source in _system_sources(mounts):
        return "This volume holds your system or home files, so write tests are refused."
    return None


def _shared_top_reason(st: os.stat_result) -> str | None:
    """Why other accounts could tamper with what's directly under this folder."""
    if st.st_uid not in (0, os.geteuid()):
        return ("This volume's top folder belongs to another account, which could move the "
                "test files during a run. Write tests are refused.")
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH) and not st.st_mode & stat.S_ISVTX:
        return ("Other accounts can rename things in this volume's top folder, so write tests "
                "are refused. Make it private (chmod go-w) or set the sticky bit (chmod +t).")
    return None


def _check_open_dir(test_dir: TestDir) -> None:
    """The open directory is still one only this account can change, on the same volume."""
    st = os.fstat(test_dir.fd)
    if not stat.S_ISDIR(st.st_mode):
        raise UnsafeTarget(f"The test directory isn't a directory any more: {test_dir.path}")
    if st.st_uid != os.geteuid():
        raise UnsafeTarget(f"The test directory belongs to another account: {test_dir.path}")
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise UnsafeTarget(f"Other accounts can write to the test directory: {test_dir.path}")
    if st.st_dev != test_dir.device:
        raise UnsafeTarget(f"The test directory isn't on the volume being tested: {test_dir.path}")


def create_test_dir(mountpoint: str, mounts: list[Mount] | None = None) -> TestDir:
    """Create a fresh test directory on an approved volume and hold it open."""
    mounts = read_mounts() if mounts is None else mounts
    reason = write_block_reason(mountpoint, mounts)
    if reason:
        raise UnsafeTarget(reason)
    name = f"{TEST_DIR_PREFIX}{uuid.uuid4().hex}"
    top_fd = os.open(mountpoint, os.O_RDONLY | os.O_DIRECTORY)
    try:
        top = os.fstat(top_fd)
        reason = _shared_top_reason(top)
        if reason:
            raise UnsafeTarget(reason)
        os.mkdir(name, 0o700, dir_fd=top_fd)
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=top_fd)
    finally:
        os.close(top_fd)
    test_dir = TestDir(mountpoint, name, fd, top.st_dev)
    try:
        _check_open_dir(test_dir)
    except UnsafeTarget:
        os.close(fd)
        raise
    with _open_lock:
        _open_dirs[fd] = test_dir
    return test_dir


def approve_write_path(path: str, mounts: list[Mount] | None = None) -> str:
    """Return the path if it may be written to, otherwise raise UnsafeTarget.

    Only paths from TestDir.file() pass: anything named by an ordinary path,
    however it's spelled, is refused.
    """
    match = _ANCHORED_RE.match(path)
    if not match:
        raise UnsafeTarget(f"Refusing a write outside a DriveScope test directory: {path}")
    fd, name = int(match.group(1)), match.group(2)
    with _open_lock:
        test_dir = _open_dirs.get(fd)
    if test_dir is None:
        raise UnsafeTarget(f"Refusing a write outside an open DriveScope test directory: {path}")
    _check_open_dir(test_dir)

    try:
        st = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    except OSError as e:
        # Anything we can't inspect is refused, never assumed to be fine.
        raise UnsafeTarget(f"Can't check this write path ({e.strerror}): {name}") from e
    else:
        if not stat.S_ISREG(st.st_mode):
            raise UnsafeTarget(f"Refusing to write to something that isn't a regular file: {name}")
        if st.st_nlink > 1:
            raise UnsafeTarget(f"Refusing to write to a file that's linked elsewhere: {name}")

    reason = write_block_reason(test_dir.mountpoint, mounts)
    if reason:
        raise UnsafeTarget(reason)
    return path


def _empty_dir(fd: int) -> None:
    for entry in os.listdir(fd):
        st = os.stat(entry, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISDIR(st.st_mode):
            shutil.rmtree(entry, dir_fd=fd)
        else:
            os.unlink(entry, dir_fd=fd)


def _remove_open_dir(mountpoint: str, name: str, fd: int) -> None:
    """Empty the directory through its descriptor, then remove it by name only if
    the name still points at that same directory."""
    _empty_dir(fd)
    held = os.fstat(fd)
    top_fd = os.open(mountpoint, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            st = os.stat(name, dir_fd=top_fd, follow_symlinks=False)
        except FileNotFoundError:
            return  # moved away: its contents are gone, and the rest isn't ours to touch
        if (st.st_dev, st.st_ino) == (held.st_dev, held.st_ino):
            os.rmdir(name, dir_fd=top_fd)
    finally:
        os.close(top_fd)


def remove_test_dir(test_dir: TestDir) -> None:
    """Delete a test directory created by create_test_dir(), and close it."""
    with _open_lock:
        _open_dirs.pop(test_dir.fd, None)
    try:
        _remove_open_dir(test_dir.mountpoint, test_dir.name, test_dir.fd)
    finally:
        os.close(test_dir.fd)


def remove_orphaned_test_dir(path: str) -> None:
    """Delete a test directory a crash left behind. Refuses anything that isn't
    one, is a symlink, or belongs to another account."""
    path = path.rstrip("/")
    mountpoint, name = os.path.split(path)
    if not _TEST_DIR_RE.match(name):
        raise UnsafeTarget(f"Refusing to delete something that isn't a test directory: {path}")
    top_fd = os.open(mountpoint, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=top_fd)
        except FileNotFoundError:
            return
        except OSError as e:
            # ELOOP for a symlink, ENOTDIR for anything else.
            raise UnsafeTarget(f"Refusing to delete something that isn't a test directory: {path}") from e
    finally:
        os.close(top_fd)
    try:
        if os.fstat(fd).st_uid != os.geteuid():
            raise UnsafeTarget(f"Refusing to delete a test directory owned by another account: {path}")
        _remove_open_dir(mountpoint, name, fd)
    finally:
        os.close(fd)


def find_orphaned_test_dirs(mountpoint: str) -> list[str]:
    """Test directories left behind by a crash. Removed on the next launch."""
    try:
        entries = list(Path(mountpoint).iterdir())
    except OSError:
        return []
    return [
        str(e) for e in entries
        if _TEST_DIR_RE.match(e.name) and e.is_dir() and not e.is_symlink()
    ]
