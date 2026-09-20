"""Immutable recommendation-pipeline audit carried by scored decisions."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Literal


class PipelineStage(str, Enum):
    DATA_SOURCE = "data_source"
    STATIC_MARKET = "static_market"
    STATIC_STANDARDIZE = "static_standardize"
    STATIC_FILTER = "static_filter"
    DYNAMIC_MARKET = "dynamic_market"
    DYNAMIC_STANDARDIZE = "dynamic_standardize"
    DYNAMIC_FILTER = "dynamic_filter"
    CANDIDATE_POOL = "candidate_pool"
    QUALITY_CHECK = "quality_check"
    LOCAL_SCORE = "local_score"
    RISK_REVIEW = "risk_review"
    SCORE_MERGE = "score_merge"
    DOWNSIDE_ACTION = "downside_action"
    FINAL_SELECTION = "final_selection"


PIPELINE_STAGES: tuple[PipelineStage, ...] = tuple(PipelineStage)


class StageState(str, Enum):
    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"
    NOT_READY = "not_ready"


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class SourceHealthState(str, Enum):
    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class SourceHealth:
    state: SourceHealthState
    source_count: int
    healthy_source_count: int
    latest_success_at: datetime | None = None
    age_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.source_count < 0 or not 0 <= self.healthy_source_count <= self.source_count:
            raise ValueError("source health counts are invalid")
        if self.latest_success_at is not None:
            _require_shanghai(self.latest_success_at, "source latest success")
        if self.age_seconds is not None and (not math.isfinite(self.age_seconds) or self.age_seconds < 0.0):
            raise ValueError("source age must be finite and non-negative")
        if self.state is SourceHealthState.READY and self.source_count != self.healthy_source_count:
            raise ValueError("ready source health requires every source to be healthy")
        if self.state is SourceHealthState.UNAVAILABLE and self.healthy_source_count:
            raise ValueError("unavailable source health cannot contain healthy sources")


@dataclass(frozen=True, slots=True)
class StageReasonAggregate:
    code: str
    label: str
    count: int
    severity: Severity

    def __post_init__(self) -> None:
        if _PIPELINE_KEY.fullmatch(self.code) is None or not self.label.strip() or self.count < 1:
            raise ValueError("pipeline reason aggregate is invalid")


@dataclass(frozen=True, slots=True)
class BusinessRejectionSummary:
    input_count: int
    rejected_count: int
    rejection_rate: Decimal
    reasons: tuple[StageReasonAggregate, ...] = ()

    def __post_init__(self) -> None:
        if self.input_count < 0 or not 0 <= self.rejected_count <= self.input_count:
            raise ValueError("business rejection counts are invalid")
        expected = Decimal(self.rejected_count) / Decimal(self.input_count) if self.input_count else Decimal("0")
        if self.rejection_rate != expected:
            raise ValueError("business rejection rate must match the stage counts")
        _require_unique_reason_codes(self.reasons)


@dataclass(frozen=True, slots=True)
class PipelineStageSnapshot:
    stage: PipelineStage
    stage_order: int
    as_of: datetime
    state: StageState
    input_count: int
    output_count: int
    rejected_count: int
    pending_count: int
    failed_count: int
    reasons: tuple[StageReasonAggregate, ...]
    source_health: SourceHealth
    latency_ms: int
    degraded: bool

    def __post_init__(self) -> None:
        if self.stage_order != PIPELINE_STAGES.index(self.stage) + 1:
            raise ValueError("pipeline stage order does not match the stage")
        _require_shanghai(self.as_of, "pipeline stage snapshot")
        counts = (
            self.input_count,
            self.output_count,
            self.rejected_count,
            self.pending_count,
            self.failed_count,
            self.latency_ms,
        )
        if any(value < 0 for value in counts):
            raise ValueError("pipeline stage counts and latency cannot be negative")
        if self.output_count + self.rejected_count + self.pending_count + self.failed_count > self.input_count:
            raise ValueError("pipeline stage outcomes cannot exceed input count")
        if self.degraded != (self.state is StageState.DEGRADED):
            raise ValueError("pipeline degraded flag must match stage state")
        _require_unique_reason_codes(self.reasons)


def _require_unique_reason_codes(reasons: tuple[StageReasonAggregate, ...]) -> None:
    if len({item.code for item in reasons}) != len(reasons):
        raise ValueError("pipeline reason codes must be unique")


def _require_shanghai(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None or getattr(value.tzinfo, "key", None) != "Asia/Shanghai":
        raise ValueError(f"{label} must use Asia/Shanghai")


PipelineStageState = Literal["pending", "running", "completed", "degraded", "not_applicable"]
PipelineStageKey = Literal[
    "input_readiness",
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
PIPELINE_STAGE_ORDER: tuple[PipelineStageKey, ...] = (
    "input_readiness",
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
    duration_ms: float | None = None

    def __post_init__(self) -> None:
        if self.input_count is not None and self.input_count < 0:
            raise ValueError("pipeline input count cannot be negative")
        if self.output_count is not None and self.output_count < 0:
            raise ValueError("pipeline output count cannot be negative")
        if self.duration_ms is not None and (not math.isfinite(self.duration_ms) or self.duration_ms < 0.0):
            raise ValueError("pipeline duration must be finite and non-negative")
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
        if keys != PIPELINE_STAGE_ORDER or self.current_stage not in keys:
            raise ValueError("recommendation pipeline stages are invalid")

    def stage(self, key: PipelineStageKey) -> PipelineStageStatus:
        return next(item for item in self.stages if item.key == key)


__all__ = [
    "BusinessRejectionSummary",
    "PIPELINE_STAGE_ORDER",
    "PIPELINE_STAGES",
    "PipelineFacet",
    "PipelineMetricName",
    "PipelineMetricRange",
    "PipelineReasonCount",
    "PipelineStage",
    "PipelineStageKey",
    "PipelineStageSnapshot",
    "PipelineStageState",
    "PipelineStageStatus",
    "RecommendationPipelineStatus",
    "Severity",
    "SourceHealth",
    "SourceHealthState",
    "StageReasonAggregate",
    "StageState",
]
