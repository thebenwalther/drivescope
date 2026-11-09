"""Main window: drive picker, connection summary, and the tabs."""

from __future__ import annotations

import subprocess
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk, Pango

from .. import DATA_DIR, VERSION
from ..core import devices, format as fmt, safety, store
from ..core.devices import Drive
from . import theme
from .bench import BenchPage
from .healthpage import HealthPage
from .history import HistoryPage
from .live import LivePage

POLL_MS = 2000


def connection_summary(drive: Drive) -> str:
    parts = [fmt.size(drive.size)]
    if drive.link:
        parts.append(f"{drive.link.speed_mbps / 1000:g} Gbps USB {drive.link.version}")
        parts.append(drive.link.protocol)
    else:
        parts.append(drive.transport.upper())
    if drive.queue_depth:
        parts.append(f"queue depth {drive.queue_depth}")
    mounted = drive.mounted
    if mounted and all(p.mount_read_only for p in mounted):
        parts.append("read-only")
    elif not mounted:
        parts.append("not mounted")
    return " · ".join(parts)


CONNECTION_HELP = (
    "Gbps is the speed the USB link negotiated, which caps what the drive can do.\n"
    "UAS is the fast USB storage protocol. BOT is the older, slower one.\n"
    "Queue depth is how many operations the drive accepts at once."
)


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app, include_internal: bool) -> None:
        super().__init__(application=app, title="DriveScope", default_width=1200, default_height=860)
        self.set_size_request(460, 420)
        self.include_internal = include_internal
        self.drives: list[Drive] = []
        self._fingerprint = None
        self._polling = False
        self.store = store.Store()
        self._cleaned_mounts: set[str] = set()

        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)
        toolbar = Adw.ToolbarView()
        self.toasts.set_child(toolbar)

        header = Adw.HeaderBar()
        self.picker_model = Gtk.StringList()
        self.picker = Gtk.DropDown(model=self.picker_model, tooltip_text="Choose a drive")
        self.picker.connect("notify::selected", lambda *_: self._apply_selection())
        logo = Gtk.Image(pixel_size=24, margin_start=4, margin_end=2)
        style = Adw.StyleManager.get_default()
        style.connect("notify::dark", lambda *_: logo.set_from_icon_name(theme.app_icon()))
        logo.set_from_icon_name(theme.app_icon())
        header.pack_start(logo)
        header.pack_start(self.picker)
        about = Gio.SimpleAction(name="about")
        about.connect("activate", lambda *_: self._show_about())
        self.add_action(about)
        menu = Gio.Menu()
        menu.append("About DriveScope", "win.about")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Main menu"))
        self.view_stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher(stack=self.view_stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)
        toolbar.add_top_bar(header)
        self.switcher_bar = Adw.ViewSwitcherBar(stack=self.view_stack)
        toolbar.add_bottom_bar(self.switcher_bar)
        # In a narrow tile the tabs move to the bottom, where they have room.
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 900sp"))
        narrow.add_setter(self.switcher_bar, "reveal", True)
        narrow.add_setter(header, "title-widget", Gtk.Label(label="DriveScope", css_classes=["heading"]))
        self.add_breakpoint(narrow)

        self.link_bar = Gtk.Box(spacing=8, css_classes=["ds-strip", "toolbar"], tooltip_text=CONNECTION_HELP)
        self.link_label = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        self.link_bar.append(self.link_label)
        self.warning_banner = Adw.Banner(revealed=False)
        toolbar.add_top_bar(self.warning_banner)
        toolbar.add_top_bar(self.link_bar)

        self.live = LivePage(self.show_message)
        self.view_stack.add_titled_with_icon(self.live, "live", "Live", "utilities-system-monitor-symbolic")
        self.history = HistoryPage(self.store, self.show_message)
        self.bench = BenchPage(self.store, self.show_message, self._running_changed, self.history.reload)
        self.view_stack.add_titled_with_icon(self.bench, "bench", "Benchmarks", "speedometer-symbolic")
        self.health = HealthPage(self.store, self.show_message)
        self.view_stack.add_titled_with_icon(self.health, "health", "Health", "emblem-ok-symbolic")
        self.view_stack.add_titled_with_icon(self.history, "history", "History", "document-open-recent-symbolic")

        self.empty = self._build_empty_state()
        self.outer = Gtk.Stack()
        self.outer.add_named(self.view_stack, "drive")
        self.outer.add_named(self.empty, "empty")
        toolbar.set_content(self.outer)

        self.connect("close-request", self._on_close)
        self.refresh()
        GLib.timeout_add(POLL_MS, self.refresh)

    @staticmethod
    def _build_empty_state() -> Gtk.Widget:
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=8,
            halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, margin_start=24, margin_end=24,
        )
        wordmark = Gtk.Picture(alternative_text="DriveScope", content_fit=Gtk.ContentFit.CONTAIN, margin_bottom=20)

        def match_style(manager, *_):
            name = "wordmark-dark-tight.png" if manager.get_dark() else "wordmark-tight.png"
            wordmark.set_filename(str(DATA_DIR / "brand" / name))

        style = Adw.StyleManager.get_default()
        style.connect("notify::dark", match_style)
        match_style(style)
        # A picture asks for its full pixel size, so the clamp sets its real width.
        box.append(Adw.Clamp(child=wordmark, maximum_size=400, tightening_threshold=400))
        box.append(Gtk.Label(label="No external drive connected", css_classes=["title-2"], wrap=True, justify=Gtk.Justification.CENTER))
        box.append(Gtk.Label(
            label="Plug in a USB drive and it will appear here automatically.",
            css_classes=["dim-label"], wrap=True, justify=Gtk.Justification.CENTER,
        ))
        return box

    def _show_about(self) -> None:
        Adw.AboutDialog(
            application_name="DriveScope",
            application_icon=theme.app_icon(),
            version=VERSION,
            comments="Real-world speed and health of external drives.",
        ).present(self)

    def _on_close(self, _window) -> bool:
        if not self.bench.running:
            return False
        # Leaving now would strand fio and its test files, so finish cleaning up first.
        self.set_visible(False)
        self.bench.stop_then(self.close)
        return True

    def _running_changed(self, running: bool) -> None:
        self.picker.set_sensitive(not running)

    def _remove_orphans(self, found: list[Drive]) -> None:
        """Remove test files a crash left behind, once per volume each session."""
        if self.bench.running:
            return
        for drive in found:
            for part in drive.mounted:
                if part.mountpoint in self._cleaned_mounts or part.mount_read_only:
                    continue
                self._cleaned_mounts.add(part.mountpoint)
                if safety.write_block_reason(part.mountpoint) is None:
                    threading.Thread(target=self._clean_volume, args=(part.mountpoint,), daemon=True).start()

    def _clean_volume(self, mountpoint: str) -> None:
        for path in safety.find_orphaned_test_dirs(mountpoint):
            try:
                safety.remove_orphaned_test_dir(path)
            except (OSError, safety.UnsafeTarget):
                continue
            GLib.idle_add(self._show_once, f"Removed test files left behind on {mountpoint}.")

    def _show_once(self, text: str) -> bool:
        self.show_message(text)
        return GLib.SOURCE_REMOVE

    def show_message(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=6))

    def refresh(self) -> bool:
        if not self._polling:
            self._polling = True
            threading.Thread(target=self._poll_worker, daemon=True).start()
        return GLib.SOURCE_CONTINUE

    def _poll_worker(self) -> None:
        try:
            found = devices.list_drives(self.include_internal)
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            GLib.idle_add(self._poll_failed, str(e))
            return
        GLib.idle_add(self._poll_done, found)

    def _poll_failed(self, error: str) -> bool:
        self._polling = False
        self.show_message(f"Couldn't list drives: {error}")
        return GLib.SOURCE_REMOVE

    def _poll_done(self, found: list[Drive]) -> bool:
        self._polling = False
        self._remove_orphans(found)
        # Free space changes constantly and doesn't warrant rebuilding anything.
        fingerprint = [
            (d.stable_id, d.name, d.link, tuple((p.name, p.mountpoint, p.mount_read_only) for p in d.partitions))
            for d in found
        ]
        if fingerprint == self._fingerprint:
            return GLib.SOURCE_REMOVE
        self._fingerprint = fingerprint

        previous = self.current_drive()
        self.drives = found
        self.picker_model.splice(0, self.picker_model.get_n_items(), [
            d.display_name if d.display_name == d.name else f"{d.display_name} ({d.name})" for d in found
        ])
        index = 0
        if previous:
            for i, d in enumerate(found):
                if d.stable_id == previous.stable_id:
                    index = i
        if found:
            self.picker.set_selected(index)
        self._apply_selection()
        return GLib.SOURCE_REMOVE

    def current_drive(self) -> Drive | None:
        i = self.picker.get_selected()
        return self.drives[i] if 0 <= i < len(self.drives) else None

    def _apply_selection(self) -> None:
        drive = self.current_drive()
        has = drive is not None
        self.outer.set_visible_child_name("drive" if has else "empty")
        self.picker.set_visible(has)
        self.link_bar.set_visible(has)
        self.live.set_drive(drive)
        self.bench.set_drive(drive)
        self.health.set_drive(drive)
        self.history.set_drive(drive)
        if not has:
            self.warning_banner.set_revealed(False)
            return
        self.link_label.set_text(connection_summary(drive))
        warnings = drive.link.warnings() if drive.link else []
        self.warning_banner.set_title(GLib.markup_escape_text(" ".join(warnings)))
        self.warning_banner.set_revealed(bool(warnings))
