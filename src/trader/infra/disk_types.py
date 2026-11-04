"""Immutable disk configuration and detection evidence."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

DiskKind = Literal["hdd", "ssd", "unknown"]
DiskMedium = Literal["hdd", "ssd"]


@dataclass(frozen=True)
class DiskProfile:
    kind: DiskKind
    device: str
    source: Literal["automatic", "configuration"]
    reason: str


@dataclass(frozen=True)
class DiskLocation:
    role: str
    path: Path
    profile: DiskProfile
