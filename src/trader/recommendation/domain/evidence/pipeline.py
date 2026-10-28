"""Immutable recommendation-pipeline audit carried by scored freezes."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Literal
from zoneinfo import ZoneInfo


class PipelineStage(str, Enum):
    LOCAL_DATA = "local_data"
    STATIC_FILTER = "static_filter"
    DYNAMIC_MARKET = "dynamic_market"
    DYNAMIC_FILTER = "dynamic_filter"
    MARKET_FEATURES = "market_features"
    MODEL_FILTER = "model_filter"
    LOCAL_SCORE = "local_score"
    LOCAL_RISK = "local_risk"
    FIXED_SELECTION = "fixed_selection"
    RISK_REVIEW = "risk_review"
    ACTION_PUBLISH = "action_publish"


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
        if not isinstance(self.state, SourceHealthState):
            raise ValueError("active source health requires the active state type")
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
        if not isinstance(self.severity, Severity):
            raise ValueError("active stage reasons require the active severity type")
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
    input_batch_id: str
    output_batch_id: str
    as_of: datetime
    state: StageState
    input_count: int
    output_count: int
    rejected_count: int
    pending_count: int
    failed_count: int
    reasons: tuple[StageReasonAggregate, ...]
    source_health: SourceHealth
    latency_ms: int | None
    degraded: bool
    not_selected_count: int = 0
    operation_durations: tuple[tuple[str, int | None], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.stage, PipelineStage):
            raise ValueError("active stage snapshots require the active stage type")
        if not isinstance(self.state, StageState) or not isinstance(self.source_health, SourceHealth):
            raise ValueError("active stage snapshots require active state and health types")
        if any(not isinstance(item, StageReasonAggregate) for item in self.reasons):
            raise ValueError("active stage snapshots require active reason types")
        if self.stage_order != PIPELINE_STAGES.index(self.stage) + 1:
            raise ValueError("pipeline stage order does not match the stage")
        if not self.input_batch_id.strip() or not self.output_batch_id.strip():
            raise ValueError("pipeline batch identities are required")
        _require_shanghai(self.as_of, "pipeline stage snapshot")
        counts = (
            self.input_count,
            self.output_count,
            self.rejected_count,
            self.pending_count,
            self.failed_count,
            self.not_selected_count,
        )
        _validate_operation_timings(self.latency_ms, self.operation_durations)
        if any(value < 0 for value in counts):
            raise ValueError("pipeline stage counts and latency cannot be negative")
        if (
            self.output_count + self.rejected_count + self.pending_count + self.failed_count + self.not_selected_count
            != self.input_count
        ):
            raise ValueError("pipeline stage outcomes must partition the input count")
        if self.degraded != (self.state is StageState.DEGRADED):
            raise ValueError("pipeline degraded flag must match stage state")
        _require_unique_reason_codes(self.reasons)


def _validate_operation_timings(latency_ms: int | None, operations: tuple[tuple[str, int | None], ...]) -> None:
    if latency_ms is not None and latency_ms < 0:
        raise ValueError("pipeline latency cannot be negative")
    if len(dict(operations)) != len(operations) or any(
        _PIPELINE_KEY.fullmatch(name) is None or (value is not None and value < 0) for name, value in operations
    ):
        raise ValueError("pipeline operation durations are invalid")


def _require_unique_reason_codes(reasons: tuple[StageReasonAggregate, ...]) -> None:
    if len({item.code for item in reasons}) != len(reasons):
        raise ValueError("pipeline reason codes must be unique")


def ValidateStageBatchContinuity(stages: tuple[PipelineStageSnapshot, ...]) -> None:
    """Reject a stage list whose immutable handoff identities do not line up."""
    if any(not isinstance(item, PipelineStageSnapshot) for item in stages):
        raise ValueError("active pipeline requires active stage snapshots")
    if tuple(item.stage for item in stages) != PIPELINE_STAGES:
        raise ValueError("pipeline stage snapshots must contain all stages in order")
    for previous, current in zip(stages, stages[1:], strict=False):
        if previous.output_batch_id != current.input_batch_id:
            raise ValueError("pipeline stage batch identities are not continuous")
        if previous.output_count != current.input_count:
            raise ValueError("pipeline stage counts are not continuous")


def _require_shanghai(value: datetime, label: str) -> None:
    if (
        value.tzinfo is None
        or value.utcoffset() is None
        or not (isinstance(value.tzinfo, ZoneInfo) and value.tzinfo.key == "Asia/Shanghai")
    ):
        raise ValueError(f"{label} must use Asia/Shanghai")


PipelineStageState = Literal["pending", "running", "completed", "degraded", "not_applicable"]
PipelineStageKey = Literal[
    "local_data",
    "static_filter",
    "dynamic_market",
    "dynamic_filter",
    "market_features",
    "model_filter",
    "local_score",
    "local_risk",
    "fixed_selection",
    "risk_review",
    "action_publish",
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
    "local_risk_penalty",
    "local_score",
    "deepseek_score",
    "deepseek_risk_penalty",
    "final_score",
]
PIPELINE_STAGE_ORDER: tuple[PipelineStageKey, ...] = (
    "local_data",
    "static_filter",
    "dynamic_market",
    "dynamic_filter",
    "market_features",
    "model_filter",
    "local_score",
    "local_risk",
    "fixed_selection",
    "risk_review",
    "action_publish",
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
    rejected_count: int | None = None
    pending_count: int | None = None
    failed_count: int | None = None
    not_selected_count: int | None = None

    def __post_init__(self) -> None:
        if (
            any(not isinstance(item, PipelineMetricRange) for item in self.metric_ranges)
            or any(not isinstance(item, PipelineFacet) for item in self.facets)
            or any(not isinstance(item, PipelineReasonCount) for item in self.reason_counts)
        ):
            raise ValueError("active diagnostics require active evidence types")
        if self.input_count is not None and self.input_count < 0:
            raise ValueError("pipeline input count cannot be negative")
        if self.output_count is not None and self.output_count < 0:
            raise ValueError("pipeline output count cannot be negative")
        if self.duration_ms is not None and (not math.isfinite(self.duration_ms) or self.duration_ms < 0.0):
            raise ValueError("pipeline duration must be finite and non-negative")
        if self.rejected_count is not None and self.rejected_count < 0:
            raise ValueError("pipeline rejected count cannot be negative")
        if self.input_count is not None and self.rejected_count is not None and self.rejected_count > self.input_count:
            raise ValueError("pipeline rejected count cannot exceed input")
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
    stage_snapshots: tuple[PipelineStageSnapshot, ...] = ()
    elapsed_ms: int | None = None

    def __post_init__(self) -> None:
        if self.elapsed_ms is not None and self.elapsed_ms < 0:
            raise ValueError("pipeline elapsed time cannot be negative")
        if any(not isinstance(item, PipelineStageStatus) for item in self.stages):
            raise ValueError("active pipeline requires active diagnostic types")
        keys = tuple(item.key for item in self.stages)
        if keys != PIPELINE_STAGE_ORDER or self.current_stage not in keys:
            raise ValueError("recommendation pipeline stages are invalid")
        if self.stage_snapshots:
            ValidateStageBatchContinuity(self.stage_snapshots)

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
    "ValidateStageBatchContinuity",
    "PipelineStageState",
    "PipelineStageStatus",
    "RecommendationPipelineStatus",
    "Severity",
    "SourceHealth",
    "SourceHealthState",
    "StageReasonAggregate",
    "StageState",
]
