"""Live status of external drives as JSON lines, for the Omarchy bar panel.

Uses the same counters and the same safe-to-unplug rules as the app, so the
panel and the app can't disagree. It only reads /proc, /sys and lsblk: it never
opens a drive, and it needs nothing beyond the Python standard library.

The drive list is read on a background thread because checking a stuck mount
can block forever. Speeds keep updating if that happens, and every drive reports
"Checking" until the list is known to match the current mounts, so a stale list
can't produce "Safe to unplug".
"""

from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from .core import devices, stats, unplug
from .core import format as fmt
from .core.devices import Drive

PROTOCOL_VERSION = 1
RESCAN_S = 10.0  # catches a drive whose USB details weren't ready when it appeared
ACTIVE_BPS = 1_000_000  # below this, the bar shows only the icon
MAX_DRIVES = 16
MAX_TEXT = 64

_STATE_ORDER = (unplug.BUSY, unplug.UNKNOWN, unplug.SAFE)


def _clip(text: str) -> str:
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"


def _read_signature() -> tuple[tuple[str, ...], str]:
    return tuple(sorted(os.listdir("/sys/block"))), Path("/proc/self/mountinfo").read_text()


def _app_available() -> bool:
    """Whether the GTK app can open. The panel itself doesn't need any of this."""
    importlib.invalidate_caches()
    return (
        importlib.util.find_spec("gi") is not None
        and importlib.util.find_spec("cairo") is not None
        and bool(glob.glob("/usr/lib*/girepository-1.0/Adw-1.typelib"))
    )


def _link_text(drive: Drive) -> str:
    if drive.link is None:
        return drive.transport.upper()
    speed = drive.link.speed_mbps
    rate = f"{speed / 1000:g} Gbps" if speed >= 1000 else f"{speed} Mbps"
    return f"{rate} · {drive.link.protocol}"


def _volumes_text(drive: Drive) -> str:
    mounted = drive.mounted
    if not mounted:
        return "Not mounted"
    names = ", ".join(_clip(p.display_name) for p in mounted)
    return f"{names} (read-only)" if all(p.mount_read_only for p in mounted) else names


def _activity(read_bps: float, write_bps: float) -> str:
    parts = []
    if read_bps >= ACTIVE_BPS:
        parts.append(f"R {fmt.rate(read_bps)}")
    if write_bps >= ACTIVE_BPS:
        parts.append(f"W {fmt.rate(write_bps)}")
    return "  ".join(parts)


def _tooltip(entries: list[dict]) -> str:
    if not entries:
        return "DriveScope: no external drive"
    if len(entries) == 1:
        return f"{entries[0]['name']}: {entries[0]['title']}"
    states = {e["state"] for e in entries}
    if states == {unplug.SAFE}:
        summary = "all safe to unplug"
    elif unplug.BUSY in states:
        summary = "at least one is working"
    else:
        summary = "not all confirmed safe to unplug"
    return f"{len(entries)} drives: {summary}"


class Watcher:
    def __init__(
        self,
        list_drives: Callable[[], list[Drive]] = devices.list_drives,
        read_diskstats: Callable[[], str] = lambda: Path("/proc/diskstats").read_text(),
        read_meminfo: Callable[[], str] = lambda: Path("/proc/meminfo").read_text(),
        read_signature: Callable[[], object] = _read_signature,
        app_available: Callable[[], bool] = _app_available,
        clock: Callable[[], float] = time.monotonic,
        spawn: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self._list_drives = list_drives
        self._read_diskstats = read_diskstats
        self._read_meminfo = read_meminfo
        self._read_signature = read_signature
        self._check_app = app_available
        self._clock = clock
        self._spawn = spawn or (lambda fn: threading.Thread(target=fn, daemon=True).start())
        self._lock = threading.Lock()
        self._drives: list[Drive] = []
        self._app = False
        self._error = ""
        self._scanned_signature: object = None
        self._scanning = False
        self._last_scan = float("-inf")
        self._prev: dict[str, tuple[stats.Counters, float]] = {}

    def _scan(self, signature: object) -> None:
        try:
            drives = self._list_drives()
            app = self._check_app()
        except (OSError, subprocess.SubprocessError, ValueError) as err:
            with self._lock:
                self._error = f"Couldn't list drives: {err}"
                self._scanning = False
            return
        with self._lock:
            self._drives = drives[:MAX_DRIVES]
            self._app = app
            self._error = ""
            self._scanned_signature = signature
            self._scanning = False

    def _describe(self, drive: Drive, counters: stats.Counters, now: float,
                  dirty: int, writeback: int, settled: bool) -> dict:
        previous = self._prev.get(drive.name)
        sample = None
        if previous is not None:
            sample = stats.derive(previous[0], counters, now - previous[1], timestamp=now)
        self._prev[drive.name] = (counters, now)

        read_bps = sample.read_bps if sample else 0.0
        write_bps = sample.write_bps if sample else 0.0
        if settled:
            status = unplug.evaluate(drive, counters.in_flight, dirty, writeback, None, 0)
            state, title, detail = status.state, status.title, status.detail
            if state == unplug.UNKNOWN:
                # The panel has no flush button, so point at the place that does.
                detail = "Linux can't show which waiting data belongs to this drive. Flush or eject it in DriveScope."
        else:
            state, title, detail = unplug.UNKNOWN, "Checking", "Reading the latest drive and mount details."
        return {
            "id": drive.name,
            "name": _clip(drive.display_name),
            "size": fmt.size(drive.size),
            "link": _link_text(drive),
            "volumes": _volumes_text(drive),
            "warnings": drive.link.warnings() if drive.link else [],
            "readBps": read_bps,
            "writeBps": write_bps,
            "read": fmt.rate(read_bps),
            "write": fmt.rate(write_bps),
            "busy": fmt.percent(sample.busy_pct if sample else 0.0),
            "state": state,
            "title": title,
            "detail": detail,
        }

    def tick(self) -> dict:
        now = self._clock()
        signature = self._read_signature()
        with self._lock:
            start_scan = not self._scanning and (
                signature != self._scanned_signature or now - self._last_scan >= RESCAN_S
            )
            if start_scan:
                self._scanning = True
                self._last_scan = now
        if start_scan:
            self._spawn(lambda: self._scan(signature))

        with self._lock:
            drives = list(self._drives)
            app, error = self._app, self._error
            ready = self._scanned_signature is not None
            settled = self._scanned_signature == signature

        counters = stats.parse_diskstats(self._read_diskstats())
        dirty, writeback = stats.parse_meminfo_pending(self._read_meminfo())
        entries = [
            self._describe(d, counters[d.name], now, dirty, writeback, settled)
            for d in drives if d.name in counters  # otherwise it was just unplugged
        ]
        present = {e["id"] for e in entries}
        self._prev = {k: v for k, v in self._prev.items() if k in present}

        states = {e["state"] for e in entries}
        return {
            "version": PROTOCOL_VERSION,
            "ready": ready,
            "error": error,
            "appAvailable": app,
            "state": next((s for s in _STATE_ORDER if s in states), "none"),
            "activity": _activity(sum(e["readBps"] for e in entries), sum(e["writeBps"] for e in entries)),
            "tooltip": _tooltip(entries),
            "drives": entries,
        }


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="drivescope-watch", description="Print live external drive status as JSON lines."
    )
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between lines (0.5 to 10)")
    args = parser.parse_args()
    interval = min(10.0, max(0.5, args.interval))

    watcher = Watcher()
    try:
        while True:
            print(json.dumps(watcher.tick(), ensure_ascii=False, separators=(",", ":")), flush=True)
            time.sleep(interval)
    except BrokenPipeError:
        # The shell went away. Stop interpreter shutdown from flushing to the closed pipe.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
