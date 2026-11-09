"""The benchmark suite: which tests run, and in what order, for a given volume."""

from __future__ import annotations

import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from . import filetests, fio, mounts, safety, scan, stats
from .fio import Cancelled, Job
from .store import Result

GIB = 1024 ** 3
STANDARD_RUNTIME_S = 8
SUSTAINED_READ_MAX_S = 60
SUSTAINED_WRITE_MAX_S = 300
SUSTAINED_WRITE_MAX_BYTES = 100 * GIB
STANDARD_FILE_BYTES = 4 * GIB
COPY_BYTES = 4 * GIB
SMALL_WRITE_COUNT = 5000
SMALL_WRITE_BYTES = 32 * 1024
SMALL_WRITE_MAX_S = 60
INTEGRITY_BYTES = 4 * GIB
SCAN_POINTS = 200
MIN_DEVICE_SAMPLES = 4
MIN_FREE_MARGIN = 5 * GIB
TEMPERATURE_POLL_S = 10
# FAT can't hold a file of 4 GiB or more. Everything else USB drives commonly use can.
_FILE_LIMITS = {"vfat": 4 * GIB - 1024 * 1024, "msdos": 4 * GIB - 1024 * 1024}


def max_file_bytes(fstype: str | None) -> int | None:
    return _FILE_LIMITS.get(fstype or "")


def _capped(wanted: int, limit: int, fstype: str | None) -> int:
    return min(wanted, limit, max_file_bytes(fstype) or wanted)


def free_margin(total: int) -> int:
    """Space that write tests always leave free on a volume."""
    return max(total // 10, MIN_FREE_MARGIN)


def max_write_bytes(free: int, total: int) -> int:
    """Leave at least 10% of the volume, and at least 5 GiB, free."""
    return max(0, free - max(total // 10, MIN_FREE_MARGIN))


@dataclass(frozen=True)
class StandardTest:
    test_id: str
    label: str
    mode: str
    block_size: str
    queue_depth: int


STANDARD_READS = (
    StandardTest("seq-read-1m-q8", "Sequential read, 1M blocks, queue 8", "read", "1M", 8),
    StandardTest("seq-read-1m-q1", "Sequential read, 1M blocks, one at a time", "read", "1M", 1),
    StandardTest("seq-read-128k-q32", "Sequential read, 128K blocks, queue 32", "read", "128k", 32),
    StandardTest("rand-read-4k-q32", "Random read, 4K blocks, queue 32", "randread", "4k", 32),
    StandardTest("rand-read-4k-q1", "Random read, 4K blocks, one at a time", "randread", "4k", 1),
)
STANDARD_WRITES = (
    StandardTest("seq-write-1m-q8", "Sequential write, 1M blocks, queue 8", "write", "1M", 8),
    StandardTest("seq-write-1m-q1", "Sequential write, 1M blocks, one at a time", "write", "1M", 1),
    StandardTest("seq-write-128k-q32", "Sequential write, 128K blocks, queue 32", "write", "128k", 32),
    StandardTest("rand-write-4k-q32", "Random write, 4K blocks, queue 32", "randwrite", "4k", 32),
    StandardTest("rand-write-4k-q1", "Random write, 4K blocks, one at a time", "randwrite", "4k", 1),
)


@dataclass(frozen=True)
class SuiteOptions:
    standard_reads: bool = True
    sustained_read: bool = True
    small_file_read: bool = True
    drive_scan: bool = False
    standard_writes: bool = False
    sustained_write: bool = False
    small_file_write: bool = False
    real_copy: bool = False
    integrity: bool = False
    integrity_full: bool = False

    @property
    def writes(self) -> bool:
        return (self.standard_writes or self.sustained_write or self.small_file_write
                or self.real_copy or self.integrity)


def planned_write_bytes(options: SuiteOptions, free: int, total: int, fstype: str | None = None) -> int:
    """The most this suite can write, for the confirmation shown beforehand.
    Every write job is limited by size, so this is a real limit, not an estimate."""
    limit = max_write_bytes(free, total)
    planned = 0
    if options.writes:
        planned += _capped(STANDARD_FILE_BYTES, limit, fstype)  # every write suite creates this file
    if options.standard_writes:
        planned += _capped(STANDARD_FILE_BYTES, limit, fstype) * len(STANDARD_WRITES)
    if options.sustained_write:
        planned += _capped(SUSTAINED_WRITE_MAX_BYTES, limit, fstype)
    if options.small_file_write:
        planned += SMALL_WRITE_COUNT * SMALL_WRITE_BYTES
    if options.real_copy:
        planned += _capped(COPY_BYTES, limit, fstype)
    if options.integrity:
        planned += _capped(limit if options.integrity_full else INTEGRITY_BYTES, limit, fstype)
    return planned


@dataclass
class SuiteOutcome:
    results: list[Result] = field(default_factory=list)
    write_curve: list[tuple[float, float]] = field(default_factory=list)
    read_curve: list[tuple[float, float]] = field(default_factory=list)
    scan_points: list[scan.ScanPoint] = field(default_factory=list)
    copy: filetests.CopyResult | None = None
    integrity_passed: bool | None = None
    integrity_bytes: int = 0
    temperatures: list[int] = field(default_factory=list)
    temperature_curve: list[tuple[float, int]] = field(default_factory=list)  # (seconds, °C)
    skipped: list[str] = field(default_factory=list)
    device_sectors_written: int | None = None


class Suite:
    """Runs on a worker thread. Callbacks arrive on that thread as well."""

    def __init__(
        self,
        device_name: str,
        mountpoint: str | None,
        options: SuiteOptions,
        on_stage: Callable[[str, int, int], None],
        on_point: Callable[[float, float], None],
        on_result: Callable[[Result], None],
        on_scan_point: Callable[[scan.ScanPoint], None] | None = None,
        read_temperature: Callable[[], int | None] | None = None,
        scan_fd: int | None = None,
    ) -> None:
        self.device = device_name
        self.mountpoint = mountpoint
        self.options = options
        self.on_stage = on_stage
        self.on_point = on_point
        self.on_result = on_result
        self.on_scan_point = on_scan_point
        self.read_temperature = read_temperature
        self.scan_fd = scan_fd
        self.outcome = SuiteOutcome()
        self._cancel = threading.Event()
        self._engine = fio.detect_engine()
        self._stage = 0
        self._stages = 0
        self._file_bytes = STANDARD_FILE_BYTES
        self._fstype: str | None = None

    def cancel(self) -> None:
        self._cancel.set()

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    # --- stages ---

    def _begin(self, title: str) -> None:
        if self.cancelled():
            raise Cancelled()
        self._stage += 1
        self.on_stage(title, self._stage, self._stages)

    def _run_fio(self, test: StandardTest, command: list[str], direction: str, ramp_s: int = 2) -> fio.FioResult:
        """Run a job while sampling the device, so its result can be cross-checked.

        Too short a run has too few samples to compare, so it reports None rather
        than a misleading figure.
        """
        sampler = stats.Sampler(self.device)
        samples: list[stats.Sample] = []
        stop = threading.Event()

        def sample_loop() -> None:
            sampler.tick()
            while not stop.wait(0.5):
                sample = sampler.tick()
                if sample:
                    samples.append(sample)

        thread = threading.Thread(target=sample_loop, daemon=True)
        thread.start()
        try:
            result = fio.run(command, direction, self.on_point, self.cancelled)
        finally:
            stop.set()
            thread.join(timeout=2)
        # The opening samples are the ramp, which fio's own figure leaves out too.
        settled = samples[ramp_s * 2 :]
        attr = "read_bps" if direction == "read" else "write_bps"
        device_bps = sum(getattr(s, attr) for s in settled) / len(settled) if len(settled) >= MIN_DEVICE_SAMPLES else None
        self.on_result(self._record(test.test_id, test.label, result, device_bps))
        return result

    def _record(self, test_id, label, result: fio.FioResult, device_bps) -> Result:
        record = Result(
            test_id, label, result.bps, device_bps, result.iops,
            result.p50_ms, result.p99_ms, result.p999_ms,
        )
        self.outcome.results.append(record)
        return record

    def _add_simple(self, test_id, label, bps, detail="") -> None:
        record = Result(test_id, label, bps, None, None, None, None, None, detail)
        self.outcome.results.append(record)
        self.on_result(record)

    def _start_temperature_polling(self) -> threading.Event:
        """Sample throughout the run, so heat can be seen against speed."""
        stop = threading.Event()
        if not self.read_temperature:
            return stop

        def poll() -> None:
            started = time.monotonic()
            while True:
                temperature = self.read_temperature()
                if temperature is not None:
                    self.outcome.temperatures.append(temperature)
                    self.outcome.temperature_curve.append((time.monotonic() - started, temperature))
                if stop.wait(TEMPERATURE_POLL_S):
                    return

        threading.Thread(target=poll, daemon=True).start()
        return stop

    def _count_stages(self, read_file: bool, files: filetests.VolumeFiles | None) -> int:
        o = self.options
        total = 1 if self.mountpoint else 0  # survey
        if read_file and o.standard_reads:
            total += len(STANDARD_READS)
        if read_file and o.sustained_read:
            total += 1
        if files and len(files.small_files) >= filetests.MIN_SMALL_FILES and o.small_file_read:
            total += 1
        if o.drive_scan:
            total += 1
        if o.writes:
            total += 1  # creating the test file
            total += len(STANDARD_WRITES) if o.standard_writes else 0
            total += sum((o.sustained_write, o.small_file_write, o.real_copy, o.integrity))
        return total

    # --- the run ---

    def run(self) -> SuiteOutcome:
        o = self.options
        test_dir = None
        start_written = stats.total_sectors_written(self.device)
        stop_polling = self._start_temperature_polling()
        if self.mountpoint:
            mount = mounts.mount_at(self.mountpoint, mounts.read_mounts())
            self._fstype = mount.fstype if mount else None
        try:
            files = None
            read_path = None
            if self.mountpoint:
                self._stages = 1
                self._begin("Looking for files to read")
                files = filetests.survey_volume(self.mountpoint, self.cancelled)
                if files.largest_size >= filetests.MIN_BIG_FILE:
                    read_path = files.largest_path

            if o.writes:
                reason = safety.write_block_reason(self.mountpoint)
                if reason:
                    raise safety.UnsafeTarget(reason)
                test_dir = safety.create_test_dir(self.mountpoint)
                # Fresh random data, so reads can't be flattered by compression
                # or by a drive treating never-written blocks as empty.
                read_path = test_dir.file("bench.bin")

            self._stages = self._stage + self._count_stages(read_path is not None, files) - (1 if self.mountpoint else 0)

            if test_dir:
                self._prepare_test_file(read_path)
                if o.standard_writes:
                    self._standard_writes(read_path)

            if read_path:
                if o.standard_reads:
                    self._standard_reads(read_path)
                if o.sustained_read:
                    self._sustained_read(read_path)
            elif o.standard_reads or o.sustained_read:
                self.outcome.skipped.append(
                    "Speed tests need a file of at least 1 GB, and this volume doesn't have one."
                )

            if test_dir and os.path.exists(read_path):
                # Reads are finished with it, and small drives need the room.
                os.unlink(read_path)

            if files and o.small_file_read:
                if len(files.small_files) >= filetests.MIN_SMALL_FILES:
                    self._small_read(files.small_files)
                else:
                    self.outcome.skipped.append(
                        f"Small-file test skipped: it needs at least {filetests.MIN_SMALL_FILES} "
                        f"small files, and this volume has {len(files.small_files)}."
                    )

            if test_dir:
                if o.small_file_write:
                    self._small_write(test_dir)
                if o.real_copy:
                    self._copy(test_dir)
                if o.sustained_write:
                    self._sustained_write(test_dir)
                if o.integrity:
                    self._integrity(test_dir)

            if o.drive_scan and self.scan_fd is not None:
                self._scan()
            self._add_temperature_summary()
        finally:
            stop_polling.set()
            if self.scan_fd is not None:
                os.close(self.scan_fd)
            if test_dir:
                try:
                    safety.remove_test_dir(test_dir)
                except OSError as e:
                    # Usually the drive was unplugged. It's removed on the next launch.
                    self.outcome.skipped.append(f"Couldn't remove the test files yet: {e.strerror}.")
        end_written = stats.total_sectors_written(self.device)
        if start_written is not None and end_written is not None:
            self.outcome.device_sectors_written = end_written - start_written
        return self.outcome

    def _add_temperature_summary(self) -> None:
        temps = self.outcome.temperatures
        if temps:
            self._add_simple(
                "temperature", "Temperature during this run", None,
                f"{temps[0]}°C at the start, {temps[-1]}°C at the end, peaking at {max(temps)}°C.",
            )

    def _size_for(self, wanted: int) -> int:
        usage = shutil.disk_usage(self.mountpoint)
        return _capped(wanted, max_write_bytes(usage.free, usage.total), self._fstype)

    def _prepare_test_file(self, path: str) -> None:
        self._file_bytes = self._size_for(STANDARD_FILE_BYTES)
        if self._file_bytes < GIB:
            raise safety.UnsafeTarget("There isn't enough free space on this volume for write tests.")
        self._begin("Creating a test file")
        job = Job("prepare", "write", "1M", 8, path, runtime_s=600, ramp_s=0, time_based=False,
                  size_bytes=self._file_bytes)
        fio.run(fio.write_command(job, self._engine), "write", self.on_point, self.cancelled)

    def _standard_writes(self, path: str) -> None:
        for test in STANDARD_WRITES:
            self._begin(test.label)
            # Ends at the time limit or the data limit, whichever comes first. The
            # drive is already awake from creating the file, so no ramp is needed.
            job = Job(test.test_id, test.mode, test.block_size, test.queue_depth, path,
                      runtime_s=STANDARD_RUNTIME_S, ramp_s=0, time_based=False,
                      size_bytes=self._file_bytes, io_limit_bytes=self._file_bytes)
            self._run_fio(test, fio.write_command(job, self._engine), "write", ramp_s=0)

    def _standard_reads(self, path: str) -> None:
        for test in STANDARD_READS:
            self._begin(test.label)
            job = Job(test.test_id, test.mode, test.block_size, test.queue_depth, path,
                      runtime_s=STANDARD_RUNTIME_S)
            self._run_fio(test, fio.read_command(job, self._engine), "read")

    def _sustained_read(self, path: str) -> None:
        test = StandardTest("sustained-read", "Sustained read", "read", "1M", 8)
        self._begin(test.label)
        job = Job(test.test_id, "read", "1M", 8, path, runtime_s=SUSTAINED_READ_MAX_S, time_based=False)
        result = self._run_fio(test, fio.read_command(job, self._engine), "read")
        self.outcome.read_curve = result.intervals

    def _sustained_write(self, test_dir: safety.TestDir) -> None:
        test = StandardTest("sustained-write", "Sustained write", "write", "1M", 8)
        self._begin(test.label)
        size = self._size_for(SUSTAINED_WRITE_MAX_BYTES)
        if size < GIB:
            self.outcome.skipped.append("Sustained write skipped: not enough free space.")
            return
        path = test_dir.file("sustained.bin")
        job = Job(test.test_id, "write", "1M", 8, path, runtime_s=SUSTAINED_WRITE_MAX_S,
                  ramp_s=0, time_based=False, size_bytes=size)
        result = self._run_fio(test, fio.write_command(job, self._engine), "write", ramp_s=0)
        self.outcome.write_curve = result.intervals
        os.unlink(path)

    def _small_read(self, paths: tuple[str, ...]) -> None:
        self._begin(f"Reading {len(paths):,} small files")
        result = filetests.read_small_files(paths, self.cancelled)
        self._add_simple(
            "small-read", "Small files, read", result.bps,
            f"{result.files:,} files, {result.files_per_s:,.0f} files/s",
        )

    def _has_room(self, needed: int, what: str) -> bool:
        usage = shutil.disk_usage(self.mountpoint)
        if max_write_bytes(usage.free, usage.total) < needed:
            self.outcome.skipped.append(f"{what} skipped: not enough free space.")
            return False
        return True

    def _small_write(self, test_dir: safety.TestDir) -> None:
        if not self._has_room(SMALL_WRITE_COUNT * SMALL_WRITE_BYTES, "Small-file write"):
            return
        self._begin(f"Writing up to {SMALL_WRITE_COUNT:,} small files")
        result = filetests.write_small_files(
            test_dir, SMALL_WRITE_COUNT, SMALL_WRITE_BYTES, self.cancelled, SMALL_WRITE_MAX_S,
        )
        detail = f"{result.files:,} files, {result.files_per_s:,.0f} files/s before flushing"
        if result.files < SMALL_WRITE_COUNT:
            detail += f". Stopped at the {SMALL_WRITE_MAX_S} second limit."
        self._add_simple("small-write", "Small files, write", result.bps, detail)

    def _copy(self, test_dir: safety.TestDir) -> None:
        size = self._size_for(COPY_BYTES)
        if size < GIB:
            self.outcome.skipped.append("Real copy skipped: not enough free space.")
            return
        self._begin("Copying a real file from the internal drive")
        result = filetests.real_copy(test_dir, size, self.cancelled, on_progress=self.on_point)
        self.outcome.copy = result
        self._add_simple(
            "real-copy", "Real copy from internal drive", result.on_disk_bps,
            f"Looked like {result.apparent_bps / 1e6:.0f} MB/s before the final flush. "
            f"Source reads at {result.source_bps / 1e6:.0f} MB/s.",
        )
        os.unlink(test_dir.file("copy-target.bin"))

    def _integrity(self, test_dir: safety.TestDir) -> None:
        full = self.options.integrity_full
        self._begin("Writing and verifying all free space" if full else "Writing and verifying data")
        usage = shutil.disk_usage(self.mountpoint)
        limit = max_write_bytes(usage.free, usage.total)
        size = self._size_for(limit if full else INTEGRITY_BYTES)
        if full and size < limit:
            self.outcome.skipped.append(
                f"This filesystem can't hold one file that large, so only {size / 1e9:.1f} GB of "
                f"{limit / 1e9:.1f} GB free space was checked. It can't rule out a fake-capacity drive."
            )
        if size < GIB:
            self.outcome.skipped.append("Integrity check skipped: not enough free space.")
            return
        path = test_dir.file("integrity.bin")
        # Long enough for a full pass over a large volume at USB 2 speed.
        job = Job("integrity", "write", "1M", 8, path, runtime_s=7 * 24 * 3600, ramp_s=0,
                  time_based=False, size_bytes=size, verify=True)
        result = fio.run(fio.write_command(job, self._engine), "write", self.on_point,
                         self.cancelled, accept_failure=True)
        self.outcome.integrity_passed = result.error == 0
        self.outcome.integrity_bytes = size
        verdict = "Every block read back correctly" if result.error == 0 else f"Verification failed (fio error {result.error})"
        self._add_simple("integrity", "Integrity check", None, f"{verdict}, {size / 1e9:.1f} GB checked.")
        os.unlink(path)

    def _scan(self) -> None:
        self._begin("Reading across the whole drive")
        size = os.lseek(self.scan_fd, 0, os.SEEK_END)

        def on_point(point: scan.ScanPoint) -> None:
            self.outcome.scan_points.append(point)
            if self.on_scan_point:
                self.on_scan_point(point)

        scan.scan(self.scan_fd, scan.sample_offsets(size, SCAN_POINTS), on_point, self.cancelled)
        if self.cancelled():
            raise Cancelled()
        if self.outcome.scan_points:
            speeds = sorted(p.bps for p in self.outcome.scan_points)
            median_bps = speeds[len(speeds) // 2]
            self._add_simple(
                "drive-scan", "Whole-drive scan", median_bps,
                f"Slowest point {speeds[0] / 1e6:.0f} MB/s across {len(speeds)} points.",
            )


def timestamp() -> float:
    return time.time()
