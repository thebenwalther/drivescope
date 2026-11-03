"""What's limiting this drive, stated only as far as the measurements support."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median

from . import format as fmt

LINK_SATURATED = 0.70  # protocol overhead keeps real USB transfers well below the raw rate
CACHE_DROP_RATIO = 0.70
MIN_CURVE_POINTS = 20
HEAT_RISE_C = 10


@dataclass(frozen=True)
class Finding:
    kind: str  # "link", "source", "cache", "heat", "queue" or "inconclusive"
    title: str
    evidence: str


@dataclass(frozen=True)
class CacheDrop:
    at_seconds: float
    at_bytes: float
    before_bps: float
    after_bps: float


def detect_cache_drop(curve: list[tuple[float, float]]) -> CacheDrop | None:
    """Find a lasting fall in speed in a (seconds, bytes/s) curve.

    Compares the opening fifth of the run to the closing half, so one slow second
    doesn't count. The drop point is the first sample that falls below the
    midpoint and never recovers above it.
    """
    if len(curve) < MIN_CURVE_POINTS:
        return None
    speeds = [s for _, s in curve]
    before = median(speeds[: len(speeds) // 5])
    after = median(speeds[len(speeds) // 2 :])
    if before <= 0 or after / before > CACHE_DROP_RATIO:
        return None
    midpoint = (before + after) / 2
    index = len(speeds) - 1
    while index > 0 and speeds[index - 1] < midpoint:
        index -= 1
    transferred = 0.0
    previous_t = 0.0
    for t, s in curve[:index]:
        transferred += s * (t - previous_t)
        previous_t = t
    return CacheDrop(curve[index][0], transferred, before, after)


def analyze(
    link_ceiling_bps: int | None,
    link_speed_mbps: int | None,
    best_read_bps: float | None,
    best_write_bps: float | None,
    q1_read_bps: float | None = None,
    write_curve: list[tuple[float, float]] | None = None,
    copy_on_disk_bps: float | None = None,
    copy_source_bps: float | None = None,
    temps_c: list[int] | None = None,
) -> list[Finding]:
    findings: list[Finding] = []

    best = max(best_read_bps or 0, best_write_bps or 0)
    if link_ceiling_bps and best >= link_ceiling_bps * LINK_SATURATED:
        share = best / link_ceiling_bps * 100
        findings.append(Finding(
            "link", "The USB connection is the limit",
            f"Fastest result {fmt.rate(best)} is {share:.0f}% of the {link_speed_mbps / 1000:g} Gbps link's "
            f"raw {fmt.rate(link_ceiling_bps)}. Overhead uses up most of the rest.",
        ))

    if q1_read_bps and best_read_bps and q1_read_bps < best_read_bps * 0.8:
        findings.append(Finding(
            "queue", "Simple copies won't reach top speed",
            f"With one request at a time it reads {fmt.rate(q1_read_bps)}, against {fmt.rate(best_read_bps)} "
            "with several queued. Tools that send one request at a time will see the lower figure.",
        ))

    drop = detect_cache_drop(write_curve) if write_curve else None
    if drop:
        findings.append(Finding(
            "cache", "A fast write cache runs out",
            f"Writes held {fmt.rate(drop.before_bps)} for about {fmt.size(drop.at_bytes)} "
            f"({drop.at_seconds:.0f} s), then settled at {fmt.rate(drop.after_bps)}.",
        ))

    if copy_on_disk_bps and copy_source_bps and best_write_bps:
        if copy_source_bps < best_write_bps * 0.8 and copy_on_disk_bps >= copy_source_bps * 0.8:
            findings.append(Finding(
                "source", "The source drive is the limit for copies",
                f"The source reads at {fmt.rate(copy_source_bps)} and the copy reached {fmt.rate(copy_on_disk_bps)}, "
                f"while this drive can write {fmt.rate(best_write_bps)}.",
            ))

    if temps_c and len(temps_c) >= 2 and drop and temps_c[-1] - temps_c[0] >= HEAT_RISE_C:
        findings.append(Finding(
            "heat", "Heat may also be slowing it",
            f"It warmed from {temps_c[0]}°C to {temps_c[-1]}°C during a run where speed fell. "
            "This shows they happened together, not that one caused the other.",
        ))

    if not findings:
        findings.append(Finding(
            "inconclusive", "No clear limit found",
            "These results don't point to the USB link, cache or source drive. "
            "The limit may be the drive's own controller or flash.",
        ))
    return findings
