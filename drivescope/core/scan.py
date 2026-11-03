"""Whole-drive read scan, through a read-only file descriptor from UDisks2.

Python does the reading, so a scan can be cancelled and no root process is left
running. The descriptor is opened by UDisks2 with writable=false, which makes
writing through it impossible at the OS level.
"""

from __future__ import annotations

import mmap
import os
import time
from dataclasses import dataclass
from typing import Callable

import gi

from gi.repository import Gio, GLib

BUS = "org.freedesktop.UDisks2"
BLOCK_IFACE = "org.freedesktop.UDisks2.Block"
CHUNK_BYTES = 16 * 1024 * 1024
ALIGN_BYTES = 4096


def open_read_only(device_name: str, timeout_ms: int = 120_000) -> int:
    """Ask UDisks2 for a read-only O_DIRECT fd, prompting through polkit if needed."""
    bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    options = GLib.Variant("(a{sv})", ({"writable": GLib.Variant("b", False)},))
    result, fds = bus.call_with_unix_fd_list_sync(
        BUS,
        f"/org/freedesktop/UDisks2/block_devices/{device_name}",
        BLOCK_IFACE,
        "OpenForBenchmark",
        options,
        GLib.VariantType("(h)"),
        Gio.DBusCallFlags.NONE,
        timeout_ms,
        None,
        None,
    )
    return fds.get(result.unpack()[0])


def sample_offsets(device_size: int, points: int, chunk: int = CHUNK_BYTES) -> list[int]:
    """Evenly spaced, aligned offsets from the start of the drive to its end."""
    usable = device_size - chunk
    if usable <= 0:
        return []
    if points <= 1:
        return [0]
    return [(usable * i // (points - 1)) // ALIGN_BYTES * ALIGN_BYTES for i in range(points)]


@dataclass(frozen=True)
class ScanPoint:
    offset: int
    bps: float
    seconds: float


def scan(
    fd: int,
    offsets: list[int],
    on_point: Callable[[ScanPoint], None],
    cancelled: Callable[[], bool],
    chunk: int = CHUNK_BYTES,
) -> int:
    """Time a read at each offset. Returns how many points completed."""
    buffer = mmap.mmap(-1, chunk)  # page-aligned, as O_DIRECT requires
    done = 0
    try:
        for offset in offsets:
            if cancelled():
                break
            start = time.perf_counter()
            got = os.preadv(fd, [buffer], offset)
            elapsed = time.perf_counter() - start
            if got <= 0:
                break
            on_point(ScanPoint(offset, got / elapsed if elapsed > 0 else 0.0, elapsed))
            done += 1
    finally:
        buffer.close()
    return done
