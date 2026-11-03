"""The Benchmarks tab."""

from __future__ import annotations

import shutil
import subprocess
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk

from ..core import benchmarks, fio, format as fmt, health, safety, scan, store, verdict
from ..core.benchmarks import SuiteOptions
from ..core.devices import Drive, Partition
from ..core.store import Result
from .graph import LineChart
from .widgets import label, on_ui, table

LIVE_POINTS = 60
MIN_CURVE_POINTS = 5  # a run shorter than this has no shape worth drawing


def temperature_axis(temps: list[int]) -> tuple[int, int]:
    """Round tens just outside the readings, so a rise of a few degrees is visible."""
    return (min(temps) - 1) // 10 * 10, (max(temps) + 10) // 10 * 10
RESULT_HEADERS = [
    ("Test", ""),
    ("Speed", "What the test measured"),
    ("At the drive", "The same run, measured independently from the kernel's counters for the drive"),
    ("IOPS", "Separate operations completed each second"),
    ("Typical wait", "Half of all operations finished faster than this (p50)"),
    ("p99", "Only 1 in 100 operations took longer than this"),
    ("p99.9", "Only 1 in 1,000 operations took longer than this"),
]


def _opt(value, formatter) -> str:
    return formatter(value) if value else "–"


def result_cells(r: Result) -> list[str]:
    return [
        r.label, _opt(r.bps, fmt.rate), _opt(r.device_bps, fmt.rate), _opt(r.iops, fmt.count),
        _opt(r.p50_ms, fmt.latency), _opt(r.p99_ms, fmt.latency), _opt(r.p999_ms, fmt.latency),
    ]


class BenchPage(Gtk.ScrolledWindow):
    def __init__(self, store_: store.Store, on_message, on_running_changed, on_saved) -> None:
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.store = store_
        self.on_message = on_message
        self.on_running_changed = on_running_changed
        self.on_saved = on_saved
        self.drive: Drive | None = None
        self.volumes: list[Partition] = []
        self.suite: benchmarks.Suite | None = None
        self.running = False
        self._live_values: list[float] = []
        self._cancel_requested = False
        self._after_finish = None

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, css_classes=["ds-section"])
        root.set_margin_top(12)
        self.set_child(root)

        self.volume_group = Adw.PreferencesGroup(title="Volume")
        self.volume_row = Adw.ComboRow(title="Test on")
        self.volume_model = Gtk.StringList()
        self.volume_row.set_model(self.volume_model)
        self.volume_row.connect("notify::selected", lambda *_: self._update_availability())
        self.volume_group.add(self.volume_row)
        root.append(self.volume_group)

        self.read_group = Adw.PreferencesGroup(title="Read tests", description="These only read, and don't change your files.")
        self.opt_reads = self._switch(self.read_group, "Standard speed tests", "Sequential and random reads, about a minute", True)
        self.opt_sustained_read = self._switch(self.read_group, "Sustained read", "Reads the largest file for up to a minute", True)
        self.opt_small_read = self._switch(self.read_group, "Small files", "Reads up to 5,000 real files from this volume", True)
        self.opt_scan = self._switch(
            self.read_group, "Whole-drive scan",
            "Samples speed from start to end to find slow regions. Asks for your approval.", False,
        )
        root.append(self.read_group)

        self.write_group = Adw.PreferencesGroup(title="Write tests")
        self.opt_writes = self._switch(self.write_group, "Standard speed tests", "Sequential and random writes, about a minute", False)
        self.opt_sustained_write = self._switch(self.write_group, "Sustained write", "Shows the point where a fast cache fills. Up to 5 minutes.", False)
        self.opt_small_write = self._switch(self.write_group, "Small files", "Writes up to 5,000 small files, for about a minute at most", False)
        self.opt_copy = self._switch(self.write_group, "Real copy", "Copies a 4 GB file from your internal drive, which needs 4 GB free there too", False)
        self.opt_integrity = self._switch(self.write_group, "Integrity check", "Writes 4 GB, reads it back and compares checksums", False)
        self.opt_full = self._switch(
            self.write_group, "Check all free space",
            "Catches fake-capacity drives. Can take many hours, and adds real wear.", False,
        )
        self.opt_integrity.connect("notify::active", lambda *_: self._update_availability())
        root.append(self.write_group)

        actions = Gtk.Box(spacing=12)
        self.start_button = Gtk.Button(label="Run tests", css_classes=["suggested-action", "pill"])
        self.start_button.connect("clicked", lambda _b: self._start_clicked())
        actions.append(self.start_button)
        self.stage_label = label("", ["dim-label"], wrap=True)
        self.stage_label.set_hexpand(True)
        actions.append(self.stage_label)
        root.append(actions)

        self.progress_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, visible=False)
        self.progress = Gtk.ProgressBar()
        self.progress_box.append(self.progress)
        self.live_chart = LineChart(
            [("Speed", "drive")], fmt.rate, 10_000_000, LIVE_POINTS, 1.0, fmt.ago,
            "Speed of the test now running", height=160,
        )
        self.progress_box.append(self.live_chart)
        root.append(self.progress_box)

        self.results_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        root.append(self.results_box)

    @staticmethod
    def _switch(group, title, subtitle, active) -> Adw.SwitchRow:
        row = Adw.SwitchRow(title=title, subtitle=subtitle, active=active)
        group.add(row)
        return row

    # --- drive and volume state ---

    def set_drive(self, drive: Drive | None) -> None:
        if self.running:
            return
        changed = not (drive and self.drive and drive.stable_id == self.drive.stable_id)
        self.drive = drive
        selected_name = self._selected_volume().name if self._selected_volume() else None
        self.volumes = list(drive.mounted) if drive else []
        self.volume_model.splice(0, self.volume_model.get_n_items(), [p.display_name for p in self.volumes])
        for i, p in enumerate(self.volumes):
            if p.name == selected_name:
                self.volume_row.set_selected(i)
        self.volume_group.set_visible(bool(self.volumes))
        if changed:
            self._clear_results()
        self._update_availability()

    def _selected_volume(self) -> Partition | None:
        i = self.volume_row.get_selected()
        return self.volumes[i] if 0 <= i < len(self.volumes) else None

    def _update_availability(self) -> None:
        volume = self._selected_volume()
        if volume:
            # In the subtitle, because the row's own value cuts long names short.
            self.volume_row.set_subtitle(f"{volume.display_name} · {fmt.size(volume.free_bytes or 0)} free · {volume.fstype}")
        for row in (self.opt_reads, self.opt_sustained_read, self.opt_small_read):
            row.set_sensitive(volume is not None and not self.running)
        self.opt_scan.set_sensitive(self.drive is not None and not self.running)
        if volume is None:
            self.read_group.set_description(
                "Nothing on this drive is mounted, so only the whole-drive scan can run. Mount a volume for the other tests."
            )
        else:
            self.read_group.set_description("These only read, and don't change your files.")

        reason = safety.write_block_reason(volume.mountpoint) if volume else "Mount a writable volume to use these."
        can_write = reason is None and not self.running
        for row in (self.opt_writes, self.opt_sustained_write, self.opt_small_write, self.opt_copy, self.opt_integrity):
            row.set_sensitive(can_write)
            if reason:
                row.set_active(False)
        self.opt_full.set_sensitive(can_write and self.opt_integrity.get_active())
        if reason or not self.opt_integrity.get_active():
            self.opt_full.set_active(False)
        self.write_group.set_description(
            reason or "These create a temporary folder on the volume and delete it afterward. Your files aren't touched."
        )
        self.start_button.set_sensitive(self.drive is not None)

    def _options(self) -> SuiteOptions:
        return SuiteOptions(
            standard_reads=self.opt_reads.get_active() and self.opt_reads.get_sensitive(),
            sustained_read=self.opt_sustained_read.get_active() and self.opt_sustained_read.get_sensitive(),
            small_file_read=self.opt_small_read.get_active() and self.opt_small_read.get_sensitive(),
            drive_scan=self.opt_scan.get_active(),
            standard_writes=self.opt_writes.get_active(),
            sustained_write=self.opt_sustained_write.get_active(),
            small_file_write=self.opt_small_write.get_active(),
            real_copy=self.opt_copy.get_active(),
            integrity=self.opt_integrity.get_active(),
            integrity_full=self.opt_full.get_active(),
        )

    # --- starting ---

    def _start_clicked(self) -> None:
        if self.running:
            self.start_button.set_sensitive(False)
            self.start_button.set_label("Cancelling…")
            self._request_cancel()
            return
        options = self._options()
        if not any((options.standard_reads, options.sustained_read, options.small_file_read, options.drive_scan, options.writes)):
            self.on_message("Turn on at least one test first.")
            return
        volume = self._selected_volume()
        if options.writes:
            self._confirm_writes(options, volume)
        else:
            self._start(options, volume)

    def _request_cancel(self) -> None:
        # While waiting on the approval prompt no suite exists yet, so the worker
        # picks this flag up as soon as the prompt returns.
        self._cancel_requested = True
        if self.suite:
            self.suite.cancel()

    def stop_then(self, callback) -> None:
        """Cancel the run and remove its test files, then call back."""
        self._after_finish = callback
        self._request_cancel()

    def _confirm_writes(self, options: SuiteOptions, volume: Partition) -> None:
        usage = shutil.disk_usage(volume.mountpoint)
        planned = benchmarks.planned_write_bytes(options, usage.free, usage.total, volume.fstype)
        dialog = Adw.AlertDialog(
            heading=f"Write up to {fmt.size(planned)} to {volume.display_name}?",
            body=f"That's the total across every test. Test files are deleted along the way, and at least "
            f"{fmt.size(benchmarks.free_margin(usage.total))} stays free throughout.\n\n"
            "DriveScope only works in a temporary folder, which it deletes afterward, and doesn't touch your files. "
            "A slow USB stick can take about half an hour. Writing uses up a small part of a flash drive's limited lifespan.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("write", "Write test data")
        dialog.set_response_appearance("write", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, r: self._start(options, volume) if r == "write" else None)
        dialog.present(self.get_root())

    def _start(self, options: SuiteOptions, volume: Partition | None) -> None:
        self.running = True
        self._cancel_requested = False
        self.on_running_changed(True)
        self._update_availability()
        self.start_button.set_label("Cancel")
        self.start_button.remove_css_class("suggested-action")
        self.start_button.add_css_class("destructive-action")
        self.start_button.set_sensitive(True)
        self.progress_box.set_visible(True)
        self.progress.set_fraction(0)
        self._clear_results()
        self._live_values = []
        self.live_chart.set_values([[]])
        drive = self.drive
        threading.Thread(target=self._worker, args=(drive, options, volume), daemon=True).start()

    # --- worker thread ---

    def _worker(self, drive: Drive, options: SuiteOptions, volume: Partition | None) -> None:
        mountpoint = volume.mountpoint if volume else None
        notes = []
        scan_fd = None
        if options.drive_scan:
            on_ui(self.stage_label.set_text, "Waiting for your approval to read the whole drive…")
            try:
                scan_fd = scan.open_read_only(drive.name)
            except GLib.Error as e:
                if e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.TIMED_OUT):
                    notes.append("Whole-drive scan skipped: the approval prompt wasn't answered in time.")
                else:
                    notes.append(f"Whole-drive scan skipped: {e.message}")

        read_temperature = None
        if health.can_poll_silently():
            bridge = self.store.smart_bridge(drive.stable_id)
            if not bridge:
                try:
                    report = health.probe(drive.path)
                except (OSError, subprocess.SubprocessError, ValueError):
                    report = None
                if report:
                    bridge = report.bridge_type
                    self.store.set_smart_bridge(drive.stable_id, bridge)
            if bridge:
                def read_temperature():
                    try:
                        report = health.read_report(drive.path, bridge)
                    except (OSError, subprocess.SubprocessError):
                        return None
                    return report.temperature_c if report else None
            else:
                notes.append("Temperature wasn't tracked, because this drive didn't return health data. Some USB adapters don't pass it through.")
        else:
            notes.append("Temperature wasn't tracked, because it isn't set up. See the Health tab.")

        self.suite = benchmarks.Suite(
            drive.name, mountpoint, options,
            on_stage=lambda title, i, n: on_ui(self._on_stage, title, i, n),
            on_point=lambda _t, bps: on_ui(self._on_point, bps),
            on_result=lambda r: on_ui(self._on_result, r),
            read_temperature=read_temperature,
            scan_fd=scan_fd,
        )
        if self._cancel_requested:
            self.suite.cancel()
        try:
            outcome = self.suite.run()
        except fio.Cancelled:
            on_ui(self._finished, drive, volume, options, None, notes, "Cancelled. Nothing was saved, and any test files were removed.")
        except (fio.FioError, OSError, safety.UnsafeTarget, ValueError) as e:
            on_ui(self._finished, drive, volume, options, None, notes, f"The tests stopped: {e}")
        else:
            if read_temperature and not outcome.temperatures:
                notes.append("Temperature wasn't tracked, because the drive didn't report it during this run.")
            on_ui(self._finished, drive, volume, options, outcome, notes, None)

    # --- progress, on the main thread ---

    def _on_stage(self, title: str, index: int, total: int) -> None:
        self.stage_label.set_text(f"{title} ({index} of {total})" if total > 1 else title)
        self.progress.set_fraction(max(0, index - 1) / max(1, total))
        self._live_values = []
        self.live_chart.set_values([[]])

    def _on_point(self, bps: float) -> None:
        self._live_values = (self._live_values + [bps])[-LIVE_POINTS:]
        self.live_chart.set_values([self._live_values])

    def _on_result(self, _result: Result) -> None:
        pass  # everything is shown together at the end, with the verdict

    def _finished(self, drive, volume, options, outcome, notes, message) -> None:
        self.running = False
        self.suite = None
        self.on_running_changed(False)
        self.start_button.set_label("Run tests")
        self.start_button.remove_css_class("destructive-action")
        self.start_button.add_css_class("suggested-action")
        self.progress_box.set_visible(False)
        self.stage_label.set_text("")
        self._update_availability()
        if self._after_finish:
            self._after_finish()
            return
        if message:
            self.stage_label.set_text(message)
            return
        run_id = None
        if outcome.results:
            run_id = self.store.add_run(
                drive.stable_id, drive.display_name,
                drive.link.speed_mbps if drive.link else None,
                drive.link.protocol if drive.link else None,
                volume.display_name if volume else None,
                volume.fstype if volume else None,
                outcome.results,
            )
            self.on_saved()
        self._show_results(drive, outcome, notes, run_id, options)

    # --- results ---

    def _clear_results(self) -> None:
        child = self.results_box.get_first_child()
        while child:
            next_child = child.get_next_sibling()
            self.results_box.remove(child)
            child = next_child

    def _heading(self, text: str) -> Gtk.Label:
        return label(text, ["title-4"])

    def _show_results(self, drive, outcome, notes, run_id, options) -> None:
        self._clear_results()
        by_id = {r.test_id: r.bps for r in outcome.results if r.bps}
        reads = [by_id[k] for k in by_id if "read" in k and k.startswith(("seq", "sustained"))]
        writes = [by_id[k] for k in by_id if "write" in k and k.startswith(("seq", "sustained"))]
        findings = verdict.analyze(
            drive.link.ceiling_bps if drive.link else None,
            drive.link.speed_mbps if drive.link else None,
            max(reads, default=None), max(writes, default=None),
            q1_read_bps=by_id.get("seq-read-1m-q1"),
            write_curve=outcome.write_curve or None,
            copy_on_disk_bps=outcome.copy.on_disk_bps if outcome.copy else None,
            copy_source_bps=outcome.copy.source_bps if outcome.copy else None,
            temps_c=outcome.temperatures or None,
        )
        self.results_box.append(self._heading("What's limiting this drive"))
        group = Adw.PreferencesGroup()
        for finding in findings:
            row = Adw.ActionRow(
                title=GLib.markup_escape_text(finding.title),
                subtitle=GLib.markup_escape_text(finding.evidence),
            )
            group.add(row)
        self.results_box.append(group)

        if outcome.integrity_passed is False:
            self.results_box.append(label(
                "Integrity check failed: data read back from this drive didn't match what was written. "
                "Don't trust it with important files.", ["error", "heading"], wrap=True,
            ))

        if run_id is not None:
            summary = store.summarize_alerts(self.store.slow_alerts(run_id))
            if summary:
                self.results_box.append(label(summary, ["warning"], wrap=True))

        if outcome.results:
            self.results_box.append(self._heading("Results"))
            self.results_box.append(table(RESULT_HEADERS, [result_cells(r) for r in outcome.results]))
            details = [f"{r.label}: {r.detail}" for r in outcome.results if r.detail]
            for line in details:
                self.results_box.append(label(line, ["dim-label", "caption"], wrap=True))

        if len(outcome.write_curve) >= MIN_CURVE_POINTS:
            self._curve("Sustained write, speed over time", outcome.write_curve, "write")
        if len(outcome.read_curve) >= MIN_CURVE_POINTS:
            self._curve("Sustained read, speed over time", outcome.read_curve, "read")
        if outcome.scan_points:
            self._scan_chart(drive, outcome.scan_points)
        if len(outcome.temperature_curve) >= MIN_CURVE_POINTS:
            self._temperature_chart(outcome.temperature_curve)

        written = outcome.device_sectors_written
        if written is not None and not options.writes:
            text = (
                "The drive's own counters confirm nothing was written during this run."
                if written == 0
                else f"The drive's counters show {fmt.size(written * 512)} written during this run, by something else on this computer."
            )
            self.results_box.append(label(text, ["dim-label", "caption"], wrap=True))
        for note in [*outcome.skipped, *notes]:
            self.results_box.append(label(note, ["dim-label", "caption"], wrap=True))

    def _curve(self, title, points, key) -> None:
        self.results_box.append(self._heading(title))
        seconds = points[-1][0] if points else 0
        chart = LineChart(
            [(title.split(",")[0], key)], fmt.rate, 10_000_000, max(2, len(points)), 1.0, fmt.ago, title, height=170,
        )
        last = len(points) - 1
        chart.set_axis(
            len(points), 1.0, "0 s", f"{seconds:.0f} s",
            lambda back: f"{points[max(0, last - round(back))][0]:.0f} s in",
        )
        chart.set_values([[v for _, v in points]])
        self.results_box.append(chart)

    def _temperature_chart(self, points) -> None:
        title = "Temperature during this run"
        self.results_box.append(self._heading(title))
        low, high = temperature_axis([c for _, c in points])
        chart = LineChart(
            [("Temperature", "drive")], lambda c: f"{c:.0f}°C", high, max(2, len(points)), 1.0, fmt.ago, title,
            height=130, fixed_max=high, fixed_min=low,
        )
        last = len(points) - 1
        chart.set_axis(
            len(points), 1.0, "Start", f"{points[-1][0] / 60:.1f} min",
            lambda back: f"{points[max(0, last - round(back))][0]:.0f} s in",
        )
        chart.set_values([[float(c) for _, c in points]])
        self.results_box.append(chart)

    def _scan_chart(self, drive, points) -> None:
        title = "Speed across the whole drive"
        self.results_box.append(self._heading(title))
        chart = LineChart([("Read", "read")], fmt.rate, 10_000_000, max(2, len(points)), 1.0, fmt.ago, title, height=170)
        last = len(points) - 1
        chart.set_axis(
            len(points), 1.0, "Start", f"End ({fmt.size(drive.size)})",
            lambda back: f"At {fmt.size(points[max(0, last - round(back))].offset)}",
        )
        chart.set_values([[p.bps for p in points]])
        self.results_box.append(chart)
