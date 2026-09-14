"""Typed scheduler runtime status values shared by scheduling and presentation adapters."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from trader.domain.recommendation.models import Strategy

InputQualityState = Literal["ready", "business_empty", "transient_invalid_empty", "not_ready"]
PipelineStageState = Literal["pending", "running", "completed", "degraded", "not_applicable"]
PipelineStageKey = Literal[
    "dynamic_filter",
    "board_cross_section",
    "strategy_history",
    "model_input",
    "candidate_score",
    "board_limit",
    "candidate_refresh",
    "input_coverage",
    "evidence_score",
    "model_cost_gate",
    "local_score",
    "deepseek_review",
    "fusion",
    "action_gate",
    "concentration",
]
PipelineMetricName = Literal[
    "board_reliability",
    "history_sessions",
    "input_completeness",
    "candidate_score",
    "quote_age_seconds",
    "base_score",
    "model_signal_score",
    "predicted_excess_return_pct",
    "estimated_cost_pct",
    "predicted_net_excess_pct",
    "model_disagreement_pct",
    "local_risk_penalty",
    "local_score",
    "deepseek_score",
    "deepseek_risk_penalty",
    "final_score",
]

_PIPELINE_STAGE_ORDER: tuple[PipelineStageKey, ...] = (
    "dynamic_filter",
    "board_cross_section",
    "strategy_history",
    "model_input",
    "candidate_score",
    "board_limit",
    "candidate_refresh",
    "input_coverage",
    "evidence_score",
    "model_cost_gate",
    "local_score",
    "deepseek_review",
    "fusion",
    "action_gate",
    "concentration",
)
_PIPELINE_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass(frozen=True)
class PipelineMetricRange:
    metric: PipelineMetricName
    minimum: float
    maximum: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.minimum) or not math.isfinite(self.maximum) or self.minimum > self.maximum:
            raise ValueError("pipeline metric range is invalid")


@dataclass(frozen=True)
class PipelineFacet:
    key: str
    count: int
    total: int | None = None

    def __post_init__(self) -> None:
        if _PIPELINE_KEY.fullmatch(self.key) is None or self.count < 0:
            raise ValueError("pipeline facet is invalid")
        if self.total is not None and (self.total < 0 or self.count > self.total):
            raise ValueError("pipeline facet count cannot exceed total")


@dataclass(frozen=True)
class PipelineReasonCount:
    reason: str
    count: int

    def __post_init__(self) -> None:
        if _PIPELINE_KEY.fullmatch(self.reason) is None or self.count < 1:
            raise ValueError("pipeline reason count is invalid")


@dataclass(frozen=True)
class PipelineStageStatus:
    key: PipelineStageKey
    state: PipelineStageState
    input_count: int | None
    output_count: int | None
    metric_ranges: tuple[PipelineMetricRange, ...] = ()
    threshold: float | None = None
    facets: tuple[PipelineFacet, ...] = ()
    reason_counts: tuple[PipelineReasonCount, ...] = ()

    def __post_init__(self) -> None:
        if self.input_count is not None and self.input_count < 0:
            raise ValueError("pipeline input count cannot be negative")
        if self.output_count is not None and self.output_count < 0:
            raise ValueError("pipeline output count cannot be negative")
        if self.input_count is not None and self.output_count is not None and self.output_count > self.input_count:
            raise ValueError("pipeline output count cannot exceed input")
        if self.threshold is not None and not math.isfinite(self.threshold):
            raise ValueError("pipeline threshold must be finite")
        if len({item.metric for item in self.metric_ranges}) != len(self.metric_ranges):
            raise ValueError("pipeline metric ranges must be unique")
        if len({item.key for item in self.facets}) != len(self.facets):
            raise ValueError("pipeline facets must be unique")
        if len({item.reason for item in self.reason_counts}) != len(self.reason_counts):
            raise ValueError("pipeline reasons must be unique")


@dataclass(frozen=True)
class RecommendationPipelineStatus:
    current_stage: PipelineStageKey
    stages: tuple[PipelineStageStatus, ...]

    def __post_init__(self) -> None:
        keys = tuple(item.key for item in self.stages)
        if keys != _PIPELINE_STAGE_ORDER or self.current_stage not in keys:
            raise ValueError("recommendation pipeline stages are invalid")

    def stage(self, key: PipelineStageKey) -> PipelineStageStatus:
        return next(item for item in self.stages if item.key == key)


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
    supply_reason_counts: tuple[tuple[str, int], ...] = ()
    primary_blocker: str = "ready"

    def __post_init__(self) -> None:
        if self.strategy not in {Strategy.TODAY, Strategy.TOMORROW, Strategy.D25}:
            raise ValueError("input quality requires a scored strategy")
        counts = (
            self.population_count,
            self.candidate_count,
            self.candidate_feature_count,
            self.population_rejected_count,
            self.candidate_rejected_count,
            self.candidate_scored_count,
            self.security_master_covered_count,
            self.history_covered_count,
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
    "PipelineFacet",
    "PipelineMetricName",
    "PipelineMetricRange",
    "PipelineReasonCount",
    "PipelineStageKey",
    "PipelineStageState",
    "PipelineStageStatus",
    "RecommendationPipelineStatus",
    "SupplySummary",
]
