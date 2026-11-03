"""The only code allowed to approve a write target.

DriveScope benchmarks real drives, so a bug here could destroy someone's data.
The rules:

* Writes only ever go to regular files inside a `.drivescope-test-<uuid>/`
  directory, created by this module, directly under a mountpoint.
* The filesystem must be mounted read-write.
* The filesystem must not back /, /home or /boot.
* Block devices are never a valid target, whatever path reaches them.

Everything that builds a write job has to pass through approve_write_path().
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import uuid
from pathlib import Path

from .mounts import Mount, mount_at, mount_containing, read_mounts

TEST_DIR_PREFIX = ".drivescope-test-"
SYSTEM_MOUNTPOINTS = ("/", "/home", "/boot")
_TEST_DIR_RE = re.compile(rf"^{re.escape(TEST_DIR_PREFIX)}[0-9a-f]{{32}}$")


class UnsafeTarget(Exception):
    """A write was requested somewhere it must never go. str(e) is user-facing."""


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


def create_test_dir(mountpoint: str, mounts: list[Mount] | None = None) -> str:
    """Create a fresh test directory on an approved volume and return its path."""
    reason = write_block_reason(mountpoint, mounts)
    if reason:
        raise UnsafeTarget(reason)
    path = os.path.join(mountpoint, f"{TEST_DIR_PREFIX}{uuid.uuid4().hex}")
    os.mkdir(path, 0o700)
    return path


def approve_write_path(path: str, mounts: list[Mount] | None = None) -> str:
    """Return the path if it may be written to, otherwise raise UnsafeTarget."""
    if not os.path.isabs(path):
        raise UnsafeTarget(f"Refusing a relative write path: {path}")

    # Check the unresolved path first so a symlink can't launder its way past
    # the name check, then the resolved one so it can't escape the directory.
    if os.path.realpath(path) != os.path.normpath(path):
        raise UnsafeTarget(f"Refusing a write path that goes through a symlink: {path}")
    if path.startswith("/dev/") or path.startswith("/sys/") or path.startswith("/proc/"):
        raise UnsafeTarget(f"Refusing to write to a system path: {path}")

    parent = os.path.dirname(path)
    if not _TEST_DIR_RE.match(os.path.basename(parent)):
        raise UnsafeTarget(f"Refusing a write outside a DriveScope test directory: {path}")

    try:
        st = os.lstat(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        # Anything we can't inspect is refused, never assumed to be fine.
        raise UnsafeTarget(f"Can't check this write path ({e.strerror}): {path}") from e
    else:
        if not stat.S_ISREG(st.st_mode):
            raise UnsafeTarget(f"Refusing to write to something that isn't a regular file: {path}")

    mounts = read_mounts() if mounts is None else mounts
    mount = mount_containing(path, mounts)
    if mount is None:
        raise UnsafeTarget(f"Can't tell which volume this path is on: {path}")
    if os.path.dirname(parent) != mount.mountpoint:
        raise UnsafeTarget(f"The test directory must sit directly on the volume: {path}")
    reason = write_block_reason(mount.mountpoint, mounts)
    if reason:
        raise UnsafeTarget(reason)
    return path


def remove_test_dir(path: str) -> None:
    """Delete a test directory. Refuses anything that isn't one, or is a symlink."""
    if not _TEST_DIR_RE.match(os.path.basename(path.rstrip("/"))):
        raise UnsafeTarget(f"Refusing to delete something that isn't a test directory: {path}")
    if os.path.islink(path):
        raise UnsafeTarget(f"Refusing to delete a symlink: {path}")
    if os.path.isdir(path):
        shutil.rmtree(path)


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
