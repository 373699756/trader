"""Immutable structured-review values and risk facts."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType


class ReviewOutcome(str, Enum):
    APPLIED = "applied"
    ABSTAIN = "abstain"
    REJECTED = "rejected"
    LATE = "late"


@dataclass(frozen=True)
class RiskFact:
    risk_fact_id: str
    risk_code: str
    severity: str
    penalty: float
    source: str
    observed_at: datetime
    confidence: float = 1.0
    evidence_ids: tuple[str, ...] = ()
    group: str = ""
    veto: bool = False
    threshold: str = ""
    actual: str | float | bool | None = None
    assessment: str = ""


@dataclass(frozen=True)
class RiskRule:
    risk_code: str
    severity: str
    penalty: float
    minimum_confidence: float
    group: str
    evidence_ttl_hours: int = 876_000
    veto: bool = False
    allowed_evidence_types: tuple[str, ...] = ()
    strategies: tuple[str, ...] = ()
    trigger_factor: str = ""
    trigger_operator: str = ""
    trigger_thresholds: tuple[float, ...] = ()
    combination_mode: str = "exclusive"
    risk_fact_id_fields: tuple[str, ...] = ()
    local_trigger_enabled: bool = True


@dataclass(frozen=True)
class DimensionAssessment:
    name: str
    score: float
    confidence: float
    assessment: str
    flags: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    is_unknown: bool = False


@dataclass(frozen=True)
class DeepSeekReview:
    code: str
    outcome: ReviewOutcome
    dimensions: Mapping[str, DimensionAssessment]
    risk_facts: tuple[RiskFact, ...]
    completed_at: datetime
    error: str = ""
    rating: str = "neutral"
    review_stage: str = "primary"
    challenger_status: str = "not_run"
    requested_model: str | None = None
    actual_model: str | None = None
    thinking_mode: str | None = None
    raw_confidence: float | None = None
    calibrated_confidence: float | None = None
    evidence_manifest_hash: str | None = None
    calibration_version: str | None = None
    model_role: str | None = None
    reasoning_effort: str | None = None
    system_fingerprint: str | None = None
    prompt_cache_hit_tokens: int | None = None
    prompt_cache_miss_tokens: int | None = None
    challenger_requested_model: str | None = None
    challenger_actual_model: str | None = None
    challenger_thinking_mode: str | None = None
    challenger_reasoning_effort: str | None = None
    challenger_system_fingerprint: str | None = None
    challenger_prompt_cache_hit_tokens: int | None = None
    challenger_prompt_cache_miss_tokens: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "dimensions", MappingProxyType(dict(self.dimensions)))


@dataclass(frozen=True)
class ReviewCandidateContext:
    local_score: float
    local_rank: int
    action_threshold: float | None
    in_protection_set: bool
    has_new_high_risk: bool = False
    near_action_threshold: bool = False
    near_global_boundary: bool = False
    direction_conflict: bool = False
    evidence_conflict: bool = False
    was_reviewed: bool = False

    def __post_init__(self) -> None:
        if not math.isfinite(self.local_score):
            raise ValueError("review context local score must be finite")
        if self.local_rank < 1:
            raise ValueError("review context local rank must be positive")
        if self.action_threshold is not None and not math.isfinite(self.action_threshold):
            raise ValueError("review context action threshold must be finite")


__all__ = [
    "DeepSeekReview",
    "DimensionAssessment",
    "ReviewCandidateContext",
    "ReviewOutcome",
    "RiskFact",
    "RiskRule",
]
