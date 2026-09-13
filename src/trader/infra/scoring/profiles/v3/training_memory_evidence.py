"""Typed evidence emitted by the Tomorrow V3 training memory gate."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from trader.application.research.tomorrow_training import TomorrowTrainingStage

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TRAINING_STAGES: frozenset[str] = frozenset(
    (
        "resource_preflight",
        "partition_validation",
        "history_conversion",
        "cross_section_conversion",
        "model_fit",
        "artifact_publish",
    )
)


@dataclass(frozen=True)
class TomorrowTrainingStageDuration:
    stage: TomorrowTrainingStage
    duration_ms: float

    def __post_init__(self) -> None:
        if self.stage not in _TRAINING_STAGES or not math.isfinite(self.duration_ms) or self.duration_ms < 0.0:
            raise ValueError("Tomorrow training stage duration is invalid")


@dataclass(frozen=True)
class TomorrowTrainingMemoryEvidence:
    training_status: str
    repeat_training_status: str
    training_input_hash: str
    model_hash: str
    report_hash: str
    peak_rss_bytes: int
    max_rss_bytes: int
    sample_database_peak_bytes: int
    stage_durations: tuple[TomorrowTrainingStageDuration, ...]

    def __post_init__(self) -> None:
        if (
            self.training_status != "engineering_ready"
            or self.repeat_training_status != "already_current"
            or any(
                _SHA256.fullmatch(value) is None
                for value in (self.training_input_hash, self.model_hash, self.report_hash)
            )
            or self.peak_rss_bytes < 1
            or self.max_rss_bytes < 1
            or self.peak_rss_bytes > self.max_rss_bytes
            or self.max_rss_bytes > 2048 * 1024 * 1024
            or self.sample_database_peak_bytes < 1
            or len({item.stage for item in self.stage_durations}) != len(self.stage_durations)
        ):
            raise ValueError("Tomorrow training memory evidence is invalid")


__all__ = ["TomorrowTrainingMemoryEvidence", "TomorrowTrainingStageDuration"]
