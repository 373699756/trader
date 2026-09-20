"""Typed scheduler runtime status values shared by scheduling and presentation adapters."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Protocol

from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.evidence.pipeline import (
    PIPELINE_STAGES,
    PipelineStageSnapshot,
    RecommendationPipelineStatus,
)

InputQualityState = Literal["ready", "business_empty", "transient_invalid_empty", "not_ready"]


class ResearchAuditIdentity(Protocol):
    decision_version: str
    decision_hash: str


@dataclass(frozen=True)
class SupplySummary:
    trade_date: date
    quote_total_count: int = 0
    quote_covered_count: int = 0
    quote_missing_count: int = 0
    security_identity_missing_count: int = 0
    latest_quote_source: str | None = None
    latest_quote_source_time: datetime | None = None
    highest_final_score: float | None = None

    def __post_init__(self) -> None:
        counts = (
            self.quote_total_count,
            self.quote_covered_count,
            self.quote_missing_count,
            self.security_identity_missing_count,
        )
        if any(value < 0 for value in counts):
            raise ValueError("supply summary counts cannot be negative")
        if self.quote_covered_count + self.quote_missing_count != self.quote_total_count:
            raise ValueError("quote coverage must partition the requested total")
        if self.latest_quote_source_time is not None and (
            self.latest_quote_source_time.tzinfo is None or self.latest_quote_source_time.utcoffset() is None
        ):
            raise ValueError("latest quote source time must be timezone-aware")
        if self.highest_final_score is not None and (
            not math.isfinite(self.highest_final_score) or not 0.0 <= self.highest_final_score <= 100.0
        ):
            raise ValueError("highest final score must be in [0, 100]")


@dataclass(frozen=True)
class InputQualityStatus:
    strategy: Strategy
    status: InputQualityState
    publishable: bool
    summary: SupplySummary
    pipeline: RecommendationPipelineStatus
    stage_snapshots: tuple[PipelineStageSnapshot, ...]
    population_count: int = 0
    candidate_count: int = 0
    candidate_feature_count: int = 0
    population_rejected_count: int = 0
    candidate_rejected_count: int = 0
    candidate_scored_count: int = 0
    security_master_covered_count: int = 0
    history_covered_count: int = 0
    history_required_sessions: int = 20
    candidate_feature_coverage_ratio: float = 0.0
    security_master_coverage_ratio: float = 0.0
    history_coverage_ratio: float = 0.0
    population_filter_reason_counts: tuple[tuple[str, int], ...] = ()
    candidate_filter_reason_counts: tuple[tuple[str, int], ...] = ()
    candidate_transient_reason_counts: tuple[tuple[str, int], ...] = ()
    candidate_optional_reason_counts: tuple[tuple[str, int], ...] = ()
    degraded_reasons: tuple[str, ...] = ()
    data_pending_count: int = 0
    refresh_pending_count: int = 0
    supply_reason_counts: tuple[tuple[str, int], ...] = ()
    primary_blocker: str = "ready"

    def __post_init__(self) -> None:
        if self.strategy not in {Strategy.TOMORROW, Strategy.D25}:
            raise ValueError("input quality requires a scored strategy")
        expected = PIPELINE_STAGES[:9]
        if tuple(item.stage for item in self.stage_snapshots) != expected:
            raise ValueError("input quality requires the ordered first nine pipeline snapshots")
        counts = (
            self.population_count,
            self.candidate_count,
            self.candidate_feature_count,
            self.population_rejected_count,
            self.candidate_rejected_count,
            self.candidate_scored_count,
            self.security_master_covered_count,
            self.history_covered_count,
            self.data_pending_count,
            self.refresh_pending_count,
        )
        if any(value < 0 for value in counts):
            raise ValueError("input quality counts cannot be negative")
        if self.history_required_sessions < 1:
            raise ValueError("input quality history requirement must be positive")
        for value in (
            self.candidate_feature_coverage_ratio,
            self.security_master_coverage_ratio,
            self.history_coverage_ratio,
        ):
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError("input quality ratios must be in [0, 1]")
        for name in (
            "population_filter_reason_counts",
            "candidate_filter_reason_counts",
            "candidate_transient_reason_counts",
            "candidate_optional_reason_counts",
            "supply_reason_counts",
        ):
            pairs = tuple(sorted(getattr(self, name)))
            if any(not key or value < 0 for key, value in pairs):
                raise ValueError("input quality reason counts must be non-negative")
            if len({key for key, _value in pairs}) != len(pairs):
                raise ValueError("input quality reason keys must be unique")
            object.__setattr__(self, name, pairs)
        object.__setattr__(self, "degraded_reasons", tuple(sorted(set(self.degraded_reasons))))
        if not self.primary_blocker:
            raise ValueError("primary blocker must not be empty")


__all__ = [
    "InputQualityState",
    "InputQualityStatus",
    "RecommendationPipelineStatus",
    "ResearchAuditIdentity",
    "SupplySummary",
]
