"""Device-level speed from /proc/diskstats.

These counters report what the block device actually did, independent of the
page cache, so they are the reference every benchmark result is checked against.
Sector counts in diskstats are always 512-byte units, whatever the drive's
physical sector size.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

SECTOR_BYTES = 512


@dataclass(frozen=True)
class Counters:
    read_ios: int
    read_sectors: int
    read_ticks_ms: int
    write_ios: int
    write_sectors: int
    write_ticks_ms: int
    in_flight: int
    io_ticks_ms: int
    weighted_ticks_ms: int


@dataclass(frozen=True)
class Sample:
    """One interval of activity, derived from two counter readings."""

    timestamp: float
    read_bps: float
    write_bps: float
    read_iops: float
    write_iops: float
    read_latency_ms: float
    write_latency_ms: float
    queue_depth: float
    busy_pct: float
    in_flight: int


def parse_diskstats(text: str) -> dict[str, Counters]:
    devices: dict[str, Counters] = {}
    for line in text.splitlines():
        fields = line.split()
        # major minor name + at least the 11 classic counters.
        if len(fields) < 14:
            continue
        values = [int(v) for v in fields[3:14]]
        devices[fields[2]] = Counters(
            read_ios=values[0],
            read_sectors=values[2],
            read_ticks_ms=values[3],
            write_ios=values[4],
            write_sectors=values[6],
            write_ticks_ms=values[7],
            in_flight=values[8],
            io_ticks_ms=values[9],
            weighted_ticks_ms=values[10],
        )
    return devices


def derive(prev: Counters, cur: Counters, dt: float, timestamp: float = 0.0) -> Sample | None:
    """Turn two readings into rates.

    Returns None when the interval is unusable: no elapsed time, or a counter
    that went backwards, which means the device was replugged between readings.
    """
    if dt <= 0:
        return None
    monotonic = (
        ("read_ios"), ("read_sectors"), ("read_ticks_ms"),
        ("write_ios"), ("write_sectors"), ("write_ticks_ms"),
        ("io_ticks_ms"), ("weighted_ticks_ms"),
    )
    if any(getattr(cur, f) < getattr(prev, f) for f in monotonic):
        return None

    read_ios = cur.read_ios - prev.read_ios
    write_ios = cur.write_ios - prev.write_ios
    dt_ms = dt * 1000.0
    return Sample(
        timestamp=timestamp,
        read_bps=(cur.read_sectors - prev.read_sectors) * SECTOR_BYTES / dt,
        write_bps=(cur.write_sectors - prev.write_sectors) * SECTOR_BYTES / dt,
        read_iops=read_ios / dt,
        write_iops=write_ios / dt,
        read_latency_ms=(cur.read_ticks_ms - prev.read_ticks_ms) / read_ios if read_ios else 0.0,
        write_latency_ms=(cur.write_ticks_ms - prev.write_ticks_ms) / write_ios if write_ios else 0.0,
        queue_depth=(cur.weighted_ticks_ms - prev.weighted_ticks_ms) / dt_ms,
        # io_ticks can slightly overshoot the wall clock interval; clamp for display.
        busy_pct=min(100.0, (cur.io_ticks_ms - prev.io_ticks_ms) / dt_ms * 100.0),
        in_flight=cur.in_flight,
    )


class Sampler:
    """Polls one device. Call tick() on a timer; each call returns a Sample or None."""

    def __init__(
        self,
        device: str,
        read_text: Callable[[], str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.device = device
        self._read_text = read_text or (lambda: Path("/proc/diskstats").read_text())
        self._clock = clock
        self._prev: Counters | None = None
        self._prev_t = 0.0

    def tick(self) -> Sample | None:
        now = self._clock()
        cur = parse_diskstats(self._read_text()).get(self.device)
        if cur is None:
            self._prev = None
            return None
        sample = None
        if self._prev is not None:
            sample = derive(self._prev, cur, now - self._prev_t, timestamp=now)
        self._prev, self._prev_t = cur, now
        return sample


def total_sectors_written(device: str, read_text: Callable[[], str] | None = None) -> int | None:
    """Lifetime sectors written since boot. Used to prove a read suite wrote nothing."""
    text = (read_text or (lambda: Path("/proc/diskstats").read_text()))()
    counters = parse_diskstats(text).get(device)
    return counters.write_sectors if counters else None


def parse_meminfo_pending(text: str) -> tuple[int, int]:
    """System-wide (dirty, writeback) bytes. The kernel only exposes these per
    device through root-only debugfs, so they can't be attributed to one drive."""
    dirty = writeback = 0
    for line in text.splitlines():
        if line.startswith("Dirty:"):
            dirty = int(line.split()[1]) * 1024
        elif line.startswith("Writeback:"):
            writeback = int(line.split()[1]) * 1024
    return dirty, writeback


def pending_write_bytes() -> tuple[int, int]:
    return parse_meminfo_pending(Path("/proc/meminfo").read_text())
