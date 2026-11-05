"""Immutable inputs owned by the training diagnostic adapter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from scripts.core.paths import PROJECT_ROOT

from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId, SCORING_PROFILE_IDS


@dataclass(frozen=True)
class TrainingOptions:
    action_identity: str
    command_timeout_seconds: float = 180.0
    runtime_config: Path = PROJECT_ROOT / 'config/runtime.json'
    history_root: Path | None = None
    v5_probe_directory: Path | None = None
    v5_kernel_probe: bool = False
    training_probe_directory: Path | None = None
    wide_sample_source: Path | None = None
    baseline_checkout: Path | None = None
    v5_sample_root: Path | None = None
    v5_candidate_config: Path | None = None
    training_profiles: tuple[ScoringProfileId, ...] = SCORING_PROFILE_IDS
    training_signal_date: date | None = None
    product_checkout: Path | None = None
    published_history_read_only: bool = False
    sample_cache_transfer: Literal["copy", "hardlink"] = "copy"
    scheduling_candidate: Literal["baseline", "fixed", "long_task_first", "borrowing"] = "baseline"
    scheduling_evidence: Path | None = None
    computation_candidate: Literal["baseline", "indexed_anchors", "schema_cache", "combined"] = "baseline"
    reference_config: Path | None = None
    quality_only: bool = False
    largest_only: bool = False
    cancel_after_batches: int | None = None
    prepare_samples: bool = False
    prepare_only: bool = False
    training_sample_size: int = 64
    training_sample_sessions: int = 750
