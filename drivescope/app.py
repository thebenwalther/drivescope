"""Application entry point."""

from __future__ import annotations

import argparse
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, Gtk

from . import APP_ID, DATA_DIR
from .ui import theme
from .ui.window import MainWindow


class DriveScopeApp(Adw.Application):
    def __init__(self, include_internal: bool) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.include_internal = include_internal

    def do_activate(self) -> None:
        window = self.get_active_window()
        if window is None:
            provider = Gtk.CssProvider()
            provider.load_from_string(theme.CSS)
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
            # So the logo also shows when running from the source folder, uninstalled.
            Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(str(DATA_DIR / "icons"))
            Gtk.Window.set_default_icon_name(APP_ID)
            window = MainWindow(self, self.include_internal)
        window.present()


def main() -> int:
    parser = argparse.ArgumentParser(prog="drivescope", description="External drive speed and health.")
    parser.add_argument(
        "--include-internal", action="store_true",
        help="also list internal and loop drives. Write tests stay refused on system volumes.",
    )
    args, gtk_args = parser.parse_known_args()
    return DriveScopeApp(args.include_internal).run([sys.argv[0], *gtk_args])


if __name__ == "__main__":
    sys.exit(main())
