"""SMART data, kernel-logged problems and error counters."""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

# Tried in order until one returns believable data. USB bridges rarely identify
# themselves, and some of these "succeed" with an empty report.
BRIDGE_TYPES = ("auto", "sat", "sntasmedia", "sntjmicron", "sntrealtek")
NVME_DATA_UNIT_BYTES = 512_000
POLL_ACTION = "dev.drivescope.smartctl"
# A drive that has been idle can miss the first commands while it wakes up.
PROBE_PASSES = 3
PROBE_RETRY_S = 3
_DEVICE_RE = re.compile(r"^/dev/[A-Za-z0-9]+$")
_DOC_MARK = "---DRIVESCOPE-TYPE:"


@dataclass(frozen=True)
class HealthReport:
    bridge_type: str
    model: str
    firmware: str
    passed: bool | None
    temperature_c: int | None
    temperature_warn_c: int | None
    wear_pct: int | None
    spare_pct: int | None
    data_read_bytes: int | None
    data_written_bytes: int | None
    power_on_hours: int | None
    power_cycles: int | None
    unsafe_shutdowns: int | None
    media_errors: int | None
    error_log_entries: int | None
    critical_warning: int | None


def is_believable(doc: dict) -> bool:
    """Reject the empty or zeroed reports some bridges hand back with exit code 0."""
    status = doc.get("smartctl", {}).get("exit_status", 1)
    if status & 0b11:  # bit 0: bad command line, bit 1: couldn't open or identify
        return False
    return bool((doc.get("model_name") or "").strip()) and "smart_status" in doc


def parse_report(doc: dict, bridge_type: str) -> HealthReport:
    nvme = doc.get("nvme_smart_health_information_log") or {}
    attrs = {a.get("name"): a for a in doc.get("ata_smart_attributes", {}).get("table", [])}

    def raw(*names: str) -> int | None:
        for name in names:
            if name in attrs:
                return attrs[name].get("raw", {}).get("value")
        return None

    temperature = doc.get("temperature") or {}
    written = nvme.get("data_units_written")
    read = nvme.get("data_units_read")
    lbas_written = raw("Total_LBAs_Written")
    lbas_read = raw("Total_LBAs_Read")
    return HealthReport(
        bridge_type=bridge_type,
        model=(doc.get("model_name") or "").strip(),
        firmware=(doc.get("firmware_version") or "").strip(),
        passed=doc.get("smart_status", {}).get("passed"),
        temperature_c=temperature.get("current") or nvme.get("temperature") or None,
        temperature_warn_c=temperature.get("op_limit_max"),
        wear_pct=nvme.get("percentage_used"),
        spare_pct=nvme.get("available_spare"),
        data_read_bytes=read * NVME_DATA_UNIT_BYTES if read is not None else (lbas_read * 512 if lbas_read else None),
        data_written_bytes=written * NVME_DATA_UNIT_BYTES if written is not None else (lbas_written * 512 if lbas_written else None),
        power_on_hours=(doc.get("power_on_time") or {}).get("hours"),
        power_cycles=doc.get("power_cycle_count", nvme.get("power_cycles")),
        unsafe_shutdowns=nvme.get("unsafe_shutdowns", raw("Power-Off_Retract_Count", "Unexpected_Power_Loss_Ct")),
        media_errors=nvme.get("media_errors", raw("Reallocated_Sector_Ct")),
        error_log_entries=nvme.get("num_err_log_entries"),
        critical_warning=nvme.get("critical_warning"),
    )


def split_probe_output(text: str) -> list[tuple[str, dict]]:
    """Split the probe script's output into (bridge type, parsed document) pairs."""
    out = []
    for chunk in text.split(_DOC_MARK)[1:]:
        name, _, body = chunk.partition("\n")
        try:
            out.append((name.strip(), json.loads(body)))
        except json.JSONDecodeError:
            continue
    return out


def choose_report(docs: list[tuple[str, dict]]) -> HealthReport | None:
    for name, doc in docs:
        if is_believable(doc):
            return parse_report(doc, name)
    return None


def _checked_device(device: str) -> str:
    if not _DEVICE_RE.match(device):
        raise ValueError(f"Not a device path: {device}")
    return device


def probe(device: str, timeout_s: int = 180) -> HealthReport | None:
    """Try each bridge type under a single authentication prompt. Only reads data."""
    _checked_device(device)
    if can_poll_silently():
        for attempt in range(PROBE_PASSES):
            if attempt:
                time.sleep(PROBE_RETRY_S)
            for bridge in BRIDGE_TYPES:
                try:
                    report = read_report(device, bridge)
                except subprocess.SubprocessError:
                    continue
                if report:
                    return report
        return None
    script = (
        'for t in ' + " ".join(BRIDGE_TYPES) + '; do '
        f'echo "{_DOC_MARK}$t"; '
        'timeout 15 /usr/bin/smartctl -j -a -d "$t" "$1"; '
        'done'
    )
    done = subprocess.run(
        ["pkexec", "/bin/sh", "-c", script, "drivescope", device],
        capture_output=True, text=True, timeout=timeout_s,
    )
    if done.returncode in (126, 127):
        raise PermissionError("Authentication was cancelled or isn't available.")
    return choose_report(split_probe_output(done.stdout))


def read_report(device: str, bridge_type: str, timeout_s: int = 30) -> HealthReport | None:
    """Re-read using a known bridge type. Silent once the polkit action is installed."""
    _checked_device(device)
    if bridge_type not in BRIDGE_TYPES:
        raise ValueError(f"Unknown bridge type: {bridge_type}")
    done = subprocess.run(
        ["pkexec", "/usr/bin/smartctl", "-j", "-a", "-d", bridge_type, device],
        capture_output=True, text=True, timeout=timeout_s,
    )
    if done.returncode in (126, 127):
        return None
    try:
        doc = json.loads(done.stdout)
    except json.JSONDecodeError:
        return None
    return parse_report(doc, bridge_type) if is_believable(doc) else None


def can_poll_silently() -> bool:
    """True when the optional polkit action lets temperature be read without a prompt."""
    try:
        done = subprocess.run(
            ["pkcheck", "--action-id", POLL_ACTION, "--process", str(_self_pid())],
            capture_output=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def _self_pid() -> int:
    import os

    return os.getpid()


@dataclass(frozen=True)
class CommandCounters:
    rejected_or_failed: int | None  # includes harmless unsupported commands
    timeouts: int | None
    requests: int | None
    completed: int | None


def read_counters(device_name: str, sys_block: Path = Path("/sys/block")) -> CommandCounters:
    base = sys_block / device_name / "device"

    def value(name: str) -> int | None:
        try:
            return int((base / name).read_text().strip(), 16)
        except (OSError, ValueError):
            return None

    return CommandCounters(value("ioerr_cnt"), value("iotmo_cnt"), value("iorequest_cnt"), value("iodone_cnt"))


@dataclass(frozen=True)
class KernelEvent:
    timestamp: str
    kind: str  # "error", "reset" or "disconnect"
    message: str


_EVENT_PATTERNS = (
    ("error", re.compile(r"I/O error|critical medium error|Medium Error|blk_update_request|Buffer I/O", re.I)),
    ("reset", re.compile(r"reset (SuperSpeed|high-speed|full-speed)|reset .*USB device|uas_eh|task abort|device reset", re.I)),
    ("disconnect", re.compile(r"USB disconnect", re.I)),
)


def classify_kernel_log(lines: list[str]) -> list[KernelEvent]:
    """Keep the lines that indicate trouble. Routine attach messages are ignored."""
    events = []
    for line in lines:
        for kind, pattern in _EVENT_PATTERNS:
            if pattern.search(line):
                stamp, _, rest = line.partition(" ")
                message = rest.split("kernel: ", 1)[-1]
                events.append(KernelEvent(stamp, kind, message.strip()))
                break
    return events


def read_kernel_events(device_name: str, usb_port: str, timeout_s: int = 15) -> list[KernelEvent]:
    """Problems logged for this drive since boot. Empty if the log can't be read."""
    terms = [rf"\b{re.escape(device_name)}\b"]
    if usb_port:
        terms.append(rf"usb {re.escape(usb_port)}\b")
    try:
        done = subprocess.run(
            ["journalctl", "-k", "-b", "--no-pager", "-o", "short-iso", "--grep", "|".join(terms)],
            capture_output=True, text=True, timeout=timeout_s,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return classify_kernel_log(done.stdout.splitlines())
