"""Immutable inputs owned by the infra/disk diagnostic adapter."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from scripts.core.paths import PROJECT_ROOT


@dataclass(frozen=True)
class DiskOptions:
    action_identity: str
    command_timeout_seconds: float = 180.0
    runtime_config: Path = PROJECT_ROOT / 'config/runtime.json'
    disk_paths: tuple[Path, ...] = ()
    disk_policy_audit: bool = False
