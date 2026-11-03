import unittest

from drivescope.core import format as fmt
from drivescope.core import procs, unplug
from drivescope.core.devices import Drive, Partition


def drive(*mounts):
    """Each mount is (mountpoint, read_only). An empty mountpoint means unmounted."""
    parts = tuple(
        Partition(f"sdz{i}", f"/dev/sdz{i}", 1000, "ext4", "", mp, ro, None)
        for i, (mp, ro) in enumerate(mounts)
    )
    return Drive("sdz", "/dev/sdz", "V", "M", "S", 1000, "usb", False, True, parts)


class UnplugTests(unittest.TestCase):
    def state(self, d, in_flight=0, dirty=4096, writeback=0, since=None, written=0):
        return unplug.evaluate(d, in_flight, dirty, writeback, since, written).state

    def test_in_flight_is_busy_even_when_read_only(self):
        self.assertEqual(self.state(drive(("/m", True)), in_flight=1), unplug.BUSY)

    def test_read_only_is_safe_despite_system_dirty_data(self):
        self.assertEqual(self.state(drive(("/m", True))), unplug.SAFE)

    def test_unmounted_is_safe(self):
        self.assertEqual(self.state(drive(("", False))), unplug.SAFE)

    def test_writable_with_system_dirty_data_is_unknown(self):
        self.assertEqual(self.state(drive(("/m", False))), unplug.UNKNOWN)

    def test_one_writable_partition_is_enough_to_be_unknown(self):
        self.assertEqual(self.state(drive(("/a", True), ("/b", False))), unplug.UNKNOWN)

    def test_no_dirty_data_anywhere_is_safe(self):
        self.assertEqual(self.state(drive(("/m", False)), dirty=0), unplug.SAFE)

    def test_writeback_counts_as_pending(self):
        self.assertEqual(self.state(drive(("/m", False)), dirty=0, writeback=1), unplug.UNKNOWN)

    def test_recent_flush_is_safe(self):
        self.assertEqual(self.state(drive(("/m", False)), since=5), unplug.SAFE)

    def test_flush_expires(self):
        self.assertEqual(self.state(drive(("/m", False)), since=unplug.FLUSH_TRUST_S + 1), unplug.UNKNOWN)

    def test_write_after_flush_cancels_it(self):
        self.assertEqual(self.state(drive(("/m", False)), since=5, written=8), unplug.UNKNOWN)


class FormatTests(unittest.TestCase):
    def test_rate(self):
        self.assertEqual(fmt.rate(0), "0 MB/s")
        self.assertEqual(fmt.rate(5_000), "5 KB/s")
        self.assertEqual(fmt.rate(5_000_000), "5.00 MB/s")
        self.assertEqual(fmt.rate(523_000_000), "523 MB/s")
        self.assertEqual(fmt.rate(1_212_000_000), "1.21 GB/s")

    def test_size(self):
        self.assertEqual(fmt.size(0), "0 B")
        self.assertEqual(fmt.size(3_490_000), "3.49 MB")
        self.assertEqual(fmt.size(1_000_204_886_016), "1.00 TB")

    def test_latency(self):
        self.assertEqual(fmt.latency(0), "0 ms")
        self.assertEqual(fmt.latency(0.25), "250 µs")
        self.assertEqual(fmt.latency(1.66), "1.66 ms")

    def test_ago(self):
        self.assertEqual(fmt.ago(0), "now")
        self.assertEqual(fmt.ago(34), "34 s ago")
        self.assertEqual(fmt.ago(119.5), "2 min ago")
        self.assertEqual(fmt.ago(90), "1.5 min ago")


class ProcsTests(unittest.TestCase):
    def test_parse_io(self):
        text = "rchar: 1\nwchar: 2\nsyscr: 3\nsyscw: 4\nread_bytes: 500\nwrite_bytes: 600\ncancelled_write_bytes: 0\n"
        self.assertEqual(procs.parse_io(text), (500, 600))

    def test_mount_prefix_does_not_match_sibling(self):
        self.assertTrue(procs._under("/media/a/file", ("/media/a",)))
        self.assertFalse(procs._under("/media/ab/file", ("/media/a",)))


class BrandAssetTests(unittest.TestCase):
    def test_every_icon_is_a_png_of_its_stated_size(self):
        import struct

        from drivescope import APP_ID, DATA_DIR

        icons = sorted((DATA_DIR / "icons" / "hicolor").glob(f"*/apps/{APP_ID}*.png"))
        self.assertEqual(len(icons), 18)  # nine sizes, for each of the launcher and light icons
        for path in icons:
            with self.subTest(path=path.parent.parent.name):
                header = path.read_bytes()[:24]
                self.assertEqual(header[:8], b"\x89PNG\r\n\x1a\n")
                width, height = struct.unpack(">II", header[16:24])
                self.assertEqual(f"{width}x{height}", path.parent.parent.name)

    def test_artwork_exists_for_both_themes(self):
        from drivescope import DATA_DIR

        for name in ("wordmark-tight", "wordmark-dark-tight"):
            self.assertTrue((DATA_DIR / "brand" / f"{name}.png").is_file(), name)
        for name in ("logo-dark", "logo-light", "wordmark-dark", "wordmark-light"):
            svg = (DATA_DIR / "brand" / f"drivescope-{name}.svg").read_text()
            self.assertIn("<path", svg, name)
            self.assertNotIn("<image", svg, name)  # real vectors, not a wrapped bitmap

    def test_desktop_entry_uses_the_app_icon(self):
        from drivescope import APP_ID, DATA_DIR

        text = (DATA_DIR / f"{APP_ID}.desktop").read_text()
        self.assertIn(f"Icon={APP_ID}", text)


if __name__ == "__main__":
    unittest.main()
