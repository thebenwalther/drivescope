"""Chart colors, validated for color-blind separation against Adwaita's surfaces.

Dark: #3987e5,#d95926,#199e70 on #222226. Light: #2a78d6,#eb6834,#1baf7a on #fafafb.
Colors follow what they mean: read is always blue and write always orange, in
every chart. Measures of the drive as a whole, which belong to neither, use aqua.
"""

from __future__ import annotations

import gi

gi.require_version("Adw", "1")
from gi.repository import Adw

from .. import APP_ID

_SERIES = {
    "read": {"dark": "#3987e5", "light": "#2a78d6"},
    "write": {"dark": "#d95926", "light": "#eb6834"},
    "drive": {"dark": "#199e70", "light": "#1baf7a"},
}
_SURFACE = {"dark": "#222226", "light": "#fafafb"}

CSS = """
.ds-value { font-size: 2.1rem; font-weight: 700; }
.ds-chart-title { font-weight: 700; }
.ds-mini-value { font-size: 1.15rem; font-weight: 600; font-feature-settings: "tnum"; }
.ds-strip { padding: 10px 16px; }
.ds-section { padding: 6px 16px 18px 16px; }
flowbox.ds-section > flowboxchild { padding: 0; background: none; }
.ds-table-head { font-weight: 600; font-size: 0.85rem; }
.ds-table-cell { font-feature-settings: "tnum"; }
"""


def _mode() -> str:
    return "dark" if Adw.StyleManager.get_default().get_dark() else "light"


def app_icon() -> str:
    """The logo version for the current theme. The launcher always uses the plain name."""
    return APP_ID if _mode() == "dark" else f"{APP_ID}-light"


def _rgb(hex_color: str) -> tuple[float, float, float]:
    h = hex_color.lstrip("#")
    return tuple(int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))


def series_rgb(key: str) -> tuple[float, float, float]:
    return _rgb(_SERIES[key][_mode()])


def surface_rgb() -> tuple[float, float, float]:
    return _rgb(_SURFACE[_mode()])
