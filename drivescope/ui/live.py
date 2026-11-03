"""The Live tab: what the drive is actually doing right now."""

from __future__ import annotations

import subprocess
import threading
import time
from collections import deque

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Pango

from ..core import format as fmt
from ..core import stats, unplug
from ..core.devices import Drive
from ..core.procs import DriveUser, ProcessScanner
from .graph import LineChart, Swatch

SAMPLE_MS = 500
HISTORY_POINTS = 240  # two minutes
PROCESS_SCAN_EVERY = 6  # samples, so every three seconds
_STATE_ICONS = {
    unplug.SAFE: "emblem-ok-symbolic",
    unplug.BUSY: "content-loading-symbolic",
    unplug.UNKNOWN: "dialog-warning-symbolic",
}


def _label(text="", classes=(), xalign=0.0, wrap=False) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=xalign, css_classes=list(classes))
    if wrap:
        label.set_wrap(True)
        label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    return label


class _ThroughputHeader(Gtk.Box):
    """Current speed for one direction, doubling as the chart's legend."""

    def __init__(self, title: str, color_key: str) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0, hexpand=False)
        top = Gtk.Box(spacing=7)
        top.append(Swatch(color_key))
        top.append(_label(title, ["heading"]))
        self.value = _label("0 MB/s", ["ds-value"])
        self.detail = _label("Peak 0 MB/s", ["dim-label", "caption"])
        self.append(top)
        self.append(self.value)
        self.append(self.detail)

    def update(self, current: float, peak: float) -> None:
        self.value.set_text(fmt.rate(current))
        self.detail.set_text(f"Peak {fmt.rate(peak)} in the last 2 min")


class _MiniChart(Gtk.Box):
    """A titled small chart. Each measure gets its own axis, never a shared one."""

    def __init__(self, title, explanation, series, formatter, floor, fixed_max=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        self.formatter = formatter
        head = Gtk.Box(spacing=8)
        head.append(_label(title, ["ds-chart-title"]))
        self.value = _label("", ["dim-label", "numeric"], xalign=1.0)
        self.value.set_hexpand(True)
        head.append(self.value)
        self.append(head)
        self.append(_label(explanation, ["dim-label", "caption"], wrap=True))
        self.chart = LineChart(
            series, formatter, floor, HISTORY_POINTS, SAMPLE_MS / 1000, fmt.ago,
            f"{title}: {explanation}", height=110, fixed_max=fixed_max,
        )
        self.chart.set_margin_top(6)
        self.append(self.chart)

    def update(self, values: list[list[float]]) -> None:
        self.chart.set_values(values)
        currents = [self.formatter(v[-1]) for v in values if v]
        names = [s.name for s in self.chart.series]
        if len(currents) > 1:
            self.value.set_text(" · ".join(f"{n} {c}" for n, c in zip(names, currents)))
        else:
            self.value.set_text(currents[0] if currents else "")


class LivePage(Gtk.ScrolledWindow):
    def __init__(self, on_message) -> None:
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.on_message = on_message
        self.drive: Drive | None = None
        self.sampler: stats.Sampler | None = None
        self.samples: deque[stats.Sample] = deque(maxlen=HISTORY_POINTS)
        self.scanner = ProcessScanner()
        self._tick_count = 0
        self._scanning = False
        self._flushed_at: float | None = None
        self._flushed_sectors = 0
        self._busy_action = False

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.set_child(root)
        root.append(self._build_status())
        root.append(Gtk.Separator())
        root.append(self._build_throughput())
        root.append(self._build_minis())
        root.append(self._build_pending_and_users())

        GLib.timeout_add(SAMPLE_MS, self._tick)

    # --- construction ---

    def _build_status(self) -> Gtk.Widget:
        strip = Gtk.Box(spacing=12, css_classes=["ds-strip"])
        self.status_icon = Gtk.Image(icon_size=Gtk.IconSize.NORMAL)
        strip.append(self.status_icon)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.status_title = _label("", ["heading"])
        self.status_detail = _label("", ["dim-label", "caption"], wrap=True)
        text.append(self.status_title)
        text.append(self.status_detail)
        strip.append(text)
        self.flush_button = Gtk.Button(
            label="Flush and verify", valign=Gtk.Align.CENTER,
            tooltip_text="Wait until cached writes for this drive's volumes have reached the drive",
        )
        self.flush_button.connect("clicked", lambda _b: self._flush())
        strip.append(self.flush_button)
        self.eject_button = Gtk.Button(
            label="Safely eject", valign=Gtk.Align.CENTER,
            tooltip_text="Unmount every volume on this drive and power it off",
        )
        self.eject_button.connect("clicked", lambda _b: self._eject())
        strip.append(self.eject_button)
        return strip

    def _build_throughput(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, css_classes=["ds-section"])
        box.set_margin_top(8)
        heads = Gtk.Box(spacing=40)
        self.read_head = _ThroughputHeader("Read", "read")
        self.write_head = _ThroughputHeader("Write", "write")
        heads.append(self.read_head)
        heads.append(self.write_head)
        box.append(heads)
        self.throughput = LineChart(
            [("Read", "read"), ("Write", "write")], fmt.rate, 10_000_000, HISTORY_POINTS,
            SAMPLE_MS / 1000, fmt.ago, "Read and write speed measured at the drive", height=230,
        )
        box.append(self.throughput)
        return box

    def _build_minis(self) -> Gtk.Widget:
        self.iops = _MiniChart(
            "IOPS", "Separate read and write operations each second",
            [("Read", "read"), ("Write", "write")], fmt.count, 10,
        )
        self.lat = _MiniChart(
            "Latency", "Average wait for each operation to finish",
            [("Read", "read"), ("Write", "write")], fmt.latency, 1,
        )
        self.queue = _MiniChart(
            "Queue depth", "Operations waiting or in progress at once",
            [("Queue depth", "drive")], fmt.depth, 2,
        )
        self.busy = _MiniChart(
            "Busy", "Share of time the drive was working",
            [("Busy", "drive")], fmt.percent, 100, fixed_max=100,
        )
        grid = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, min_children_per_line=1,
            max_children_per_line=4, column_spacing=24, row_spacing=18, css_classes=["ds-section"],
        )
        for mini in (self.iops, self.lat, self.queue, self.busy):
            mini.set_size_request(280, -1)
            grid.append(mini)
            mini.get_parent().set_focusable(False)
        return grid

    def _build_pending_and_users(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, css_classes=["ds-section"])
        pending = Adw.PreferencesGroup(
            title="Data not on a drive yet",
            description="Writes wait in RAM before reaching a drive. "
            "Linux reports the first two rows for all drives together, not per drive.",
        )
        self.dirty_row = Adw.ActionRow(title="Waiting in RAM", subtitle="All drives")
        self.writeback_row = Adw.ActionRow(title="Being written now", subtitle="All drives")
        self.inflight_row = Adw.ActionRow(title="In progress on this drive", subtitle="Operations sent, not finished")
        self._pending_values = []
        for row in (self.dirty_row, self.writeback_row, self.inflight_row):
            value = _label("", ["numeric"])
            row.add_suffix(value)
            self._pending_values.append(value)
            pending.add(row)
        box.append(pending)

        self.users_group = Adw.PreferencesGroup(
            title="Using this drive",
            description="Your processes with files open here. Their speeds cover all "
            "drives, so treat them as approximate.",
        )
        self.users_empty = Adw.ActionRow(title="None of your processes have files open on this drive")
        self.users_group.add(self.users_empty)
        self._user_rows: list[Adw.ActionRow] = []
        box.append(self.users_group)
        return box

    # --- drive changes ---

    def set_drive(self, drive: Drive | None) -> None:
        same = drive and self.drive and drive.stable_id == self.drive.stable_id
        self.drive = drive
        if same:
            self.sampler.device = drive.name
            return
        self.samples.clear()
        self.sampler = stats.Sampler(drive.name) if drive else None
        self.scanner = ProcessScanner()
        self._flushed_at = None
        self._render_charts()
        if drive and drive.link and drive.link.ceiling_bps:
            gbps = drive.link.speed_mbps / 1000
            self.throughput.set_reference(drive.link.ceiling_bps, f"{gbps:g} Gbps USB link limit")
        else:
            self.throughput.set_reference(None)

    # --- sampling ---

    def _tick(self) -> bool:
        if self.sampler is None:
            return GLib.SOURCE_CONTINUE
        sample = self.sampler.tick()
        if sample is not None:
            self.samples.append(sample)
            self._render_charts()
            self._render_status(sample)
        self._tick_count += 1
        if self._tick_count % PROCESS_SCAN_EVERY == 0:
            self._scan_processes()
        return GLib.SOURCE_CONTINUE

    def _render_charts(self) -> None:
        s = self.samples
        reads = [x.read_bps for x in s]
        writes = [x.write_bps for x in s]
        self.throughput.set_values([reads, writes])
        self.read_head.update(reads[-1] if reads else 0, max(reads, default=0))
        self.write_head.update(writes[-1] if writes else 0, max(writes, default=0))
        self.iops.update([[x.read_iops for x in s], [x.write_iops for x in s]])
        self.lat.update([[x.read_latency_ms for x in s], [x.write_latency_ms for x in s]])
        self.queue.update([[x.queue_depth for x in s]])
        self.busy.update([[x.busy_pct for x in s]])

    def _render_status(self, sample: stats.Sample) -> None:
        dirty, writeback = stats.pending_write_bytes()
        since = time.monotonic() - self._flushed_at if self._flushed_at else None
        written = 0
        if self._flushed_at:
            total = stats.total_sectors_written(self.drive.name) or 0
            written = max(0, total - self._flushed_sectors)
        status = unplug.evaluate(self.drive, sample.in_flight, dirty, writeback, since, written)
        self.status_icon.set_from_icon_name(_STATE_ICONS[status.state])
        self.status_title.set_text(status.title)
        self.status_detail.set_text(status.detail)
        for label, text in zip(self._pending_values, (fmt.size(dirty), fmt.size(writeback), str(sample.in_flight))):
            label.set_text(text)
        has_writable = any(not p.mount_read_only for p in self.drive.mounted)
        self.flush_button.set_visible(has_writable)
        self.flush_button.set_sensitive(not self._busy_action)
        self.eject_button.set_sensitive(not self._busy_action)

    # --- processes ---

    def _scan_processes(self) -> None:
        if self._scanning or not self.drive:
            return
        self._scanning = True
        mountpoints = [p.mountpoint for p in self.drive.mounted]

        def work():
            users = self.scanner.scan(mountpoints, time.monotonic())
            GLib.idle_add(self._show_users, users)

        threading.Thread(target=work, daemon=True).start()

    def _show_users(self, users: list[DriveUser]) -> bool:
        self._scanning = False
        for row in self._user_rows:
            self.users_group.remove(row)
        self._user_rows = []
        self.users_empty.set_visible(not users)
        for user in users[:8]:
            files = f"{user.open_files} open file{'s' if user.open_files != 1 else ''}"
            row = Adw.ActionRow(title=GLib.markup_escape_text(user.name), subtitle=f"PID {user.pid} · {files}")
            row.set_tooltip_text(user.example_path)
            row.add_suffix(_label(f"R {fmt.rate(user.read_bps)} · W {fmt.rate(user.write_bps)}", ["numeric", "dim-label"]))
            self.users_group.add(row)
            self._user_rows.append(row)
        return GLib.SOURCE_REMOVE

    # --- actions ---

    def _flush(self) -> None:
        mountpoints = [p.mountpoint for p in self.drive.mounted if not p.mount_read_only]
        self._busy_action = True
        self.flush_button.set_label("Flushing…")
        self.flush_button.set_sensitive(False)
        device = self.drive.name

        def work():
            error = None
            try:
                unplug.flush_mountpoints(mountpoints)
            except OSError as e:
                error = str(e)
            GLib.idle_add(self._flush_done, device, error)

        threading.Thread(target=work, daemon=True).start()

    def _flush_done(self, device: str, error: str | None) -> bool:
        self._busy_action = False
        self.flush_button.set_label("Flush and verify")
        if error:
            self.on_message(f"Couldn't flush this drive: {error}")
        else:
            self._flushed_at = time.monotonic()
            self._flushed_sectors = stats.total_sectors_written(device) or 0
            self.on_message("Flushed. Cached writes for this drive have reached it.")
        return GLib.SOURCE_REMOVE

    def _eject(self) -> None:
        drive = self.drive
        self._busy_action = True
        self.eject_button.set_label("Ejecting…")
        self.eject_button.set_sensitive(False)

        def work():
            error = unmounted = None
            for part in drive.mounted:
                done = subprocess.run(
                    ["udisksctl", "unmount", "--no-user-interaction", "-b", part.path],
                    capture_output=True, text=True,
                )
                if done.returncode != 0:
                    error = (done.stderr or done.stdout).strip()
                    break
            else:
                unmounted = True
                done = subprocess.run(["udisksctl", "power-off", "-b", drive.path], capture_output=True, text=True)
                if done.returncode != 0:
                    error = (done.stderr or done.stdout).strip()
            GLib.idle_add(self._eject_done, drive, error, unmounted)

        threading.Thread(target=work, daemon=True).start()

    def _eject_done(self, drive: Drive, error: str | None, unmounted: bool | None) -> bool:
        self._busy_action = False
        self.eject_button.set_label("Safely eject")
        if error and unmounted:
            # Unmounting already flushed everything, so the data is safe either way.
            self.on_message("Unmounted, so it's safe to unplug. It couldn't be powered off, so its light may stay on.")
        elif error:
            hint = " Close anything using the drive, listed below, and try again." if "busy" in error.lower() else ""
            self.on_message(f"Couldn't eject: {error}.{hint}")
        else:
            self.on_message(f"{drive.display_name} is powered off. You can unplug it now.")
        return GLib.SOURCE_REMOVE
