"""Decide what can honestly be said about unplugging a drive.

Linux only reports pending writes per device through root-only debugfs, so the
system-wide Dirty and Writeback totals can't be pinned on one drive. A write
sitting in RAM also doesn't touch the device's counters until it's flushed, so
a successful flush can't stay trusted for long: it expires after FLUSH_TRUST_S.
Ejecting is the only lasting guarantee.
"""

from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass

from .devices import Drive

FLUSH_TRUST_S = 30.0

SAFE = "safe"
BUSY = "busy"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class UnplugStatus:
    state: str
    title: str
    detail: str


def evaluate(
    drive: Drive,
    in_flight: int,
    dirty_bytes: int,
    writeback_bytes: int,
    seconds_since_flush: float | None,
    sectors_written_since_flush: int,
) -> UnplugStatus:
    if in_flight > 0:
        return UnplugStatus(
            BUSY, "Drive is working",
            f"{in_flight} operation{'s' if in_flight != 1 else ''} in progress. Wait for this to reach zero.",
        )

    writable = [p for p in drive.mounted if not p.mount_read_only]
    if not drive.mounted:
        return UnplugStatus(SAFE, "Safe to unplug", "Nothing on this drive is mounted.")
    if not writable:
        return UnplugStatus(
            SAFE, "Safe to unplug",
            "It's mounted read-only, so no data can be waiting to be written to it.",
        )
    if dirty_bytes + writeback_bytes == 0:
        return UnplugStatus(
            SAFE, "Safe to unplug",
            "No data is waiting to be written to any drive on this computer.",
        )
    if (
        seconds_since_flush is not None
        and seconds_since_flush <= FLUSH_TRUST_S
        and sectors_written_since_flush == 0
    ):
        return UnplugStatus(
            SAFE, "Safe to unplug",
            f"Flushed {seconds_since_flush:.0f} s ago with no writes reaching the drive since. "
            "Eject to keep it certain.",
        )
    return UnplugStatus(
        UNKNOWN, "Not confirmed",
        "Linux can't show how much waiting data belongs to this drive. "
        "Flush to confirm, or eject it.",
    )


def flush_mountpoints(mountpoints: list[str]) -> None:
    """Block until each filesystem's cached writes reach the drive. Raises OSError."""
    libc = ctypes.CDLL(None, use_errno=True)
    for mountpoint in mountpoints:
        fd = os.open(mountpoint, os.O_RDONLY | os.O_DIRECTORY)
        try:
            if libc.syncfs(fd) != 0:
                err = ctypes.get_errno()
                raise OSError(err, os.strerror(err), mountpoint)
        finally:
            os.close(fd)
