"""Bounded, read-only Windows and macOS storage metadata queries."""

from __future__ import annotations

import base64
import os
import plistlib
import subprocess
import time
from pathlib import Path
from xml.parsers.expat import ExpatError

from trader.infra.storage_types import StorageKind

_WINDOWS_QUERY = """
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$volume = Get-Volume -FilePath $env:TRADER_STORAGE_PROBE_PATH
$disks = @(Get-Partition -Volume $volume | Get-Disk)
$physical = @(Get-PhysicalDisk)
$kinds = @()
foreach ($disk in $disks) {
    if (!$disk.UniqueId) { 'unknown'; exit }
    $matches = @($physical | Where-Object { $_.UniqueId -eq $disk.UniqueId })
    if ($matches.Count -ne 1) { 'unknown'; exit }
    $kinds += [string]$matches[0].MediaType
}
if (!$kinds.Count -or @($kinds | Where-Object { $_ -notin @('HDD','SSD') }).Count) { 'unknown' }
elseif ($kinds -contains 'HDD') { 'hdd' }
else { 'ssd' }
"""


def ProbeWindowsStorage(path: Path, *, timeout: float) -> StorageKind:
    encoded = base64.b64encode(_WINDOWS_QUERY.encode("utf-16-le")).decode("ascii")
    environment = os.environ.copy()
    environment["TRADER_STORAGE_PROBE_PATH"] = str(path) + (os.sep if path.is_dir() else "")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        env=environment,
        capture_output=True,
        timeout=timeout,
        check=True,
    )
    value = result.stdout.decode("ascii", errors="replace").strip().lower()
    return "hdd" if value == "hdd" else "ssd" if value == "ssd" else "unknown"


def ProbeMacStorage(path: Path, *, timeout: float) -> StorageKind:
    deadline = time.monotonic() + timeout
    info = _diskutil(("info", "-plist", str(path)), deadline)
    # APFS can span multiple devices (including Fusion); inspect every physical store.
    container = info.get("APFSContainerReference")
    if isinstance(container, str):
        listing = _diskutil(("apfs", "list", "-plist", container), deadline)
        containers = listing.get("Containers")
        if not isinstance(containers, list) or len(containers) != 1:
            return "unknown"
        item = containers[0]
        stores = item.get("PhysicalStores") if isinstance(item, dict) else None
        if not isinstance(stores, list) or not 1 <= len(stores) <= 16:
            return "unknown"
        kinds: list[StorageKind] = []
        for store in stores:
            device = store.get("DeviceIdentifier") if isinstance(store, dict) else None
            if not isinstance(device, str) or not device.startswith("disk") or "/" in device:
                return "unknown"
            kinds.append(_mac_kind(_diskutil(("info", "-plist", device), deadline)))
        if "unknown" in kinds:
            return "unknown"
        return "hdd" if "hdd" in kinds else "ssd"
    return _mac_kind(info)


def _mac_kind(info: dict[str, object]) -> StorageKind:
    if info.get("VirtualOrPhysical") == "Virtual":
        return "unknown"
    value = info.get("SolidState")
    return "ssd" if value is True else "hdd" if value is False else "unknown"


def _diskutil(arguments: tuple[str, ...], deadline: float) -> dict[str, object]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired("diskutil", 0)
    result = subprocess.run(["/usr/sbin/diskutil", *arguments], capture_output=True, timeout=remaining, check=True)
    if len(result.stdout) > 1024 * 1024:
        return {}
    try:
        value = plistlib.loads(result.stdout)
    except (plistlib.InvalidFileException, ExpatError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}
