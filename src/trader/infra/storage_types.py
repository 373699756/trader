"""Immutable storage configuration and detection evidence."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

StorageKind = Literal["hdd", "ssd", "unknown"]


@dataclass(frozen=True)
class StorageOverride:
    path: Path
    kind: Literal["hdd", "ssd"]


@dataclass(frozen=True)
class StorageProfile:
    kind: StorageKind
    device: str
    source: Literal["automatic", "configuration"]
    reason: str


@dataclass(frozen=True)
class StorageLocation:
    role: str
    path: Path
    profile: StorageProfile
