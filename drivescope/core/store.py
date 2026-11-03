"""Benchmark history in SQLite, plus CSV and JSON export."""

from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from statistics import median

SLOW_RATIO = 0.80  # alert when more than 20% under this drive's median
MIN_RUNS_FOR_ALERT = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    drive_id TEXT NOT NULL,
    drive_name TEXT NOT NULL,
    started_at REAL NOT NULL,
    link_mbps INTEGER,
    protocol TEXT,
    volume TEXT,
    fstype TEXT
);
CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    test_id TEXT NOT NULL,
    label TEXT NOT NULL,
    bps REAL,
    device_bps REAL,
    iops REAL,
    p50_ms REAL,
    p99_ms REAL,
    p999_ms REAL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS drive_settings (
    drive_id TEXT PRIMARY KEY,
    smart_bridge TEXT
);
CREATE INDEX IF NOT EXISTS results_by_test ON results(test_id);
CREATE INDEX IF NOT EXISTS runs_by_drive ON runs(drive_id, started_at);
"""


def default_path() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "drivescope" / "history.sqlite3"


@dataclass(frozen=True)
class Run:
    id: int
    drive_id: str
    drive_name: str
    started_at: float
    link_mbps: int | None
    protocol: str | None
    volume: str | None
    fstype: str | None


@dataclass(frozen=True)
class Result:
    test_id: str
    label: str
    bps: float | None
    device_bps: float | None
    iops: float | None
    p50_ms: float | None
    p99_ms: float | None
    p999_ms: float | None
    detail: str = ""


@dataclass(frozen=True)
class SlowAlert:
    label: str
    bps: float
    median_bps: float

    @property
    def percent_below(self) -> float:
        return (1 - self.bps / self.median_bps) * 100


def summarize_alerts(alerts: list[SlowAlert]) -> str | None:
    """One readable sentence, however many tests were slow. Names the worst one."""
    if not alerts:
        return None
    worst = max(alerts, key=lambda a: a.percent_below)
    text = (
        f"{worst.label} was {worst.percent_below:.0f}% below this drive's usual speed at this link speed"
    )
    others = len(alerts) - 1
    if others:
        text += f", and {others} other test{'s were' if others > 1 else ' was'} also slower than usual"
    return text + "."


class Store:
    def __init__(self, path: Path | str | None = None) -> None:
        path = default_path() if path is None else path
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # Benchmarks finish on a worker thread and the UI reads on the main one.
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(_SCHEMA)
        self.db.commit()

    def add_run(self, drive_id, drive_name, link_mbps, protocol, volume, fstype, results: list[Result], started_at=None) -> int:
        with self.db:
            run_id = self.db.execute(
                "INSERT INTO runs (drive_id, drive_name, started_at, link_mbps, protocol, volume, fstype) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (drive_id, drive_name, time.time() if started_at is None else started_at,
                 link_mbps, protocol, volume, fstype),
            ).lastrowid
            self.db.executemany(
                "INSERT INTO results (run_id, test_id, label, bps, device_bps, iops, p50_ms, p99_ms, p999_ms, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(run_id, r.test_id, r.label, r.bps, r.device_bps, r.iops, r.p50_ms, r.p99_ms, r.p999_ms, r.detail)
                 for r in results],
            )
        return run_id

    def runs(self, drive_id: str | None = None) -> list[Run]:
        where, args = ("WHERE drive_id = ?", (drive_id,)) if drive_id else ("", ())
        rows = self.db.execute(
            "SELECT id, drive_id, drive_name, started_at, link_mbps, protocol, volume, fstype "
            f"FROM runs {where} ORDER BY started_at DESC, id DESC", args,
        ).fetchall()
        return [Run(*row) for row in rows]

    def results(self, run_id: int) -> list[Result]:
        rows = self.db.execute(
            "SELECT test_id, label, bps, device_bps, iops, p50_ms, p99_ms, p999_ms, detail "
            "FROM results WHERE run_id = ? ORDER BY id", (run_id,),
        ).fetchall()
        return [Result(*row) for row in rows]

    def delete_run(self, run_id: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM runs WHERE id = ?", (run_id,))

    def slow_alerts(self, run_id: int) -> list[SlowAlert]:
        """Results over 20% under this drive's median at the same link speed.

        Results at another link speed are left out, so a slower cable shows up as
        a link warning rather than as the drive getting worse.
        """
        run = self.db.execute("SELECT drive_id, link_mbps FROM runs WHERE id = ?", (run_id,)).fetchone()
        if run is None:
            return []
        alerts = []
        for result in self.results(run_id):
            if result.bps is None:
                continue
            earlier = [row[0] for row in self.db.execute(
                "SELECT r.bps FROM results r JOIN runs u ON u.id = r.run_id "
                "WHERE u.drive_id = ? AND u.link_mbps IS ? AND r.test_id = ? AND u.id != ? AND r.bps IS NOT NULL",
                (run[0], run[1], result.test_id, run_id),
            )]
            if len(earlier) < MIN_RUNS_FOR_ALERT:
                continue
            typical = median(earlier)
            if typical > 0 and result.bps < typical * SLOW_RATIO:
                alerts.append(SlowAlert(result.label, result.bps, typical))
        return alerts

    def smart_bridge(self, drive_id: str) -> str | None:
        row = self.db.execute("SELECT smart_bridge FROM drive_settings WHERE drive_id = ?", (drive_id,)).fetchone()
        return row[0] if row else None

    def set_smart_bridge(self, drive_id: str, bridge: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO drive_settings (drive_id, smart_bridge) VALUES (?, ?) "
                "ON CONFLICT(drive_id) DO UPDATE SET smart_bridge = excluded.smart_bridge",
                (drive_id, bridge),
            )

    def _export_rows(self, run_ids: list[int]) -> list[dict]:
        rows = []
        for run in [r for r in self.runs() if r.id in run_ids]:
            for result in self.results(run.id):
                rows.append({
                    "run_id": run.id,
                    "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(run.started_at)),
                    "drive": run.drive_name,
                    "drive_id": run.drive_id,
                    "link_mbps": run.link_mbps,
                    "protocol": run.protocol,
                    "volume": run.volume,
                    "filesystem": run.fstype,
                    "test_id": result.test_id,
                    "test": result.label,
                    "bytes_per_second": result.bps,
                    "device_bytes_per_second": result.device_bps,
                    "iops": result.iops,
                    "latency_p50_ms": result.p50_ms,
                    "latency_p99_ms": result.p99_ms,
                    "latency_p999_ms": result.p999_ms,
                    "detail": result.detail,
                })
        return rows

    def export_json(self, run_ids: list[int]) -> str:
        return json.dumps(self._export_rows(run_ids), indent=2)

    def export_csv(self, run_ids: list[int]) -> str:
        rows = self._export_rows(run_ids)
        if not rows:
            return ""
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
        return out.getvalue()
