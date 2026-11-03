"""The History tab: past runs for this drive, comparison and export."""

from __future__ import annotations

import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk

from ..core import format as fmt, store
from ..core.devices import Drive
from .bench import RESULT_HEADERS, result_cells
from .widgets import label, table


def compare_rows(older: list[store.Result], newer: list[store.Result]) -> list[list[str]]:
    """Speed for each test in both runs. Tests in only one run still get a row."""
    old = {r.test_id: r for r in older}
    new = {r.test_id: r for r in newer}
    rows = []
    for test_id in [*old, *[k for k in new if k not in old]]:
        a, b = old.get(test_id), new.get(test_id)
        name = (b or a).label
        a_bps, b_bps = (a.bps if a else None), (b.bps if b else None)
        change = f"{(b_bps / a_bps - 1) * 100:+.0f}%" if a_bps and b_bps else "–"
        rows.append([name, fmt.rate(a_bps) if a_bps else "–", fmt.rate(b_bps) if b_bps else "–", change])
    return rows


class HistoryPage(Gtk.ScrolledWindow):
    def __init__(self, store_: store.Store, on_message) -> None:
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.store = store_
        self.on_message = on_message
        self.drive: Drive | None = None
        self._checks: dict[int, Gtk.CheckButton] = {}
        self._rows: list = []
        self._selected: list[int] = []

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, css_classes=["ds-section"])
        root.set_margin_top(12)
        self.set_child(root)

        self.alert_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        root.append(self.alert_box)

        self.runs_group = Adw.PreferencesGroup(
            title="Past runs", description="Tick one to see its results, or two to compare them.",
        )
        export = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        for text, kind in (("Export CSV", "csv"), ("Export JSON", "json")):
            button = Gtk.Button(label=text)
            button.connect("clicked", lambda _b, k=kind: self._export(k))
            export.append(button)
        self.export_box = export
        self.runs_group.set_header_suffix(export)
        root.append(self.runs_group)

        self.empty = Adw.StatusPage(
            icon_name="document-open-recent-symbolic", title="No saved results for this drive yet",
            description="Every benchmark you finish is saved here, so you can spot a drive slowing down over time.",
        )
        root.append(self.empty)

        self.detail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        root.append(self.detail)

    def set_drive(self, drive: Drive | None) -> None:
        changed = not (drive and self.drive and drive.stable_id == self.drive.stable_id)
        self.drive = drive
        if changed:
            self.reload()

    def reload(self) -> None:
        for row in self._rows:
            self.runs_group.remove(row)
        self._rows = []
        self._checks = {}
        self._selected = []
        self._clear(self.alert_box)
        self._clear(self.detail)
        runs = self.store.runs(self.drive.stable_id) if self.drive else []
        self.empty.set_visible(not runs)
        self.runs_group.set_visible(bool(runs))
        for run in runs:
            row = self._run_row(run)
            self.runs_group.add(row)
            self._rows.append(row)
        if runs:
            self._checks[runs[0].id].set_active(True)
            summary = store.summarize_alerts(self.store.slow_alerts(runs[0].id))
            if summary:
                self.alert_box.append(label(f"In the latest run, {summary[0].lower()}{summary[1:]}", ["warning"], wrap=True))

    @staticmethod
    def _clear(box: Gtk.Box) -> None:
        child = box.get_first_child()
        while child:
            next_child = child.get_next_sibling()
            box.remove(child)
            child = next_child

    def _run_row(self, run: store.Run) -> Adw.ActionRow:
        results = self.store.results(run.id)
        best = max((r.bps for r in results if r.bps), default=0)
        when = time.strftime("%b %-d, %Y at %-I:%M %p", time.localtime(run.started_at))
        link = f"{run.link_mbps / 1000:g} Gbps {run.protocol or ''}".strip() if run.link_mbps else ""
        parts = [p for p in (run.volume, run.fstype, link) if p]
        row = Adw.ActionRow(
            title=GLib.markup_escape_text(when),
            subtitle=GLib.markup_escape_text(" · ".join(parts)),
        )
        check = Gtk.CheckButton(valign=Gtk.Align.CENTER, tooltip_text="Show or compare this run")
        check.connect("toggled", lambda c, i=run.id: self._toggled(i, c.get_active()))
        row.add_prefix(check)
        row.set_activatable_widget(check)
        row.add_suffix(label(f"Best {fmt.rate(best)}", ["numeric", "dim-label"]))
        delete = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"], tooltip_text="Delete this run")
        delete.update_property([Gtk.AccessibleProperty.LABEL], ["Delete this run"])
        delete.connect("clicked", lambda _b, r=run, w=when: self._confirm_delete(r, w))
        row.add_suffix(delete)
        self._checks[run.id] = check
        return row

    def _toggled(self, run_id: int, active: bool) -> None:
        if active and run_id not in self._selected:
            self._selected.append(run_id)
            if len(self._selected) > 2:
                oldest_pick = self._selected[0]
                self._checks[oldest_pick].set_active(False)  # re-enters and removes it
        elif not active and run_id in self._selected:
            self._selected.remove(run_id)
        self._show_detail()

    def _show_detail(self) -> None:
        self._clear(self.detail)
        if len(self._selected) == 1:
            results = self.store.results(self._selected[0])
            self.detail.append(label("Results", ["title-4"]))
            self.detail.append(table(RESULT_HEADERS, [result_cells(r) for r in results]))
            for r in results:
                if r.detail:
                    self.detail.append(label(f"{r.label}: {r.detail}", ["dim-label", "caption"], wrap=True))
        elif len(self._selected) == 2:
            runs = {r.id: r for r in self.store.runs(self.drive.stable_id)}
            older, newer = sorted(self._selected, key=lambda i: runs[i].started_at)
            stamp = lambda i: time.strftime("%b %-d, %-I:%M %p", time.localtime(runs[i].started_at))
            self.detail.append(label("Comparison", ["title-4"]))
            if runs[older].link_mbps != runs[newer].link_mbps:
                self.detail.append(label(
                    "These runs used different USB link speeds, so a difference may be the connection and not the drive.",
                    ["warning"], wrap=True,
                ))
            headers = [("Test", ""), (stamp(older), ""), (stamp(newer), ""), ("Change", "Newer compared to older")]
            self.detail.append(table(headers, compare_rows(self.store.results(older), self.store.results(newer))))

    def _confirm_delete(self, run: store.Run, when: str) -> None:
        dialog = Adw.AlertDialog(heading="Delete this run?", body=f"The results from {when} will be permanently removed.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def respond(_d, response):
            if response == "delete":
                self.store.delete_run(run.id)
                self.reload()

        dialog.connect("response", respond)
        dialog.present(self.get_root())

    def _export(self, kind: str) -> None:
        runs = [r.id for r in self.store.runs(self.drive.stable_id)]
        ids = self._selected or runs
        text = self.store.export_csv(ids) if kind == "csv" else self.store.export_json(ids)
        dialog = Gtk.FileDialog(title=f"Export {kind.upper()}", initial_name=f"drivescope-results.{kind}")

        def done(source, result):
            try:
                target = source.save_finish(result)
            except GLib.Error:
                return  # the user closed the dialog
            try:
                target.replace_contents(text.encode(), None, False, Gio.FileCreateFlags.REPLACE_DESTINATION, None)
                self.on_message(f"Exported {len(ids)} run{'s' if len(ids) != 1 else ''} to {target.get_basename()}.")
            except GLib.Error as e:
                self.on_message(f"Couldn't save the file: {e.message}")

        dialog.save(self.get_root(), None, done)
