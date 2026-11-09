import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from drivescope.core import devices, mounts, safety, stats

MOUNTINFO = """\
22 1 0:30 /@ / rw,relatime shared:1 - btrfs /dev/mapper/omarchy_root rw,compress=zstd
23 22 0:30 /@home /home rw,relatime shared:2 - btrfs /dev/mapper/omarchy_root rw,compress=zstd
24 22 259:5 / /boot rw,relatime shared:3 - vfat /dev/nvme0n1p5 rw
80 22 8:2 / /run/media/user/Install\\040macOS rw,nosuid shared:4 - hfsplus /dev/sda2 ro,umask=22
81 22 8:17 / /run/media/user/Backup rw,nosuid shared:5 - ext4 /dev/sdb1 rw
82 22 8:33 / /run/media/user/Locked ro,nosuid shared:6 - exfat /dev/sdc1 rw
83 22 0:30 /@snap /run/media/user/Snap rw shared:7 - btrfs /dev/mapper/omarchy_root rw
"""


class DiskstatsTests(unittest.TestCase):
    def line(self, name="sda", read_ios=0, read_sectors=0, read_ticks=0, write_ios=0,
             write_sectors=0, write_ticks=0, in_flight=0, io_ticks=0, weighted=0):
        return (f"   8 0 {name} {read_ios} 0 {read_sectors} {read_ticks} {write_ios} 0 "
                f"{write_sectors} {write_ticks} {in_flight} {io_ticks} {weighted} 0 0 0 0 0 0\n")

    def test_parses_fields(self):
        c = stats.parse_diskstats(self.line(read_ios=224, read_sectors=13594, io_ticks=41))["sda"]
        self.assertEqual((c.read_ios, c.read_sectors, c.io_ticks_ms), (224, 13594, 41))

    def test_rates_use_512_byte_sectors(self):
        a = stats.parse_diskstats(self.line())["sda"]
        b = stats.parse_diskstats(self.line(read_ios=100, read_sectors=2_000_000, read_ticks=500,
                                            io_ticks=1000, weighted=4000))["sda"]
        s = stats.derive(a, b, 2.0)
        self.assertEqual(s.read_bps, 512_000_000)  # 2,000,000 sectors * 512 B / 2 s
        self.assertEqual(s.read_iops, 50)
        self.assertEqual(s.read_latency_ms, 5)
        self.assertEqual(s.queue_depth, 2)
        self.assertEqual(s.busy_pct, 50)
        self.assertEqual(s.write_bps, 0)

    def test_idle_interval_has_zero_latency_not_division_error(self):
        a = stats.parse_diskstats(self.line())["sda"]
        s = stats.derive(a, a, 1.0)
        self.assertEqual((s.read_latency_ms, s.write_latency_ms), (0, 0))

    def test_busy_is_clamped(self):
        a = stats.parse_diskstats(self.line())["sda"]
        b = stats.parse_diskstats(self.line(io_ticks=1200))["sda"]
        self.assertEqual(stats.derive(a, b, 1.0).busy_pct, 100)

    def test_replug_counter_reset_discards_interval(self):
        a = stats.parse_diskstats(self.line(read_sectors=1000))["sda"]
        b = stats.parse_diskstats(self.line(read_sectors=10))["sda"]
        self.assertIsNone(stats.derive(a, b, 1.0))

    def test_sampler_needs_two_readings_and_survives_unplug(self):
        readings = [self.line(), self.line(read_sectors=2000), "", self.line(read_sectors=9000),
                    self.line(read_sectors=11000)]
        times = iter([0.0, 1.0, 2.0, 3.0, 4.0])
        sampler = stats.Sampler("sda", read_text=lambda: readings.pop(0), clock=lambda: next(times))
        self.assertIsNone(sampler.tick())
        self.assertEqual(sampler.tick().read_bps, 1_024_000)
        self.assertIsNone(sampler.tick())  # unplugged
        self.assertIsNone(sampler.tick())  # first reading after replug
        self.assertEqual(sampler.tick().read_bps, 1_024_000)

    def test_meminfo_pending(self):
        text = "MemTotal: 1 kB\nDirty:     20300 kB\nWriteback:   4 kB\n"
        self.assertEqual(stats.parse_meminfo_pending(text), (20300 * 1024, 4096))


class MountTests(unittest.TestCase):
    def setUp(self):
        self.m = mounts.parse_mountinfo(MOUNTINFO)

    def test_unescapes_spaces(self):
        self.assertIsNotNone(mounts.mount_at("/run/media/user/Install macOS", self.m))

    def test_read_only_from_super_options(self):
        self.assertTrue(mounts.mount_at("/run/media/user/Install macOS", self.m).read_only)

    def test_read_only_from_mount_options(self):
        self.assertTrue(mounts.mount_at("/run/media/user/Locked", self.m).read_only)

    def test_longest_prefix_wins(self):
        self.assertEqual(mounts.mount_containing("/run/media/user/Backup/a", self.m).source, "/dev/sdb1")
        self.assertEqual(mounts.mount_containing("/run/media/user/Backupish", self.m).mountpoint, "/")


class FakeVolume:
    """A temporary folder standing in for a writable external volume."""

    def __init__(self, mode=0o700):
        self._tmp = tempfile.TemporaryDirectory()
        self.mountpoint = os.path.realpath(self._tmp.name)
        os.chmod(self.mountpoint, mode)
        self.mounts = self.mounts_with("rw")

    def mounts_with(self, options):
        return mounts.parse_mountinfo(MOUNTINFO + f"90 22 8:49 / {self.mountpoint} {options} - ext4 /dev/sdd1 rw\n")

    def __enter__(self):
        self._patch = mock.patch.object(safety, "read_mounts", return_value=self.mounts)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        self._tmp.cleanup()


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.m = mounts.parse_mountinfo(MOUNTINFO)

    def test_accepts_a_file_in_an_open_test_dir(self):
        with FakeVolume() as vol:
            test_dir = safety.create_test_dir(vol.mountpoint)
            try:
                path = test_dir.file("data.bin")
                self.assertEqual(safety.approve_write_path(path), path)
                self.assertTrue(path.startswith("/proc/self/fd/"))
            finally:
                safety.remove_test_dir(test_dir)

    def test_refuses_every_ordinary_path(self):
        with FakeVolume() as vol:
            test_dir = safety.create_test_dir(vol.mountpoint)
            try:
                for path in ("/dev/sda", "/dev/sdb1", "/dev/mapper/omarchy_root", "data.bin",
                             os.path.join(test_dir.path, "data.bin"),  # its real name, not the descriptor
                             os.path.join(vol.mountpoint, "data.bin"),
                             f"/proc/self/fd/{test_dir.fd}/../data.bin",
                             f"/proc/self/fd/{test_dir.fd}/sub/data.bin",
                             f"/proc/{os.getpid()}/fd/{test_dir.fd}/data.bin"):
                    with self.subTest(path=path), self.assertRaises(safety.UnsafeTarget):
                        safety.approve_write_path(path)
            finally:
                safety.remove_test_dir(test_dir)

    def test_refuses_a_descriptor_that_isnt_an_open_test_dir(self):
        with FakeVolume() as vol:
            fd = os.open(vol.mountpoint, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with self.assertRaises(safety.UnsafeTarget):
                    safety.approve_write_path(f"/proc/self/fd/{fd}/data.bin")
            finally:
                os.close(fd)
            test_dir = safety.create_test_dir(vol.mountpoint)
            path = test_dir.file("data.bin")
            safety.remove_test_dir(test_dir)
            with self.assertRaises(safety.UnsafeTarget):
                safety.approve_write_path(path)  # closed, so the number means nothing now

    def test_refuses_unusual_file_names(self):
        with FakeVolume() as vol:
            test_dir = safety.create_test_dir(vol.mountpoint)
            try:
                for name in ("..", ".hidden", "a/b", "", "a:b"):
                    with self.subTest(name=name), self.assertRaises(safety.UnsafeTarget):
                        test_dir.file(name)
            finally:
                safety.remove_test_dir(test_dir)

    def test_refuses_symlink_fifo_and_hard_link_targets(self):
        with FakeVolume() as vol:
            test_dir = safety.create_test_dir(vol.mountpoint)
            try:
                os.symlink("/dev/null", "link", dir_fd=test_dir.fd)
                os.mkfifo("fifo", dir_fd=test_dir.fd)
                open(os.path.join(test_dir.path, "one"), "w").close()
                os.link("one", "two", src_dir_fd=test_dir.fd, dst_dir_fd=test_dir.fd)
                for name in ("link", "fifo", "two"):
                    with self.subTest(name=name), self.assertRaises(safety.UnsafeTarget):
                        safety.approve_write_path(test_dir.file(name))
            finally:
                safety.remove_test_dir(test_dir)

    def test_refuses_volume_that_became_read_only(self):
        with FakeVolume() as vol:
            test_dir = safety.create_test_dir(vol.mountpoint)
            try:
                with mock.patch.object(safety, "read_mounts", return_value=vol.mounts_with("ro")):
                    with self.assertRaises(safety.UnsafeTarget):
                        safety.approve_write_path(test_dir.file("data.bin"))
            finally:
                safety.remove_test_dir(test_dir)

    def test_refuses_system_volumes(self):
        for mp in ("/", "/home", "/boot", "/run/media/user/Snap"):
            with self.subTest(mp=mp), self.assertRaises(safety.UnsafeTarget):
                safety.create_test_dir(mp, self.m)

    def test_refuses_other_mount_of_the_system_device(self):
        reason = safety.write_block_reason("/run/media/user/Snap", self.m)
        self.assertIn("system", reason)

    def test_create_test_dir_refuses_read_only(self):
        with self.assertRaises(safety.UnsafeTarget):
            safety.create_test_dir("/run/media/user/Install macOS", self.m)

    def test_refuses_a_top_folder_other_accounts_can_rename_in(self):
        for mode in (0o777, 0o775, 0o757):
            with self.subTest(mode=oct(mode)), FakeVolume(mode) as vol:
                with self.assertRaises(safety.UnsafeTarget):
                    safety.create_test_dir(vol.mountpoint)
                self.assertEqual(os.listdir(vol.mountpoint), [])

    def test_accepts_a_sticky_or_private_top_folder(self):
        for mode in (0o1777, 0o755, 0o700):
            with self.subTest(mode=oct(mode)), FakeVolume(mode) as vol:
                safety.remove_test_dir(safety.create_test_dir(vol.mountpoint))
                self.assertEqual(os.listdir(vol.mountpoint), [])

    def test_swapping_the_test_dir_cannot_redirect_a_write(self):
        """The reported attack: after approval, the directory is renamed away and
        replaced by one whose file is a symlink to something the user can write."""
        from drivescope.core import fio

        with FakeVolume() as vol, tempfile.TemporaryDirectory() as home:
            victim = os.path.join(home, "victim")
            Path(victim).write_text("precious")
            test_dir = safety.create_test_dir(vol.mountpoint)
            try:
                path = safety.approve_write_path(test_dir.file("bench.bin"))
                moved = os.path.join(vol.mountpoint, "moved")
                os.rename(test_dir.path, moved)
                os.mkdir(test_dir.path)
                os.symlink(victim, os.path.join(test_dir.path, "bench.bin"))

                # A stand-in for fio: a separate process that opens --filename itself.
                fake_fio = ("import sys; open(sys.argv[1], 'w').write('benchmark'); "
                            "print('{\"jobs\": [{\"write\": {}}]}')")
                fio.run([sys.executable, "-c", fake_fio, path], "write")

                self.assertEqual(Path(victim).read_text(), "precious")
                self.assertEqual(Path(moved, "bench.bin").read_text(), "benchmark")
                safety.remove_test_dir(test_dir)
                self.assertEqual(os.listdir(moved), [])  # its own files are gone
                self.assertTrue(os.path.islink(os.path.join(test_dir.path, "bench.bin")))  # the decoy is left alone
                self.assertEqual(Path(victim).read_text(), "precious")
            finally:
                with safety._open_lock:
                    still_open = test_dir.fd in safety._open_dirs
                if still_open:
                    safety.remove_test_dir(test_dir)

    def test_remove_removes_contents_and_directory(self):
        with FakeVolume() as vol:
            test_dir = safety.create_test_dir(vol.mountpoint)
            for name in ("a.bin", "b.bin"):
                fd = os.open(safety.approve_write_path(test_dir.file(name)), os.O_WRONLY | os.O_CREAT, 0o600)
                os.close(fd)
            safety.remove_test_dir(test_dir)
            self.assertEqual(os.listdir(vol.mountpoint), [])
            self.assertNotIn(test_dir.fd, safety.open_test_dir_fds())

    def test_remove_orphan_only_removes_test_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            keep = os.path.join(tmp, "keep")
            os.mkdir(keep)
            with self.assertRaises(safety.UnsafeTarget):
                safety.remove_orphaned_test_dir(keep)
            self.assertTrue(os.path.isdir(keep))

            target = os.path.join(tmp, ".drivescope-test-" + "c" * 32)
            os.mkdir(target)
            os.mkdir(os.path.join(target, "sub"))
            open(os.path.join(target, "f"), "w").close()
            safety.remove_orphaned_test_dir(target)
            self.assertFalse(os.path.exists(target))

    def test_remove_orphan_refuses_symlink_named_like_test_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            keep = os.path.join(tmp, "keep")
            os.mkdir(keep)
            open(os.path.join(keep, "f"), "w").close()
            link = os.path.join(tmp, ".drivescope-test-" + "d" * 32)
            os.symlink(keep, link)
            with self.assertRaises(safety.UnsafeTarget):
                safety.remove_orphaned_test_dir(link)
            self.assertTrue(os.path.exists(os.path.join(keep, "f")))

    def test_finds_orphans(self):
        with tempfile.TemporaryDirectory() as tmp:
            orphan = os.path.join(tmp, ".drivescope-test-" + "e" * 32)
            os.mkdir(orphan)
            os.mkdir(os.path.join(tmp, ".drivescope-test-nope"))
            self.assertEqual(safety.find_orphaned_test_dirs(tmp), [orphan])


class DeviceTests(unittest.TestCase):
    def link(self, **kw):
        base = dict(speed_mbps=10000, version=" 3.20", manufacturer="Example", product="External SSD",
                    id_vendor="ffff", id_product="0001", driver="uas", max_power="896mA",
                    root_hub_mbps=10000, through_hub=False)
        base.update(kw)
        return devices.UsbLink(**base)

    def test_healthy_link_has_no_warnings(self):
        self.assertEqual(self.link().warnings(), [])
        self.assertEqual(self.link().protocol, "UAS")
        self.assertEqual(self.link().ceiling_bps, 1_212_000_000)

    def test_usb3_at_usb2_speed_warns(self):
        self.assertIn("USB 2 speed", self.link(speed_mbps=480).warnings()[0])

    def test_slower_than_bus_warns(self):
        self.assertIn("5 Gbps", self.link(speed_mbps=5000, root_hub_mbps=10000).warnings()[0])

    def test_usb3_0_at_5_gbps_is_its_top_speed_not_a_problem(self):
        self.assertEqual(self.link(speed_mbps=5000, version=" 3.00", root_hub_mbps=10000).warnings(), [])

    def test_bot_warns(self):
        link = self.link(driver="usb-storage")
        self.assertEqual(link.protocol, "BOT")
        self.assertIn("BOT", link.warnings()[0])

    def test_lsblk_hides_internal_unless_asked(self):
        text = json.dumps({"blockdevices": [
            {"name": "sda", "type": "disk", "tran": "usb", "hotplug": True},
            {"name": "nvme0n1", "type": "disk", "tran": "nvme", "hotplug": False},
            {"name": "zram0", "type": "disk", "tran": None, "hotplug": False},
        ]})
        self.assertEqual([d["name"] for d in devices.parse_lsblk(text, [])], ["sda"])
        self.assertNotIn("zram0", [d["name"] for d in devices.parse_lsblk(text, [], include_internal=True)])
        with_internal = [d["name"] for d in devices.parse_lsblk(text, [], include_internal=True)]
        self.assertIn("nvme0n1", with_internal)

    def test_real_system_finds_the_usb_drive_when_attached(self):
        found = devices.list_drives()
        for drive in found:
            self.assertTrue(drive.path.startswith("/dev/"))


if __name__ == "__main__":
    unittest.main()
