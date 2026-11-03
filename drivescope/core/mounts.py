"""Mount table parsing from /proc/self/mountinfo."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_OCTAL = re.compile(r"\\([0-7]{3})")


def _unescape(value: str) -> str:
    # The kernel writes spaces and tabs in paths as octal, e.g. \040.
    return _OCTAL.sub(lambda m: chr(int(m.group(1), 8)), value)


@dataclass(frozen=True)
class Mount:
    device_id: str  # "major:minor"
    mountpoint: str
    options: tuple[str, ...]
    fstype: str
    source: str
    super_options: tuple[str, ...]

    @property
    def read_only(self) -> bool:
        return "ro" in self.options or "ro" in self.super_options


def parse_mountinfo(text: str) -> list[Mount]:
    mounts = []
    for line in text.splitlines():
        left, sep, right = line.partition(" - ")
        if not sep:
            continue
        lf = left.split()
        rf = right.split()
        if len(lf) < 6 or len(rf) < 3:
            continue
        mounts.append(
            Mount(
                device_id=lf[2],
                mountpoint=_unescape(lf[4]),
                options=tuple(lf[5].split(",")),
                fstype=rf[0],
                source=_unescape(rf[1]),
                super_options=tuple(rf[2].split(",")),
            )
        )
    return mounts


def read_mounts() -> list[Mount]:
    return parse_mountinfo(Path("/proc/self/mountinfo").read_text())


def mount_at(mountpoint: str, mounts: list[Mount]) -> Mount | None:
    """The mount visible at this exact path. The last entry wins when stacked."""
    found = None
    for mount in mounts:
        if mount.mountpoint == mountpoint:
            found = mount
    return found


def mount_containing(path: str, mounts: list[Mount]) -> Mount | None:
    """The mount that holds this absolute path, by longest matching prefix."""
    best = None
    best_len = -1
    for mount in mounts:
        mp = mount.mountpoint
        if path == mp or mp == "/" or path.startswith(mp + "/"):
            if len(mp) >= best_len:
                best, best_len = mount, len(mp)
    return best
