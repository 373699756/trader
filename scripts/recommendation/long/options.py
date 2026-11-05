"""Immutable inputs owned by the recommendation/long diagnostic adapter."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from scripts.core.paths import PROJECT_ROOT


@dataclass(frozen=True)
class LongOptions:
    action_identity: str
    command_timeout_seconds: float = 180.0
    runtime_config: Path = PROJECT_ROOT / 'config/runtime.json'
    long_watchlist: Path = PROJECT_ROOT / 'config/long_watchlist.json'
    long_evidence_output: Path | None = None
    company_evidence_database: Path | None = None
    eligibility_evidence: tuple[Path, ...] = ()
    strategy_config: Path = PROJECT_ROOT / 'config/strategy.json'
    workers: int = 8
