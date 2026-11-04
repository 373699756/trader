"""One disk detector, with device caching and a global manual medium."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from trader.infra.disk_platform import ProbeMacDisk, ProbeWindowsDisk
from trader.infra.disk_types import DiskKind, DiskMedium, DiskProfile

_SYSFS_BLOCK_ROOT = Path("/sys/dev/block")
_VIRTUAL_DISK_IDENTIFIERS = ("VMWARE", "QEMU", "VBOX", "VIRTUAL", "XEN")


class DiskProbe:
    """Read metadata only; never create paths, benchmark disks or change mount settings."""

    def __init__(self, medium: DiskMedium | None = None) -> None:
        self._medium = medium
        self._devices: dict[int, DiskProfile] = {}

    def inspect(self, path: Path) -> DiskProfile:
        if self._medium is not None:
            return DiskProfile(self._medium, "configured", "configuration", "manual_medium")
        try:
            target = path.resolve()
            existing = target
            while True:
                try:
                    device = existing.stat().st_dev
                    break
                except FileNotFoundError:
                    if existing == existing.parent:
                        raise
                    existing = existing.parent
        except (OSError, RuntimeError, ValueError):
            return DiskProfile("unknown", "unresolved", "automatic", "path_unavailable")
        if device not in self._devices:
            self._devices[device] = _probe_device(existing, device)
        return self._devices[device]


def _probe_device(path: Path, device: int) -> DiskProfile:
    identity = f"{sys.platform}:{device}"
    try:
        if sys.platform.startswith("linux"):
            node = (_SYSFS_BLOCK_ROOT / f"{os.major(device)}:{os.minor(device)}").resolve(strict=True)
            kind, devices = _linux_kind(node, frozenset())
            return DiskProfile(kind, "+".join(sorted(devices)) or identity, "automatic", _reason(kind))
        if sys.platform == "win32":
            kind = ProbeWindowsDisk(path, timeout=3.0)
        elif sys.platform == "darwin":
            kind = ProbeMacDisk(path, timeout=3.0)
        else:
            return DiskProfile("unknown", identity, "automatic", "platform_unsupported")
        return DiskProfile(kind, identity, "automatic", _reason(kind))
    except subprocess.TimeoutExpired:
        return DiskProfile("unknown", identity, "automatic", "probe_timeout")
    except PermissionError:
        return DiskProfile("unknown", identity, "automatic", "permission_denied")
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError):
        return DiskProfile("unknown", identity, "automatic", "device_metadata_unavailable")


def _reason(kind: DiskKind) -> str:
    return "rotational_metadata" if kind != "unknown" else "device_metadata_unavailable"


def _linux_kind(node: Path, visited: frozenset[Path]) -> tuple[DiskKind, frozenset[str]]:
    node = node.resolve(strict=True)
    if node in visited or len(visited) >= 16:
        return "unknown", frozenset()
    visited = visited | {node}
    # Partition queues belong to the parent disk. Do not walk beyond that disk.
    if (node / "partition").exists() or not (node / "queue").exists():
        node = node.parent
    slaves = node / "slaves"
    children = tuple(slaves.iterdir()) if slaves.is_dir() else ()
    if children:
        if len(children) > 16:
            return "unknown", frozenset()
        results = tuple(_linux_kind(child, visited) for child in children)
        kinds = tuple(item[0] for item in results)
        devices = frozenset(device for _, values in results for device in values)
        kind: DiskKind = "unknown" if "unknown" in kinds else "hdd" if "hdd" in kinds else "ssd"
        return kind, devices
    # Hypervisors may expose a virtual SCSI disk as a physical sysfs device.
    # Its rotational flag describes the guest queue, not the host medium.
    if "virtual" in node.parts or _is_virtual_disk(node):
        return "unknown", frozenset({node.name})
    value = (node / "queue/rotational").read_text(encoding="ascii").strip()
    return ("hdd" if value == "1" else "ssd" if value == "0" else "unknown"), frozenset({node.name})


def _is_virtual_disk(node: Path) -> bool:
    for name in ("vendor", "model"):
        identity = node / "device" / name
        if not identity.is_file():
            continue
        value = identity.read_text(encoding="ascii", errors="replace").upper()
        if any(marker in value for marker in _VIRTUAL_DISK_IDENTIFIERS):
            return True
    return False
