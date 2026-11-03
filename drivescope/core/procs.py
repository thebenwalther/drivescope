"""Which processes are using a drive.

/proc/<pid>/io counts each process's storage I/O across all drives, so the rates
here are an approximation of what it's doing to this drive. Only the user's own
processes are visible without root.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class DriveUser:
    pid: int
    name: str
    open_files: int
    read_bps: float
    write_bps: float
    example_path: str


def _under(path: str, mountpoints: tuple[str, ...]) -> bool:
    return any(path == m or path.startswith(m + "/") for m in mountpoints)


def _paths_on_mounts(pid: str, mountpoints: tuple[str, ...]) -> list[str]:
    found = []
    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
        if _under(cwd, mountpoints):
            found.append(cwd)
    except OSError:
        pass
    try:
        for fd in os.listdir(f"/proc/{pid}/fd"):
            try:
                target = os.readlink(f"/proc/{pid}/fd/{fd}")
            except OSError:
                continue
            if _under(target, mountpoints):
                found.append(target)
    except OSError:
        pass
    return found


def parse_io(text: str) -> tuple[int, int]:
    values = dict(line.split(": ") for line in text.splitlines() if ": " in line)
    return int(values.get("read_bytes", 0)), int(values.get("write_bytes", 0))


class ProcessScanner:
    """Keeps the previous I/O totals so each scan can report rates."""

    def __init__(self) -> None:
        self._prev: dict[int, tuple[float, int, int]] = {}

    def scan(self, mountpoints: list[str], now: float) -> list[DriveUser]:
        mounts = tuple(mountpoints)
        users = []
        seen: dict[int, tuple[float, int, int]] = {}
        if not mounts:
            self._prev = {}
            return users
        own_uid = os.getuid()
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                if os.stat(f"/proc/{entry}").st_uid != own_uid or pid == os.getpid():
                    continue
                paths = _paths_on_mounts(entry, mounts)
                if not paths:
                    continue
                with open(f"/proc/{entry}/comm") as f:
                    name = f.read().strip()
                with open(f"/proc/{entry}/io") as f:
                    read_bytes, write_bytes = parse_io(f.read())
            except (OSError, ValueError):
                continue  # the process exited mid-scan, or we can't read it
            seen[pid] = (now, read_bytes, write_bytes)
            read_bps = write_bps = 0.0
            prev = self._prev.get(pid)
            if prev and now > prev[0]:
                read_bps = max(0, read_bytes - prev[1]) / (now - prev[0])
                write_bps = max(0, write_bytes - prev[2]) / (now - prev[0])
            users.append(DriveUser(pid, name, len(paths), read_bps, write_bps, paths[0]))
        self._prev = seen
        users.sort(key=lambda u: (-(u.read_bps + u.write_bps), u.name))
        return users
