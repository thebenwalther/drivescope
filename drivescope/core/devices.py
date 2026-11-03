"""Find drives and describe how they're connected, from lsblk and sysfs."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .mounts import Mount, mount_at, read_mounts

# Highest possible data rate after line encoding, in bytes per second. Real
# transfers land lower because of protocol overhead.
_LINK_CEILING_BPS = {
    480: 53_000_000,  # USB 2, about 53 MB/s at the bulk transfer limit
    5000: 500_000_000,  # USB 3.2 Gen 1, 8b/10b
    10000: 1_212_000_000,  # USB 3.2 Gen 2, 128b/132b
    20000: 2_424_000_000,  # USB 3.2 Gen 2x2 or USB4 20 Gbps
    40000: 4_848_000_000,  # USB4 40 Gbps
}


@dataclass(frozen=True)
class UsbLink:
    speed_mbps: int
    version: str
    manufacturer: str
    product: str
    id_vendor: str
    id_product: str
    driver: str  # "uas" or "usb-storage"
    max_power: str
    root_hub_mbps: int  # what the host port's bus can do
    through_hub: bool
    port: str = ""  # e.g. "6-1", how the kernel log refers to this device

    @property
    def protocol(self) -> str:
        return {"uas": "UAS", "usb-storage": "BOT"}.get(self.driver, self.driver or "unknown")

    @property
    def ceiling_bps(self) -> int | None:
        return _LINK_CEILING_BPS.get(self.speed_mbps)

    def _can_exceed_5_gbps(self) -> bool:
        """USB 3.0 tops out at 5 Gbps, so running at that speed isn't a problem."""
        try:
            return float(self.version.strip()) > 3.0 or self.speed_mbps != 5000
        except ValueError:
            return True

    def warnings(self) -> list[str]:
        out = []
        try:
            major = int(self.version.strip().split(".")[0])
        except ValueError:
            major = 0
        if major >= 3 and self.speed_mbps <= 480:
            out.append(
                "This USB 3 drive is connected at USB 2 speed. "
                "Try a different cable or port."
            )
        elif self.root_hub_mbps > self.speed_mbps and self.speed_mbps > 480 and self._can_exceed_5_gbps():
            out.append(
                f"The port's bus supports {self.root_hub_mbps / 1000:g} Gbps, but this drive "
                f"linked at {self.speed_mbps / 1000:g} Gbps. The drive itself may be the limit, "
                "or the cable."
            )
        if self.driver == "usb-storage":
            out.append(
                "Using the older BOT protocol instead of UAS, which limits queueing "
                "and random speed."
            )
        return out


@dataclass(frozen=True)
class Partition:
    name: str
    path: str
    size: int
    fstype: str
    label: str
    mountpoint: str
    mount_read_only: bool
    free_bytes: int | None

    @property
    def display_name(self) -> str:
        return self.label or self.name


@dataclass(frozen=True)
class Drive:
    name: str
    path: str
    vendor: str
    model: str
    serial: str
    size: int
    transport: str
    rotational: bool
    removable_or_hotplug: bool
    partitions: tuple[Partition, ...] = field(default_factory=tuple)
    link: UsbLink | None = None
    queue_depth: int | None = None
    scheduler: str = ""
    max_transfer_kb: int | None = None
    logical_block: int = 512

    @property
    def stable_id(self) -> str:
        """Identifies the physical drive across replugs, for history."""
        hw = f"{self.link.id_vendor}:{self.link.id_product}" if self.link else self.transport
        return f"{hw}:{self.serial or self.model or self.name}"

    @property
    def display_name(self) -> str:
        return " ".join(p for p in (self.vendor, self.model) if p) or self.name

    @property
    def mounted(self) -> list[Partition]:
        return [p for p in self.partitions if p.mountpoint]


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def _read_int(path: Path) -> int | None:
    try:
        return int(_read(path))
    except ValueError:
        return None


def _scheduler(text: str) -> str:
    for part in text.split():
        if part.startswith("["):
            return part.strip("[]")
    return text


def read_usb_link(block_name: str, sys_root: Path = Path("/sys")) -> UsbLink | None:
    """Walk up from the block device to the USB device that carries it."""
    try:
        node = (sys_root / "block" / block_name / "device").resolve()
    except OSError:
        return None

    driver = ""
    root = sys_root.resolve()
    while node != root and node != node.parent:
        if not driver and (node / "bInterfaceClass").exists():
            link = node / "driver"
            if link.exists():
                driver = os.path.basename(os.path.realpath(link))
        if (node / "speed").exists() and (node / "idVendor").exists():
            speed = _read_int(node / "speed") or 0
            parent = node.parent
            parent_is_hub = (parent / "idVendor").exists() and (parent / "devpath").exists()
            # Climb to the root hub, whose speed is the most the bus can do.
            top = node
            while (top.parent / "idVendor").exists():
                top = top.parent
            return UsbLink(
                speed_mbps=speed,
                version=_read(node / "version"),
                manufacturer=_read(node / "manufacturer"),
                product=_read(node / "product"),
                id_vendor=_read(node / "idVendor"),
                id_product=_read(node / "idProduct"),
                driver=driver,
                max_power=_read(node / "bMaxPower"),
                root_hub_mbps=_read_int(top / "speed") or speed,
                through_hub=parent_is_hub and top != parent,
                port=node.name,
            )
        node = node.parent
    return None


def _free_bytes(mountpoint: str) -> int | None:
    try:
        return shutil.disk_usage(mountpoint).free
    except OSError:
        return None


def parse_lsblk(text: str, mounts: list[Mount], include_internal: bool = False) -> list[dict]:
    """Keep the drives worth showing, with each partition's mount details attached."""
    out = []
    for dev in json.loads(text).get("blockdevices", []):
        if dev.get("type") not in ("disk", "loop") or dev.get("name", "").startswith("zram"):
            continue  # zram is compressed RAM used as swap, not a drive
        if dev.get("type") == "loop" and not dev.get("children") and not dev.get("mountpoint"):
            continue  # unattached or backing nothing visible
        external = dev.get("tran") in ("usb", "ieee1394") or (
            dev.get("hotplug") and dev.get("tran") not in ("nvme", "sata", None)
        )
        if not external and not include_internal:
            continue
        out.append(dev)
    return out


def _partitions(dev: dict, mounts: list[Mount]) -> tuple[Partition, ...]:
    # A drive with a filesystem directly on it, and no partition table, counts
    # as its own single partition.
    entries = dev.get("children") or ([dev] if dev.get("fstype") else [])
    parts = []
    for child in entries:
        mountpoint = child.get("mountpoint") or ""
        mount = mount_at(mountpoint, mounts) if mountpoint else None
        parts.append(
            Partition(
                name=child.get("name", ""),
                path=child.get("path", ""),
                size=int(child.get("size") or 0),
                fstype=child.get("fstype") or "",
                label=child.get("label") or "",
                mountpoint=mountpoint,
                mount_read_only=mount.read_only if mount else False,
                free_bytes=_free_bytes(mountpoint) if mountpoint else None,
            )
        )
    return tuple(parts)


def list_drives(include_internal: bool = False) -> list[Drive]:
    result = subprocess.run(
        ["lsblk", "-J", "-b", "-o",
         "NAME,PATH,TRAN,TYPE,SIZE,FSTYPE,MOUNTPOINT,MODEL,SERIAL,LABEL,VENDOR,HOTPLUG,ROTA"],
        capture_output=True, text=True, check=True, timeout=10,
    )
    mounts = read_mounts()
    drives = []
    for dev in parse_lsblk(result.stdout, mounts, include_internal):
        name = dev["name"]
        queue = Path("/sys/block") / name / "queue"
        drives.append(
            Drive(
                name=name,
                path=dev.get("path") or f"/dev/{name}",
                vendor=(dev.get("vendor") or "").strip(),
                model=(dev.get("model") or "").strip(),
                serial=(dev.get("serial") or "").strip(),
                size=int(dev.get("size") or 0),
                transport=dev.get("tran") or dev.get("type") or "",
                rotational=bool(dev.get("rota")),
                removable_or_hotplug=bool(dev.get("hotplug")),
                partitions=_partitions(dev, mounts),
                link=read_usb_link(name),
                queue_depth=_read_int(Path("/sys/block") / name / "device" / "queue_depth"),
                scheduler=_scheduler(_read(queue / "scheduler")),
                max_transfer_kb=_read_int(queue / "max_sectors_kb"),
                logical_block=_read_int(queue / "logical_block_size") or 512,
            )
        )
    return drives
