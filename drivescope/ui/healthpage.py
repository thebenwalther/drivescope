"""The Health tab: how the drive is connected, its SMART data, and logged problems."""

from __future__ import annotations

import shlex
import subprocess
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from ..core import format as fmt, health, store
from ..core.devices import Drive
from .widgets import clear_group, on_ui, value_row

# The app runs from its checkout, so the policy file sits beside the package.
POLICY_FILE = Path(__file__).resolve().parents[2] / "data" / "dev.drivescope.smartctl.policy"
SETUP_COMMAND = f"sudo install -m 644 {shlex.quote(str(POLICY_FILE))} /usr/share/polkit-1/actions/"


def _known(value, formatter=str) -> str:
    return formatter(value) if value is not None else "Not reported"


class HealthPage(Gtk.ScrolledWindow):
    def __init__(self, store_: store.Store, on_message) -> None:
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.store = store_
        self.on_message = on_message
        self.drive: Drive | None = None
        self._reading = False
        self._connection_rows: list = []
        self._smart_rows: list = []
        self._problem_rows: list = []
        self._loaded_id = None

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, css_classes=["ds-section"])
        root.set_margin_top(12)
        self.set_child(root)

        self.connection_group = Adw.PreferencesGroup(title="Connection")
        root.append(self.connection_group)

        self.smart_group = Adw.PreferencesGroup(
            title="Drive health",
            description="Only root can ask a drive for this, so reading it asks for your approval. It doesn't change anything on the drive.",
        )
        self.read_button = Gtk.Button(label="Read health data", valign=Gtk.Align.CENTER)
        self.read_button.connect("clicked", lambda _b: self._read_health())
        self.smart_group.set_header_suffix(self.read_button)
        root.append(self.smart_group)

        self.problem_group = Adw.PreferencesGroup(
            title="Problems logged since this computer started",
            description="Unplugging a drive also logs a disconnect, so one on its own is normal.",
        )
        refresh = Gtk.Button(label="Refresh", valign=Gtk.Align.CENTER)
        refresh.connect("clicked", lambda _b: self._read_problems())
        self.problem_group.set_header_suffix(refresh)
        root.append(self.problem_group)

    def set_drive(self, drive: Drive | None) -> None:
        self.drive = drive
        if drive is None:
            self._loaded_id = None
            return
        self._show_connection(drive)
        if drive.stable_id != self._loaded_id:
            self._loaded_id = drive.stable_id
            clear_group(self.smart_group, self._smart_rows)
            self._read_problems()

    # --- connection ---

    def _show_connection(self, drive: Drive) -> None:
        clear_group(self.connection_group, self._connection_rows)
        rows = []
        link = drive.link
        if link:
            for warning in link.warnings():
                row = Adw.ActionRow(title=GLib.markup_escape_text(warning))
                row.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic"))
                rows.append(row)
            rows.append(value_row(
                "Link speed", f"{link.speed_mbps / 1000:g} Gbps",
                f"Raw limit about {fmt.rate(link.ceiling_bps)}" if link.ceiling_bps else "",
            ))
            rows.append(value_row("USB version", link.version))
            protocol_help = {
                "UAS": "The fast USB storage protocol, which can queue requests",
                "BOT": "The older USB storage protocol, one request at a time",
            }.get(link.protocol, "")
            rows.append(value_row("Protocol", link.protocol, protocol_help))
            rows.append(value_row("Port's bus speed", f"{link.root_hub_mbps / 1000:g} Gbps", f"USB port {link.port}" if link.port else ""))
            rows.append(value_row("Connected through a hub", "Yes" if link.through_hub else "No"))
            rows.append(value_row("Power requested", link.max_power or "Not reported"))
        else:
            rows.append(value_row("Connection", drive.transport.upper() or "Unknown"))
        rows.append(value_row("Queue depth", str(drive.queue_depth or "Not reported"), "How many operations the drive accepts at once"))
        rows.append(value_row("Largest single transfer", f"{drive.max_transfer_kb} KB" if drive.max_transfer_kb else "Not reported"))
        rows.append(value_row("I/O scheduler", drive.scheduler or "Not reported"))
        for row in rows:
            self.connection_group.add(row)
        self._connection_rows.extend(rows)

    # --- SMART ---

    def _read_health(self) -> None:
        if self._reading or not self.drive:
            return
        self._reading = True
        self.read_button.set_sensitive(False)
        self.read_button.set_label("Waiting for approval…")
        drive = self.drive
        bridge = self.store.smart_bridge(drive.stable_id)

        def work():
            report = error = None
            try:
                if bridge:
                    report = health.read_report(drive.path, bridge)
                if report is None:
                    report = health.probe(drive.path)
                if report:
                    self.store.set_smart_bridge(drive.stable_id, report.bridge_type)
            except (OSError, subprocess.SubprocessError, ValueError) as e:
                error = str(e)
            silent = health.can_poll_silently()
            on_ui(self._health_done, drive, report, error, silent)

        threading.Thread(target=work, daemon=True).start()

    def _health_done(self, drive, report, error, silent) -> None:
        self._reading = False
        self.read_button.set_sensitive(True)
        self.read_button.set_label("Read health data")
        if error:
            self.on_message(f"Couldn't read health data: {error}")
            return
        if self.drive is None or drive.stable_id != self.drive.stable_id:
            return
        clear_group(self.smart_group, self._smart_rows)
        rows = self._report_rows(drive, report, silent)
        for row in rows:
            self.smart_group.add(row)
        self._smart_rows.extend(rows)

    def _report_rows(self, drive, r, silent) -> list:
        if r is None:
            return [Adw.ActionRow(
                title="No health data available",
                subtitle="This drive's USB bridge doesn't pass health data through to the computer.",
            )]
        verdict = {True: "Passed", False: "FAILED: back up this drive now", None: "Not reported"}[r.passed]
        rows = [value_row("Drive's own assessment", verdict)]
        same_name = r.model.lower() in drive.display_name.lower()
        rows.append(value_row(
            "Actual drive inside", r.model,
            "" if same_name else f"Sold as {drive.display_name}. Portable drives often contain another brand's SSD.",
        ))
        if r.firmware:
            rows.append(value_row("Firmware", r.firmware))
        rows.append(value_row(
            "Temperature", _known(r.temperature_c, lambda c: f"{c}°C"),
            f"Its warning limit is {r.temperature_warn_c}°C" if r.temperature_warn_c else "",
        ))
        rows.append(value_row("Wear", _known(r.wear_pct, lambda p: f"{p}%"), "How much of its rated write life is used"))
        if r.spare_pct is not None:
            rows.append(value_row("Spare capacity left", f"{r.spare_pct}%", "Reserved flash for replacing worn cells"))
        rows.append(value_row("Total written", _known(r.data_written_bytes, fmt.size)))
        rows.append(value_row("Total read", _known(r.data_read_bytes, fmt.size)))
        rows.append(value_row("Time powered on", _known(r.power_on_hours, lambda h: f"{h:,} hours")))
        rows.append(value_row("Times powered on", _known(r.power_cycles, lambda n: f"{n:,}")))
        rows.append(value_row(
            "Unsafe shutdowns", _known(r.unsafe_shutdowns, lambda n: f"{n:,}"),
            "Lost power without being shut down properly, often from unplugging without ejecting",
        ))
        rows.append(value_row("Media errors", _known(r.media_errors, lambda n: f"{n:,}"), "Data it couldn't read or write correctly. Should be 0."))
        if r.error_log_entries is not None:
            rows.append(value_row(
                "Error log entries", f"{r.error_log_entries:,}",
                "Commands the drive logged as failed. A few are often harmless when media errors are 0.",
            ))
        if r.critical_warning:
            rows.append(value_row("Critical warning flags", f"0x{r.critical_warning:02x}", "The drive is reporting a serious problem"))
        rows.append(value_row("USB bridge type", r.bridge_type, "How DriveScope reaches this drive's health data"))
        if silent:
            rows.append(value_row("Temperature during benchmarks", "On", "Recorded without asking for approval each time"))
        else:
            rows.append(value_row(
                "Temperature during benchmarks", "Not set up",
                f"Tracking it needs one system file, so it won't ask each time. To add it, run: {SETUP_COMMAND}",
            ))
        return rows

    # --- logged problems ---

    def _read_problems(self) -> None:
        drive = self.drive
        if not drive:
            return

        def work():
            counters = health.read_counters(drive.name)
            events = health.read_kernel_events(drive.name, drive.link.port if drive.link else "")
            on_ui(self._problems_done, drive, counters, events)

        threading.Thread(target=work, daemon=True).start()

    def _problems_done(self, drive, counters, events) -> None:
        if self.drive is None or drive.stable_id != self.drive.stable_id:
            return
        clear_group(self.problem_group, self._problem_rows)
        rows = []
        errors = [e for e in events if e.kind == "error"]
        resets = [e for e in events if e.kind == "reset"]
        disconnects = [e for e in events if e.kind == "disconnect"]
        rows.append(value_row("I/O errors in the kernel log", f"{len(errors):,}", "Should be 0"))
        rows.append(value_row("USB resets in the kernel log", f"{len(resets):,}", "Repeated resets usually mean a poor cable or port"))
        rows.append(value_row("Disconnects", f"{len(disconnects):,}"))
        rows.append(value_row("Command timeouts", _known(counters.timeouts, lambda n: f"{n:,}"), "Should be 0"))
        rows.append(value_row(
            "Commands rejected", _known(counters.rejected_or_failed, lambda n: f"{n:,}"),
            "Mostly harmless. Reading health data raises this, because some query types are unsupported.",
        ))
        for event in [e for e in events if e.kind != "disconnect"][-8:]:
            row = Adw.ActionRow(title=GLib.markup_escape_text(event.message), subtitle=event.timestamp)
            row.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic"))
            rows.append(row)
        for row in rows:
            self.problem_group.add(row)
        self._problem_rows.extend(rows)
