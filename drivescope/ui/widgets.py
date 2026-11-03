"""Small shared pieces of UI."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Pango


def on_ui(func, *args) -> None:
    """Run a function on the main thread, from a worker thread."""

    def call():
        func(*args)
        return GLib.SOURCE_REMOVE

    GLib.idle_add(call)


def label(text="", classes=(), xalign=0.0, wrap=False, tooltip=None) -> Gtk.Label:
    widget = Gtk.Label(label=text, xalign=xalign, css_classes=list(classes))
    if wrap:
        widget.set_wrap(True)
        widget.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    if tooltip:
        widget.set_tooltip_text(tooltip)
    return widget


def value_row(title: str, value: str, subtitle: str = "") -> Adw.ActionRow:
    """A row with a right-aligned value. Titles here are plain text, not markup."""
    row = Adw.ActionRow(title=GLib.markup_escape_text(title))
    if subtitle:
        row.set_subtitle(GLib.markup_escape_text(subtitle))
    text = label(value, ["numeric"], xalign=1.0)
    text.set_selectable(True)
    row.add_suffix(text)
    return row


def clear_group(group: Adw.PreferencesGroup, rows: list) -> None:
    for row in rows:
        group.remove(row)
    rows.clear()


def table(headers: list[tuple[str, str]], rows: list[list[str]]) -> Gtk.Widget:
    """A data table that scrolls sideways in a narrow window. The numbers are
    right-aligned so they can be compared down each column."""
    grid = Gtk.Grid(column_spacing=18, row_spacing=7)
    for col, (title, tip) in enumerate(headers):
        grid.attach(
            label(title, ["ds-table-head", "dim-label"], xalign=0.0 if col == 0 else 1.0, tooltip=tip or None),
            col, 0, 1, 1,
        )
    for r, values in enumerate(rows, start=1):
        for col, value in enumerate(values):
            cell = label(value, ["ds-table-cell"], xalign=0.0 if col == 0 else 1.0)
            cell.set_hexpand(col == 0)
            grid.attach(cell, col, r, 1, 1)
    scroller = Gtk.ScrolledWindow(
        vscrollbar_policy=Gtk.PolicyType.NEVER, hscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        propagate_natural_height=True,
    )
    scroller.set_child(grid)
    return scroller
