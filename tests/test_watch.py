import json
import os
import unittest

from drivescope.core.devices import Drive, Partition, UsbLink
from drivescope.watch import Watcher


def partition(mountpoint="/run/media/user/Backup", read_only=False, label="Backup"):
    return Partition("sdb1", "/dev/sdb1", 1000, "ext4", label, mountpoint, read_only, 500)


def drive(*partitions, name="sdb", link=True):
    usb = UsbLink(10000, " 3.20", "Example", "External SSD", "ffff", "0001", "uas", "896mA", 10000, False) if link else None
    return Drive(name, f"/dev/{name}", "Example", "External SSD", "", 1_000_000_000_000, "usb",
                 False, True, tuple(partitions), usb)


def diskstats(name="sdb", read_sectors=0, write_sectors=0, in_flight=0, read_ios=0, write_ios=0, io_ticks=0):
    return f"8 16 {name} {read_ios} 0 {read_sectors} 0 {write_ios} 0 {write_sectors} 0 {in_flight} {io_ticks} 0\n"


def meminfo(dirty_kb=0, writeback_kb=0):
    return f"Dirty: {dirty_kb} kB\nWriteback: {writeback_kb} kB\n"


class Harness:
    """A Watcher with every outside dependency replaced, and scans run on request."""

    def __init__(self, drives):
        self.drives = drives
        self.stats = diskstats()
        self.mem = meminfo()
        self.signature = "a"
        self.now = 0.0
        self.scans = 0
        self.pending = []
        self.fail = None
        self.watcher = Watcher(
            list_drives=self._list,
            read_diskstats=lambda: self.stats,
            read_meminfo=lambda: self.mem,
            read_signature=lambda: self.signature,
            app_available=lambda: True,
            clock=lambda: self.now,
            spawn=self.pending.append,
        )

    def _list(self):
        self.scans += 1
        if self.fail:
            raise self.fail
        return self.drives

    def finish_scan(self):
        while self.pending:
            self.pending.pop(0)()

    def settled_tick(self):
        self.watcher.tick()
        self.finish_scan()
        return self.watcher.tick()


class StatusTests(unittest.TestCase):
    def test_not_ready_until_the_first_scan_finishes(self):
        h = Harness([drive()])
        first = h.watcher.tick()
        self.assertFalse(first["ready"])
        self.assertEqual(first["drives"], [])
        h.finish_scan()
        self.assertTrue(h.watcher.tick()["ready"])

    def test_unmounted_drive_is_safe(self):
        out = Harness([drive()]).settled_tick()
        self.assertEqual(out["state"], "safe")
        self.assertEqual(out["drives"][0]["title"], "Safe to unplug")
        self.assertEqual(out["drives"][0]["volumes"], "Not mounted")
        self.assertEqual(out["drives"][0]["link"], "10 Gbps · UAS")

    def test_in_flight_io_is_busy(self):
        h = Harness([drive(partition())])
        h.stats = diskstats(in_flight=3)
        out = h.settled_tick()
        self.assertEqual(out["state"], "busy")
        self.assertIn("3 operations", out["drives"][0]["detail"])

    def test_pending_writes_are_never_called_safe(self):
        h = Harness([drive(partition())])
        h.mem = meminfo(dirty_kb=4)
        out = h.settled_tick()
        self.assertEqual(out["state"], "unknown")
        self.assertIn("in DriveScope", out["drives"][0]["detail"])

    def test_read_only_mount_is_safe_despite_pending_writes(self):
        h = Harness([drive(partition(read_only=True))])
        h.mem = meminfo(dirty_kb=4)
        out = h.settled_tick()
        self.assertEqual(out["state"], "safe")
        self.assertEqual(out["drives"][0]["volumes"], "Backup (read-only)")

    def test_mount_change_withholds_safe_until_rescanned(self):
        h = Harness([drive()])
        self.assertEqual(h.settled_tick()["state"], "safe")
        h.signature = "b"
        h.drives = [drive(partition())]
        h.mem = meminfo(dirty_kb=4)
        checking = h.watcher.tick()
        self.assertEqual(checking["state"], "unknown")
        self.assertEqual(checking["drives"][0]["title"], "Checking")
        h.finish_scan()
        self.assertEqual(h.watcher.tick()["drives"][0]["title"], "Not confirmed")

    def test_hung_scan_keeps_speeds_moving_and_never_says_safe(self):
        h = Harness([drive()])
        h.settled_tick()
        h.signature = "b"
        h.watcher.tick()  # this scan never finishes
        for sectors in (2000, 4000):
            h.now += 1
            h.stats = diskstats(read_sectors=sectors)
            out = h.watcher.tick()
            self.assertEqual(out["drives"][0]["title"], "Checking")
            self.assertEqual(out["drives"][0]["readBps"], 2000 * 512)
        self.assertEqual(len(h.pending), 1)

    def test_failed_scan_reports_and_keeps_withholding_safe(self):
        h = Harness([drive()])
        h.settled_tick()
        h.signature = "b"
        h.fail = OSError("lsblk missing")
        h.settled_tick()
        out = h.watcher.tick()
        self.assertIn("lsblk missing", out["error"])
        self.assertEqual(out["drives"][0]["title"], "Checking")

    def test_unplugged_drive_disappears_right_away(self):
        h = Harness([drive()])
        h.settled_tick()
        h.stats = ""
        out = h.watcher.tick()
        self.assertEqual(out["drives"], [])
        self.assertEqual(out["state"], "none")


class RateTests(unittest.TestCase):
    def test_rates_come_from_counter_differences(self):
        h = Harness([drive()])
        h.settled_tick()
        h.now += 1
        h.stats = diskstats(read_sectors=2_000_000, write_sectors=4000, io_ticks=500)
        d = h.watcher.tick()["drives"][0]
        self.assertEqual(d["readBps"], 2_000_000 * 512)
        self.assertEqual(d["read"], "1.02 GB/s")
        self.assertEqual(d["write"], "2.05 MB/s")
        self.assertEqual(d["busy"], "50%")

    def test_replug_with_lower_counters_does_not_show_a_spike(self):
        h = Harness([drive()])
        h.stats = diskstats(read_sectors=9000)
        h.settled_tick()
        h.now += 1
        h.stats = diskstats(read_sectors=10)
        self.assertEqual(h.watcher.tick()["drives"][0]["readBps"], 0)

    def test_bar_activity_hides_background_noise(self):
        h = Harness([drive()])
        h.settled_tick()
        h.now += 1
        h.stats = diskstats(read_sectors=100, write_sectors=4000)
        self.assertEqual(h.watcher.tick()["activity"], "W 2.05 MB/s")
        h.now += 1
        self.assertEqual(h.watcher.tick()["activity"], "")


class ScanTests(unittest.TestCase):
    def test_rescans_periodically_for_late_usb_details(self):
        h = Harness([drive()])
        h.settled_tick()
        scans = h.scans
        h.now += 9
        h.watcher.tick()
        h.finish_scan()
        self.assertEqual(h.scans, scans)
        h.now += 2
        h.watcher.tick()
        h.finish_scan()
        self.assertEqual(h.scans, scans + 1)

    def test_long_labels_are_clipped_and_output_is_one_json_line(self):
        h = Harness([drive(partition(label="x" * 500 + "\nnext"))])
        line = json.dumps(h.settled_tick(), ensure_ascii=False, separators=(",", ":"))
        self.assertNotIn("\n", line)
        self.assertLess(len(json.loads(line)["drives"][0]["volumes"]), 80)


class ManifestTests(unittest.TestCase):
    ROOT = os.path.dirname(os.path.dirname(__file__))

    def test_only_one_manifest_and_its_entry_point_exists(self):
        found = [
            os.path.join(folder, name)
            for folder, dirs, names in os.walk(self.ROOT)
            if ".git" not in folder
            for name in names if name == "manifest.json"
        ]
        self.assertEqual(found, [os.path.join(self.ROOT, "manifest.json")])
        with open(found[0]) as f:
            manifest = json.load(f)
        self.assertTrue(os.path.exists(os.path.join(self.ROOT, manifest["entryPoints"]["barWidget"])))

    def test_the_watcher_never_imports_gtk(self):
        import subprocess
        import sys
        code = "import sys, drivescope.watch; sys.exit('gi' in sys.modules)"
        self.assertEqual(subprocess.run([sys.executable, "-c", code], cwd=self.ROOT).returncode, 0)


if __name__ == "__main__":
    unittest.main()
