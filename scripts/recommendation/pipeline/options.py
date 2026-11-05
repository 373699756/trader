"""Immutable inputs owned by the recommendation/pipeline diagnostic adapter."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from scripts.core.paths import PROJECT_ROOT

from typing import Literal

ScoringProfileSelection = Literal["v1", "v2", "v3", "v5", "v1,v2,v3,v5"]


@dataclass(frozen=True)
class PipelineOptions:
    action_identity: str
    command_timeout_seconds: float = 180.0
    runtime_config: Path = PROJECT_ROOT / 'config/runtime.json'
    scoring_profile: ScoringProfileSelection | None = None
    performance_steady_only: bool = False
    performance_factor_candidate: str = 'baseline'
    performance_scoring_candidate: str = 'baseline'
    performance_replay_ticks: int = 0
    performance_replay_rows: int | None = None
    cpu_profile_dir: Path | None = None
    cpu_profile_mode: str = 'functions'
