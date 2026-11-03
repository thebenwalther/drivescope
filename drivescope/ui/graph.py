"""A line chart drawn with cairo: thin lines, hairline grid, crosshair on hover."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gtk, Pango, PangoCairo

from . import theme

MIN_LEFT_PAD = 36
AXIS_GAP = 8
RIGHT_PAD = 10
TOP_PAD = 8
BOTTOM_PAD = 26
LINE_WIDTH = 2.0


def nice_max(peak: float, floor: float) -> float:
    """The next round number above the peak, so the axis doesn't jitter."""
    target = max(peak * 1.1, floor)
    magnitude = 10 ** math.floor(math.log10(target))
    for step in (1, 1.25, 1.5, 2, 2.5, 4, 5, 8, 10):
        if step * magnitude >= target:
            return step * magnitude
    return 10 * magnitude


@dataclass
class Series:
    name: str
    color_key: str
    values: list[float]


class LineChart(Gtk.DrawingArea):
    """Plots evenly spaced samples, newest at the right, with one y axis."""

    def __init__(
        self,
        series: list[tuple[str, str]],
        formatter: Callable[[float], str],
        floor: float,
        capacity: int,
        interval_s: float,
        time_label: Callable[[float], str],
        description: str,
        height: int = 120,
        fixed_max: float | None = None,
        fixed_min: float = 0.0,
    ) -> None:
        super().__init__()
        self.series = [Series(name, key, []) for name, key in series]
        self.formatter = formatter
        self.floor = floor
        self.capacity = capacity
        self.interval_s = interval_s
        self.time_label = time_label
        self.fixed_max = fixed_max
        self.fixed_min = fixed_min  # above 0 only where 0 means nothing, like temperature
        self.reference: tuple[float, str] | None = None
        self.axis_labels: tuple[str, str] | None = None
        self._hover_x: float | None = None
        self._left = MIN_LEFT_PAD

        self.set_content_height(height)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)
        self.set_accessible_role(Gtk.AccessibleRole.IMG)
        self.update_property([Gtk.AccessibleProperty.LABEL], [description])

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", lambda _c, x, _y: self._set_hover(x))
        motion.connect("leave", lambda _c: self._set_hover(None))
        self.add_controller(motion)

    def _set_hover(self, x: float | None) -> None:
        self._hover_x = x
        self.queue_draw()

    def set_values(self, values: list[list[float]]) -> None:
        for s, v in zip(self.series, values):
            s.values = v
        self.queue_draw()

    def set_axis(self, capacity: int, interval_s: float, left: str, right: str, hover_label) -> None:
        """Use a fixed span instead of a rolling "now" window, for finished runs."""
        self.capacity = max(2, capacity)
        self.interval_s = interval_s
        self.axis_labels = (left, right)
        self.time_label = hover_label
        self.queue_draw()

    def set_reference(self, value: float | None, label: str = "") -> None:
        """A labelled horizontal limit, drawn only when it fits the current scale."""
        self.reference = (value, label) if value else None
        self.queue_draw()

    def _layout(self, cr, text, bold=False):
        layout = PangoCairo.create_layout(cr)
        desc = self.get_pango_context().get_font_description().copy()
        desc.set_size(int(desc.get_size() * 0.82))
        if bold:
            desc.set_weight(Pango.Weight.BOLD)
        layout.set_font_description(desc)
        layout.set_text(text, -1)
        return layout

    def _text(self, cr, x, y, text, fg, alpha=1.0, align_right=False, bold=False, center=False):
        layout = self._layout(cr, text, bold)
        w, h = layout.get_pixel_size()
        if align_right:
            x -= w
        elif center:
            x -= w / 2
        cr.set_source_rgba(fg.red, fg.green, fg.blue, alpha)
        cr.move_to(x, y)
        PangoCairo.show_layout(cr, layout)
        return w, h

    def _draw(self, _area, cr, width, height) -> None:
        fg = self.get_color()
        plot_h = max(1, height - TOP_PAD - BOTTOM_PAD)
        peak = max((max(s.values, default=0) for s in self.series), default=0)
        y_max = self.fixed_max or nice_max(peak, self.floor)
        # The margin follows the widest axis label, which changes with the scale.
        y_min = self.fixed_min
        y_span = max(y_max - y_min, 1e-9)
        tick_values = (y_min, y_min + y_span * 0.5, y_max)
        widest = max(self._layout(cr, self.formatter(v)).get_pixel_size()[0] for v in tick_values)
        left = max(MIN_LEFT_PAD, widest + AXIS_GAP)
        self._left = left
        plot_w = max(1, width - left - RIGHT_PAD)

        def to_y(v: float) -> float:
            return TOP_PAD + plot_h * (1 - (min(max(v, y_min), y_max) - y_min) / y_span)

        def to_x(i: int, n: int) -> float:
            return left + plot_w - (n - 1 - i) / (self.capacity - 1) * plot_w

        # Grid: solid hairlines that stay behind the data.
        cr.set_line_width(1)
        for frac in (0, 0.25, 0.5, 0.75, 1):
            value = y_min + y_span * frac
            y = round(to_y(value)) + 0.5
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.22 if frac == 0 else 0.09)
            cr.move_to(left, y)
            cr.line_to(left + plot_w, y)
            cr.stroke()
            if frac in (0, 0.5, 1):
                self._text(cr, left - AXIS_GAP, y - 8, self.formatter(value), fg, 0.72, align_right=True)

        span = (self.capacity - 1) * self.interval_s
        left_label, right_label = self.axis_labels or (self.time_label(span), "now")
        self._text(cr, left, TOP_PAD + plot_h + 5, left_label, fg, 0.72)
        self._text(cr, left + plot_w, TOP_PAD + plot_h + 5, right_label, fg, 0.72, align_right=True)

        if self.reference and y_min <= self.reference[0] <= y_max:
            y = round(to_y(self.reference[0])) + 0.5
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.5)
            cr.move_to(left, y)
            cr.line_to(left + plot_w, y)
            cr.stroke()
            self._text(cr, left + 6, y + 2, self.reference[1], fg, 0.8)

        # Lines, clipped to the plot so round joins can't bleed into the axis.
        cr.save()
        cr.rectangle(left, 0, plot_w + 2, TOP_PAD + plot_h + LINE_WIDTH)
        cr.clip()
        cr.set_line_width(LINE_WIDTH)
        cr.set_line_join(1)
        cr.set_line_cap(1)
        for s in self.series:
            n = len(s.values)
            if n < 2:
                continue
            cr.set_source_rgb(*theme.series_rgb(s.color_key))
            for i, v in enumerate(s.values):
                x, y = to_x(i, n), to_y(v)
                if i == 0:
                    cr.move_to(x, y)
                else:
                    cr.line_to(x, y)
            cr.stroke()
        cr.restore()

        self._draw_hover(cr, fg, width, plot_h, plot_w, to_x, to_y)

    def _draw_hover(self, cr, fg, width, plot_h, plot_w, to_x, to_y) -> None:
        n = max((len(s.values) for s in self.series), default=0)
        if self._hover_x is None or n == 0:
            return
        steps_back = round((self._left + plot_w - self._hover_x) / plot_w * (self.capacity - 1))
        if steps_back < 0 or steps_back >= n:
            return
        i = n - 1 - steps_back
        x = to_x(i, n)

        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.4)
        cr.set_line_width(1)
        cr.move_to(round(x) + 0.5, TOP_PAD)
        cr.line_to(round(x) + 0.5, TOP_PAD + plot_h)
        cr.stroke()

        surface = theme.surface_rgb()
        for s in self.series:
            if i < len(s.values):
                y = to_y(s.values[i])
                cr.set_source_rgb(*surface)  # a ring keeps overlapping dots distinct
                cr.arc(x, y, 6, 0, 2 * math.pi)
                cr.fill()
                cr.set_source_rgb(*theme.series_rgb(s.color_key))
                cr.arc(x, y, 4, 0, 2 * math.pi)
                cr.fill()

        rows = [(s, s.values[i]) for s in self.series if i < len(s.values)]
        box_w = 168
        box_h = 26 + 19 * len(rows)
        box_x = x + 12 if x + 12 + box_w < width else x - 12 - box_w
        box_y = TOP_PAD + 2
        _rounded(cr, box_x, box_y, box_w, box_h, 8)
        cr.set_source_rgba(*surface, 0.96)
        cr.fill_preserve()
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.2)
        cr.stroke()
        self._text(cr, box_x + 10, box_y + 6, self.time_label(steps_back * self.interval_s), fg, 0.72)
        for row, (s, value) in enumerate(rows):
            ry = box_y + 25 + 19 * row
            cr.set_source_rgb(*theme.series_rgb(s.color_key))
            _rounded(cr, box_x + 10, ry + 4, 8, 8, 2)
            cr.fill()
            self._text(cr, box_x + 24, ry, s.name, fg, 0.85)
            self._text(cr, box_x + box_w - 10, ry, self.formatter(value), fg, 1.0, align_right=True, bold=True)


def _rounded(cr, x, y, w, h, r) -> None:
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


class Swatch(Gtk.DrawingArea):
    """The colored mark beside a label, so identity is never carried by text color."""

    def __init__(self, color_key: str) -> None:
        super().__init__()
        self.set_content_width(10)
        self.set_content_height(10)
        self.set_valign(Gtk.Align.CENTER)
        self.set_draw_func(lambda _a, cr, w, h: self._paint(cr, w, h, color_key))

    @staticmethod
    def _paint(cr, w, h, color_key) -> None:
        cr.set_source_rgb(*theme.series_rgb(color_key))
        _rounded(cr, 0, 0, w, h, 3)
        cr.fill()
