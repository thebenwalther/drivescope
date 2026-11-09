"""Build, run and parse fio jobs.

Read jobs always pass --readonly. Write jobs can only be built for a path that
safety.approve_write_path() accepts, so the check can't be skipped by a caller.
That path reaches the test directory through a descriptor fio inherits, so
nothing done to the directory's name during a run can redirect fio's writes.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import safety

READ_MODES = ("read", "randread")
WRITE_MODES = ("write", "randwrite")
PERCENTILES = "50:99:99.9"


class FioError(Exception):
    """fio failed. str(e) is shown to the user."""


class Cancelled(Exception):
    pass


@dataclass(frozen=True)
class Job:
    name: str
    mode: str
    block_size: str
    queue_depth: int
    path: str
    runtime_s: int
    ramp_s: int = 2
    time_based: bool = True
    size_bytes: int | None = None
    io_limit_bytes: int | None = None  # stop after this much, so wear stays bounded
    verify: bool = False


@dataclass
class FioResult:
    direction: str
    bps: float
    iops: float
    mean_ms: float
    p50_ms: float
    p99_ms: float
    p999_ms: float
    max_ms: float
    io_bytes: int
    runtime_ms: int
    error: int
    intervals: list[tuple[float, float]] = field(default_factory=list)  # (seconds, bytes/s)


def detect_engine(disabled_path: Path = Path("/proc/sys/kernel/io_uring_disabled")) -> str:
    try:
        return "libaio" if disabled_path.read_text().strip() != "0" else "io_uring"
    except OSError:
        return "libaio"


def _escape_filename(path: str) -> str:
    # fio treats ":" in --filename as a separator between files.
    return path.replace("\\", "\\\\").replace(":", "\\:")


def _common_args(job: Job, engine: str) -> list[str]:
    args = [
        "fio",
        f"--name={job.name}",
        f"--filename={_escape_filename(job.path)}",
        "--direct=1",  # bypass the page cache, so this measures the drive
        f"--rw={job.mode}",
        f"--bs={job.block_size}",
        f"--iodepth={job.queue_depth}",
        f"--ioengine={engine}",
        f"--runtime={job.runtime_s}",
        f"--percentile_list={PERCENTILES}",
        "--status-interval=1",
        "--output-format=json",
        "--allow_mounted_write=0",
    ]
    if job.ramp_s:
        # Excludes the wake-up delay a sleeping drive adds to its first read.
        args.append(f"--ramp_time={job.ramp_s}")
    if job.time_based:
        args.append("--time_based=1")
    if job.size_bytes:
        args.append(f"--size={job.size_bytes}")
    if job.io_limit_bytes:
        args.append(f"--io_size={job.io_limit_bytes}")
    return args


def read_command(job: Job, engine: str) -> list[str]:
    if job.mode not in READ_MODES:
        raise ValueError(f"Not a read mode: {job.mode}")
    return [*_common_args(job, engine), "--readonly"]


def write_command(job: Job, engine: str) -> list[str]:
    """Raises safety.UnsafeTarget unless the path is an approved test file."""
    if job.mode not in WRITE_MODES:
        raise ValueError(f"Not a write mode: {job.mode}")
    safety.approve_write_path(job.path)
    if not job.size_bytes:
        raise ValueError("A write job needs an explicit size")
    if job.time_based:
        # A timed job keeps rewriting until the clock runs out, so a fast drive
        # would take far more wear than the amount promised before the run.
        raise ValueError("A write job must be limited by size, not by time")
    args = [
        *_common_args(job, engine),
        "--refill_buffers=1",  # new random data each write, so compression can't flatter it
        "--randrepeat=0",
        "--end_fsync=1",
        "--fallocate=none",  # not every filesystem a USB drive uses supports it
    ]
    if job.verify:
        args += [
            "--verify=crc32c", "--do_verify=1", "--verify_fatal=1", "--verify_dump=0",
            "--verify_state_save=0",  # otherwise fio leaves a state file in the working folder
        ]
    return args


class StreamParser:
    """Pulls complete JSON documents out of fio's status stream."""

    def __init__(self) -> None:
        self._buffer = ""
        self._decoder = json.JSONDecoder()

    def feed(self, text: str) -> list[dict]:
        self._buffer += text
        docs = []
        while True:
            start = self._buffer.find("{")
            if start < 0:
                self._buffer = ""
                return docs
            try:
                doc, end = self._decoder.raw_decode(self._buffer, start)
            except json.JSONDecodeError:
                self._buffer = self._buffer[start:]
                return docs  # incomplete, wait for more
            docs.append(doc)
            self._buffer = self._buffer[end:]


def _percentile(clat: dict, key: str) -> float:
    return clat.get("percentile", {}).get(key, 0) / 1_000_000


def parse_result(doc: dict, direction: str) -> FioResult:
    job = doc["jobs"][0]
    side = job[direction]
    clat = side.get("clat_ns", {})
    return FioResult(
        direction=direction,
        bps=float(side.get("bw_bytes", 0)),
        iops=float(side.get("iops", 0)),
        mean_ms=clat.get("mean", 0) / 1_000_000,
        p50_ms=_percentile(clat, "50.000000"),
        p99_ms=_percentile(clat, "99.000000"),
        p999_ms=_percentile(clat, "99.900000"),
        max_ms=clat.get("max", 0) / 1_000_000,
        io_bytes=int(side.get("io_bytes", 0)),
        runtime_ms=int(side.get("runtime", 0)),
        error=int(job.get("error", 0)),
    )


class IntervalTracker:
    """fio's status figures are running totals, which hide changes in speed.
    The speed for each interval comes from the difference between reports."""

    def __init__(self, direction: str) -> None:
        self.direction = direction
        self._bytes = 0
        self._ms = 0
        self.points: list[tuple[float, float]] = []

    def update(self, doc: dict) -> tuple[float, float] | None:
        side = doc["jobs"][0][self.direction]
        total, ms = int(side.get("io_bytes", 0)), int(side.get("runtime", 0))
        if ms <= self._ms or total < self._bytes:
            return None
        point = (ms / 1000, (total - self._bytes) * 1000 / (ms - self._ms))
        self._bytes, self._ms = total, ms
        self.points.append(point)
        return point


def run(
    command: list[str],
    direction: str,
    on_interval: Callable[[float, float], None] | None = None,
    cancelled: Callable[[], bool] = lambda: False,
    accept_failure: bool = False,
) -> FioResult:
    """Run a job built by read_command() or write_command().

    With accept_failure, a job that fio itself reports as failed, such as a
    verify mismatch, returns its result so the caller can read the error code.
    """
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
        pass_fds=safety.open_test_dir_fds(),
    )
    stderr_lines: list[str] = []
    reader = threading.Thread(
        target=lambda: stderr_lines.extend(process.stderr.readlines()), daemon=True,
    )
    reader.start()

    watchdog_done = threading.Event()

    def watchdog() -> None:
        while not watchdog_done.wait(0.2):
            if cancelled():
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                return

    threading.Thread(target=watchdog, daemon=True).start()

    parser = StreamParser()
    tracker = IntervalTracker(direction)
    last_doc = None
    try:
        # read() would wait for the whole run. fio flushes one document at a time.
        for line in process.stdout:
            for doc in parser.feed(line):
                last_doc = doc
                point = tracker.update(doc)
                if point and on_interval:
                    on_interval(*point)
        code = process.wait()
    finally:
        watchdog_done.set()
    reader.join(timeout=2)
    process.stdout.close()
    process.stderr.close()

    if cancelled():
        raise Cancelled()
    if last_doc is None or (code != 0 and not accept_failure):
        detail = "".join(stderr_lines).strip() or f"exit code {code}"
        raise FioError(f"fio failed: {detail[:500]}")
    result = parse_result(last_doc, direction)
    result.intervals = tracker.points
    return result
