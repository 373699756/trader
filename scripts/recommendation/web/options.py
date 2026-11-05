"""Immutable inputs owned by the recommendation/web diagnostic adapter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from scripts.core.paths import PROJECT_ROOT

from scripts.recommendation.pipeline.options import ScoringProfileSelection

WebStrategy = Literal["tomorrow", "d25"]
DEFAULT_SIMULATED_START = datetime.fromisoformat("2026-08-24T13:05:00+08:00")


@dataclass(frozen=True)
class WebOptions:
    action_identity: str
    command_timeout_seconds: float = 180.0
    base_url: str = 'http://127.0.0.1:5000'
    web_samples: int = 3
    web_interval_seconds: float = 2.0
    web_timeout_seconds: float = 3.0
    scoring_profile: ScoringProfileSelection | None = None
    runtime_config: Path = PROJECT_ROOT / 'config/runtime.json'
    browser_duration_seconds: float = 35.0
    browser_minimum_updates: int = 3
    strategies: tuple[WebStrategy, ...] = ()
    browser_strategy: WebStrategy = "tomorrow"
    simulated_start: datetime = DEFAULT_SIMULATED_START
    consecutive_zero_threshold: int = 3
