"""Human-readable values. Decimal units, matching how drives and USB are rated."""

from __future__ import annotations


def _scaled(value: float, units: tuple[str, ...]) -> tuple[float, str]:
    for unit in units[:-1]:
        if abs(value) < 1000:
            return value, unit
        value /= 1000
    return value, units[-1]


def _trim(value: float) -> str:
    if abs(value) >= 100:
        return f"{value:.0f}"
    if abs(value) >= 10:
        return f"{value:.1f}"
    return f"{value:.2f}"


def rate(bps: float) -> str:
    if bps < 1:
        return "0 MB/s"
    if bps < 1_000_000:
        return f"{bps / 1000:.0f} KB/s"
    value, unit = _scaled(bps / 1_000_000, ("MB/s", "GB/s"))
    return f"{_trim(value)} {unit}"


def size(n: float) -> str:
    if n < 1000:
        return f"{n:.0f} B"
    value, unit = _scaled(n / 1000, ("KB", "MB", "GB", "TB"))
    return f"{_trim(value)} {unit}"


def count(n: float) -> str:
    if n < 1000:
        return f"{n:.0f}"
    value, unit = _scaled(n / 1000, ("K", "M"))
    return f"{_trim(value)}{unit}"


def latency(ms: float) -> str:
    if ms <= 0:
        return "0 ms"
    if ms < 1:
        return f"{ms * 1000:.0f} µs"
    return f"{_trim(ms)} ms"


def depth(q: float) -> str:
    return f"{q:.1f}" if q < 100 else f"{q:.0f}"


def percent(p: float) -> str:
    return f"{p:.0f}%"


def ago(seconds: float) -> str:
    if seconds < 1:
        return "now"
    if seconds < 60:
        return f"{seconds:.0f} s ago"
    return f"{seconds / 60:.1f}".removesuffix(".0") + " min ago"
