import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from drivescope.core import benchmarks, fio, health, safety, store, verdict

FIXTURES = Path(__file__).parent / "fixtures"
TEST_DIR = "/run/media/user/Backup/.drivescope-test-" + "a" * 32


def load(name):
    return json.loads((FIXTURES / name).read_text())


class FioCommandTests(unittest.TestCase):
    def job(self, **kw):
        base = dict(name="t", mode="read", block_size="1M", queue_depth=8, path="/mnt/a b/big.bin", runtime_s=5)
        base.update(kw)
        return fio.Job(**base)

    def test_write_job_cannot_be_timed(self):
        job = self.job(mode="write", path=f"{TEST_DIR}/d.bin", size_bytes=1024, time_based=True)
        with mock.patch.object(safety, "approve_write_path", return_value=job.path), self.assertRaises(ValueError):
            fio.write_command(job, "io_uring")

    def test_write_job_carries_its_data_limit(self):
        job = self.job(mode="write", path=f"{TEST_DIR}/d.bin", size_bytes=1024, io_limit_bytes=2048, time_based=False)
        with mock.patch.object(safety, "approve_write_path", return_value=job.path):
            command = fio.write_command(job, "io_uring")
        self.assertIn("--io_size=2048", command)
        self.assertNotIn("--time_based=1", command)

    def test_read_job_is_always_read_only(self):
        for mode in fio.READ_MODES:
            self.assertIn("--readonly", fio.read_command(self.job(mode=mode), "io_uring"))

    def test_read_command_refuses_write_modes(self):
        for mode in ("write", "randwrite", "rw", "randrw", "trim"):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                fio.read_command(self.job(mode=mode), "io_uring")

    def test_cache_is_bypassed(self):
        self.assertIn("--direct=1", fio.read_command(self.job(), "io_uring"))

    def test_write_command_refuses_raw_device(self):
        job = self.job(mode="write", path="/dev/sda", size_bytes=1024)
        with self.assertRaises(safety.UnsafeTarget):
            fio.write_command(job, "io_uring")

    def test_write_command_refuses_path_outside_test_dir(self):
        job = self.job(mode="write", path="/run/media/user/Backup/data.bin", size_bytes=1024)
        with self.assertRaises(safety.UnsafeTarget):
            fio.write_command(job, "io_uring")

    def test_write_command_refuses_read_modes_and_missing_size(self):
        with self.assertRaises(ValueError):
            fio.write_command(self.job(mode="read"), "io_uring")

    def test_colons_in_paths_are_escaped(self):
        command = fio.read_command(self.job(path="/mnt/a:b/big.bin"), "io_uring")
        self.assertIn("--filename=/mnt/a\\:b/big.bin", command)

    def test_engine_detection(self):
        with tempfile.NamedTemporaryFile("w", suffix="uring") as f:
            f.write("0")
            f.flush()
            self.assertEqual(fio.detect_engine(Path(f.name)), "io_uring")
        with tempfile.NamedTemporaryFile("w", suffix="uring") as f:
            f.write("2")
            f.flush()
            self.assertEqual(fio.detect_engine(Path(f.name)), "libaio")
        self.assertEqual(fio.detect_engine(Path("/nonexistent")), "libaio")


class VerifyStateTests(unittest.TestCase):
    def test_integrity_jobs_leave_no_state_file_behind(self):
        job = fio.Job("integrity", "write", "1M", 8, "/m/.drivescope-test-x/f.bin", 60, time_based=False, size_bytes=1 << 30, verify=True)
        with mock.patch.object(safety, "approve_write_path", side_effect=lambda p: p):
            self.assertIn("--verify_state_save=0", fio.write_command(job, "libaio"))


class FioParseTests(unittest.TestCase):
    def setUp(self):
        self.text = (FIXTURES / "fio_seq_read.json").read_text()
        self.docs = fio.StreamParser().feed(self.text)

    def test_stream_splits_every_status_document(self):
        self.assertEqual(len(self.docs), 5)

    def test_stream_handles_partial_chunks_and_noise(self):
        parser = fio.StreamParser()
        got = []
        got += parser.feed("fio: some warning\n")
        for i in range(0, len(self.text), 700):
            got += parser.feed(self.text[i : i + 700])
        self.assertEqual(len(got), 5)

    def test_result_from_real_output(self):
        r = fio.parse_result(self.docs[-1], "read")
        self.assertEqual(r.error, 0)
        self.assertAlmostEqual(r.bps / 1e6, 723, delta=1)
        self.assertGreater(r.p999_ms, r.p50_ms)
        self.assertAlmostEqual(r.p50_ms, 8.98, delta=0.01)

    def test_intervals_show_true_speed_not_running_average(self):
        tracker = fio.IntervalTracker("read")
        points = [tracker.update(doc) for doc in self.docs]
        # Running averages said 502, 647 and 718 MB/s. The real speed was steady.
        self.assertAlmostEqual(points[2][1] / 1e6, 922, delta=15)
        self.assertAlmostEqual(points[3][1] / 1e6, 922, delta=15)
        self.assertLess(points[0][1] / 1e6, 40)  # the slow wake-up in the first second

    def test_tracker_ignores_repeated_report(self):
        tracker = fio.IntervalTracker("read")
        tracker.update(self.docs[0])
        self.assertIsNone(tracker.update(self.docs[0]))


class HealthTests(unittest.TestCase):
    def test_real_report_from_this_bridge(self):
        doc = load("smart_sntasmedia.json")
        self.assertTrue(health.is_believable(doc))
        r = health.parse_report(doc, "sntasmedia")
        self.assertEqual(r.model, "Example NVMe 1TB")
        self.assertEqual(r.temperature_c, 32)
        self.assertEqual(r.wear_pct, 0)
        self.assertEqual(r.unsafe_shutdowns, 112)
        self.assertEqual(r.media_errors, 0)
        self.assertEqual(r.power_cycles, 1104)
        self.assertEqual(r.data_written_bytes, 714748 * 512_000)
        self.assertTrue(r.passed)

    def test_bridge_that_succeeds_with_zeroes_is_rejected(self):
        self.assertFalse(health.is_believable(load("smart_sntrealtek.json")))

    def test_failed_and_scsi_only_reports_are_rejected(self):
        self.assertFalse(health.is_believable(load("smart_sat.json")))
        self.assertFalse(health.is_believable(load("smart_scsi.json")))

    def test_choose_skips_the_bad_ones_whatever_the_order(self):
        docs = [
            ("sntrealtek", load("smart_sntrealtek.json")),
            ("sat", load("smart_sat.json")),
            ("sntasmedia", load("smart_sntasmedia.json")),
        ]
        self.assertEqual(health.choose_report(docs).bridge_type, "sntasmedia")
        self.assertIsNone(health.choose_report(docs[:2]))

    def test_probe_output_split(self):
        text = '---DRIVESCOPE-TYPE:sat\n{"a": 1}\n---DRIVESCOPE-TYPE:scsi\nnot json\n---DRIVESCOPE-TYPE:auto\n{"b": 2}\n'
        self.assertEqual(health.split_probe_output(text), [("sat", {"a": 1}), ("auto", {"b": 2})])

    def test_device_path_is_validated_before_going_to_root(self):
        for bad in ("/dev/sda; rm -rf /", "sda", "/dev/../etc/passwd", "/dev/sda b"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                health.probe(bad)

    def test_bridge_type_is_validated(self):
        with self.assertRaises(ValueError):
            health.read_report("/dev/sda", "--smart=off")

    def test_kernel_log_keeps_problems_and_ignores_routine_lines(self):
        lines = [
            "2026-11-03T11:08:24-06:00 host kernel: usb 6-1: new SuperSpeed Plus Gen 2x1 USB device number 2 using xhci_hcd",
            "2026-11-03T11:08:25-06:00 host kernel: sd 0:0:0:0: [sda] Attached SCSI disk",
            "2026-11-03T11:09:00-06:00 host kernel: usb 6-1: reset SuperSpeed Plus Gen 2x1 USB device number 2 using xhci_hcd",
            "2026-11-03T11:10:00-06:00 host kernel: blk_update_request: I/O error, dev sda, sector 2048",
            "2026-11-03T11:11:00-06:00 host kernel: usb 6-1: USB disconnect, device number 2",
        ]
        kinds = [e.kind for e in health.classify_kernel_log(lines)]
        self.assertEqual(kinds, ["reset", "error", "disconnect"])

    def test_counters_read_as_hex(self):
        with tempfile.TemporaryDirectory() as tmp:
            device = Path(tmp) / "sdz" / "device"
            device.mkdir(parents=True)
            (device / "ioerr_cnt").write_text("0xf\n")
            (device / "iotmo_cnt").write_text("0x0\n")
            c = health.read_counters("sdz", Path(tmp))
            self.assertEqual((c.rejected_or_failed, c.timeouts, c.requests), (15, 0, None))


class VerdictTests(unittest.TestCase):
    def kinds(self, **kw):
        base = dict(link_ceiling_bps=1_212_000_000, link_speed_mbps=10000, best_read_bps=None, best_write_bps=None)
        base.update(kw)
        return [f.kind for f in verdict.analyze(**base)]

    def test_this_drive_is_link_limited(self):
        self.assertIn("link", self.kinds(best_read_bps=920e6))

    def test_slow_drive_on_fast_link_is_not_blamed_on_link(self):
        self.assertEqual(self.kinds(best_read_bps=300e6), ["inconclusive"])

    def test_no_data_is_inconclusive_not_a_guess(self):
        self.assertEqual(self.kinds(), ["inconclusive"])

    def test_queue_dependence(self):
        self.assertIn("queue", self.kinds(best_read_bps=920e6, q1_read_bps=500e6))
        self.assertNotIn("queue", self.kinds(best_read_bps=920e6, q1_read_bps=850e6))

    def test_cache_drop_found_with_position(self):
        curve = [(t, 800e6) for t in range(1, 41)] + [(t, 200e6) for t in range(41, 101)]
        drop = verdict.detect_cache_drop(curve)
        self.assertEqual(drop.at_seconds, 41)
        self.assertAlmostEqual(drop.at_bytes, 40 * 800e6, delta=1e6)
        self.assertEqual((drop.before_bps, drop.after_bps), (800e6, 200e6))

    def test_steady_or_noisy_curve_has_no_drop(self):
        self.assertIsNone(verdict.detect_cache_drop([(t, 500e6) for t in range(1, 100)]))
        noisy = [(t, 500e6 if t % 7 else 100e6) for t in range(1, 100)]
        self.assertIsNone(verdict.detect_cache_drop(noisy))

    def test_short_curve_is_not_judged(self):
        self.assertIsNone(verdict.detect_cache_drop([(1, 800e6), (2, 100e6)]))

    def test_source_limited_copy(self):
        self.assertIn("source", self.kinds(best_write_bps=800e6, copy_on_disk_bps=280e6, copy_source_bps=300e6))
        self.assertNotIn("source", self.kinds(best_write_bps=800e6, copy_on_disk_bps=100e6, copy_source_bps=300e6))

    def test_heat_requires_both_warming_and_a_slowdown(self):
        drop_curve = [(t, 800e6) for t in range(1, 41)] + [(t, 200e6) for t in range(41, 101)]
        steady = [(t, 800e6) for t in range(1, 101)]
        self.assertIn("heat", self.kinds(write_curve=drop_curve, temps_c=[35, 60]))
        self.assertNotIn("heat", self.kinds(write_curve=steady, temps_c=[35, 60]))
        self.assertNotIn("heat", self.kinds(write_curve=drop_curve, temps_c=[35, 38]))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.s = store.Store(":memory:")
        self.addCleanup(self.s.db.close)

    def result(self, bps, test_id="seq-read-1m-q8", label="Sequential read"):
        return store.Result(test_id, label, bps, bps, 900.0, 8.9, 11.0, 12.0)

    def run_with(self, bps, link=10000, drive="d1", when=None):
        return self.s.add_run(drive, "Drive", link, "UAS", "Vol", "ext4", [self.result(bps)], started_at=when)

    def test_round_trip_newest_first(self):
        a = self.run_with(900e6, when=100)
        b = self.run_with(910e6, when=200)
        self.assertEqual([r.id for r in self.s.runs("d1")], [b, a])
        self.assertEqual(self.s.results(a)[0].bps, 900e6)

    def test_runs_are_per_drive(self):
        self.run_with(900e6, drive="d1")
        self.run_with(900e6, drive="d2")
        self.assertEqual(len(self.s.runs("d1")), 1)

    def test_slow_run_alerts_after_enough_history(self):
        for _ in range(3):
            self.run_with(900e6)
        alerts = self.s.slow_alerts(self.run_with(600e6))
        self.assertEqual(len(alerts), 1)
        self.assertAlmostEqual(alerts[0].percent_below, 33.3, delta=0.1)

    def test_no_alert_without_enough_history(self):
        self.run_with(900e6)
        self.assertEqual(self.s.slow_alerts(self.run_with(100e6)), [])

    def test_no_alert_within_20_percent(self):
        for _ in range(3):
            self.run_with(900e6)
        self.assertEqual(self.s.slow_alerts(self.run_with(800e6)), [])

    def test_slower_link_is_not_called_drive_degradation(self):
        for _ in range(3):
            self.run_with(900e6, link=10000)
        self.assertEqual(self.s.slow_alerts(self.run_with(400e6, link=5000)), [])

    def test_delete_removes_results(self):
        a = self.run_with(900e6)
        self.s.delete_run(a)
        self.assertEqual(self.s.results(a), [])

    def test_exports(self):
        a = self.run_with(900e6)
        rows = json.loads(self.s.export_json([a]))
        self.assertEqual(rows[0]["bytes_per_second"], 900e6)
        lines = self.s.export_csv([a]).splitlines()
        self.assertIn("bytes_per_second", lines[0])
        self.assertEqual(len(lines), 2)
        self.assertEqual(self.s.export_csv([]), "")

    def test_alerts_summarize_to_one_sentence_naming_the_worst(self):
        alerts = [store.SlowAlert("A", 700e6, 1000e6), store.SlowAlert("B", 500e6, 1000e6), store.SlowAlert("C", 750e6, 1000e6)]
        text = store.summarize_alerts(alerts)
        self.assertIn("B was 50% below", text)
        self.assertIn("2 other tests were", text)
        self.assertIn("1 other test was", store.summarize_alerts(alerts[:2]))
        self.assertNotIn("other", store.summarize_alerts(alerts[:1]))
        self.assertIsNone(store.summarize_alerts([]))

    def test_bridge_setting_round_trip(self):
        self.assertIsNone(self.s.smart_bridge("d1"))
        self.s.set_smart_bridge("d1", "sat")
        self.s.set_smart_bridge("d1", "sntasmedia")
        self.assertEqual(self.s.smart_bridge("d1"), "sntasmedia")


class SuitePlanningTests(unittest.TestCase):
    def test_free_space_margin(self):
        gb = 1000 ** 3
        self.assertEqual(benchmarks.max_write_bytes(500 * gb, 1000 * gb), 400 * gb)
        self.assertEqual(benchmarks.max_write_bytes(4 * gb, 1000 * gb), 0)
        # Small volumes still keep at least 5 GiB.
        self.assertEqual(benchmarks.max_write_bytes(10 * 1024 ** 3, 16 * 1024 ** 3), 5 * 1024 ** 3)

    def test_fat_file_limit_is_respected_in_the_plan(self):
        limit = benchmarks.max_file_bytes("vfat")
        self.assertLess(limit, 4 * 1024 ** 3)
        self.assertIsNone(benchmarks.max_file_bytes("ext4"))
        o = benchmarks.SuiteOptions(sustained_write=True)
        big = 10 ** 12
        self.assertEqual(benchmarks.planned_write_bytes(o, big, big, "vfat"), limit * 2)
        self.assertGreater(benchmarks.planned_write_bytes(o, big, big, "ext4"), limit * 2)

    def test_read_only_options_plan_no_writes(self):
        o = benchmarks.SuiteOptions()
        self.assertFalse(o.writes)
        self.assertEqual(benchmarks.planned_write_bytes(o, 10 ** 12, 10 ** 12), 0)

    def test_full_integrity_plans_all_allowed_space(self):
        o = benchmarks.SuiteOptions(integrity=True, integrity_full=True)
        gb = 1000 ** 3
        # Plus the test file every write suite creates.
        self.assertEqual(benchmarks.planned_write_bytes(o, 500 * gb, 1000 * gb), 400 * gb + benchmarks.STANDARD_FILE_BYTES)

    def test_every_write_option_counts_the_test_file(self):
        o = benchmarks.SuiteOptions(small_file_write=True)
        expected = benchmarks.STANDARD_FILE_BYTES + benchmarks.SMALL_WRITE_COUNT * benchmarks.SMALL_WRITE_BYTES
        self.assertEqual(benchmarks.planned_write_bytes(o, 10 ** 12, 10 ** 12), expected)

    def test_standard_writes_are_each_limited_to_the_file_size(self):
        o = benchmarks.SuiteOptions(standard_writes=True)
        expected = benchmarks.STANDARD_FILE_BYTES * (1 + len(benchmarks.STANDARD_WRITES))
        self.assertEqual(benchmarks.planned_write_bytes(o, 10 ** 12, 10 ** 12), expected)

    def test_write_suite_refuses_read_only_volume_before_touching_it(self):
        info = "80 22 8:2 / /tmp/ro-vol rw,nosuid shared:4 - hfsplus /dev/sda2 ro,umask=22\n"
        from drivescope.core import mounts

        parsed = mounts.parse_mountinfo(info)
        suite = benchmarks.Suite(
            "sda", "/tmp/ro-vol", benchmarks.SuiteOptions(standard_reads=False, sustained_read=False,
                                                         small_file_read=False, standard_writes=True),
            lambda *_: None, lambda *_: None, lambda _: None,
        )
        with mock.patch.object(safety, "read_mounts", return_value=parsed), \
                mock.patch.object(benchmarks.filetests, "survey_volume",
                                  return_value=benchmarks.filetests.VolumeFiles(None, 0, ())), \
                mock.patch.object(safety, "create_test_dir") as create:
            with self.assertRaises(safety.UnsafeTarget):
                suite.run()
            create.assert_not_called()


class FileTests(unittest.TestCase):
    def test_survey_finds_largest_and_small_and_skips_symlinks(self):
        from drivescope.core import filetests

        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "sub").mkdir()
            Path(tmp, "sub", "big.bin").write_bytes(b"x" * 100_000)
            Path(tmp, "small.txt").write_bytes(b"x" * 10)
            Path(tmp, "empty").write_bytes(b"")
            os.symlink("/etc/passwd", Path(tmp, "link"))
            found = filetests.survey_volume(tmp)
            self.assertEqual(found.largest_size, 100_000)
            self.assertEqual([os.path.basename(p) for p in found.small_files], ["small.txt"])


class CompareTests(unittest.TestCase):
    def test_compare_handles_tests_in_only_one_run(self):
        from drivescope.ui.history import compare_rows

        def r(test_id, bps):
            return store.Result(test_id, test_id.upper(), bps, None, None, None, None, None)

        rows = compare_rows([r("a", 1000e6), r("b", 500e6)], [r("a", 600e6), r("c", 300e6)])
        self.assertEqual(rows[0], ["A", "1.00 GB/s", "600 MB/s", "-40%"])
        self.assertEqual(rows[1], ["B", "500 MB/s", "–", "–"])
        self.assertEqual(rows[2], ["C", "–", "300 MB/s", "–"])


class ProbeRetryTests(unittest.TestCase):
    def probe(self, read_report):
        with mock.patch.object(health, "can_poll_silently", return_value=True), \
                mock.patch.object(health, "read_report", side_effect=read_report) as reader, \
                mock.patch.object(health.time, "sleep") as sleeper:
            return health.probe("/dev/sdz"), reader, sleeper

    def test_a_drive_waking_up_is_found_on_a_later_pass(self):
        calls = []

        def read_report(_device, bridge):
            calls.append(bridge)
            return "report" if bridge == "sntasmedia" and len(calls) > len(health.BRIDGE_TYPES) else None

        report, _reader, sleeper = self.probe(read_report)
        self.assertEqual(report, "report")
        self.assertEqual(sleeper.call_count, 1)

    def test_a_drive_with_no_health_data_gives_up_without_a_final_wait(self):
        report, reader, sleeper = self.probe(lambda _device, _bridge: None)
        self.assertIsNone(report)
        self.assertEqual(reader.call_count, health.PROBE_PASSES * len(health.BRIDGE_TYPES))
        self.assertEqual(sleeper.call_count, health.PROBE_PASSES - 1)


class TemperatureAxisTests(unittest.TestCase):
    def test_axis_is_round_and_contains_every_reading(self):
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from drivescope.ui.bench import temperature_axis

        self.assertEqual(temperature_axis([34, 39]), (30, 40))
        self.assertEqual(temperature_axis([40, 40]), (30, 50))
        self.assertEqual(temperature_axis([28, 61]), (20, 70))


class SmallFileLimitTests(unittest.TestCase):
    def write(self, count, max_seconds):
        from drivescope.core import filetests, safety

        with tempfile.TemporaryDirectory(dir=os.path.expanduser("~/.cache")) as mount:
            fd = os.open(mount, os.O_RDONLY | os.O_DIRECTORY)
            test_dir = safety.TestDir(os.path.dirname(mount), os.path.basename(mount), fd, os.fstat(fd).st_dev)
            try:
                with mock.patch.object(safety, "approve_write_path", side_effect=lambda p: p):
                    result = filetests.write_small_files(test_dir, count, 4096, lambda: False, max_seconds)
            finally:
                os.close(fd)
            return result, len(os.listdir(mount))

    def test_time_limit_ends_the_batch_but_keeps_the_minimum(self):
        from drivescope.core import filetests

        result, on_disk = self.write(filetests.MIN_SMALL_FILES + 50, 0)
        self.assertEqual(result.files, filetests.MIN_SMALL_FILES)
        self.assertEqual(on_disk, filetests.MIN_SMALL_FILES)
        self.assertEqual(result.total_bytes, filetests.MIN_SMALL_FILES * 4096)

    def test_no_limit_writes_everything(self):
        result, on_disk = self.write(120, None)
        self.assertEqual((result.files, on_disk), (120, 120))


if __name__ == "__main__":
    unittest.main()
