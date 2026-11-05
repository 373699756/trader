"""Immutable inputs owned by the infra/database diagnostic adapter."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from scripts.core.paths import PROJECT_ROOT


@dataclass(frozen=True)
class DatabaseOptions:
    action_identity: str
    command_timeout_seconds: float = 180.0
    runtime_config: Path = PROJECT_ROOT / 'config/runtime.json'
    database_probe_directory: Path | None = None
    database_rounds: int = 5
    database_training: bool = False
    database_acceptance_focus: Literal['history', 'market-company'] = 'history'
    download_sizes: tuple[int, ...] = (8,)
    history_days: int = 61
    baseline_checkout: Path | None = None
