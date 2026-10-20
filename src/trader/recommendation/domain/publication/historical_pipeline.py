"""Frozen audit vocabulary of the persisted fourteen-stage pipeline.

These values describe historical evidence only. They never participate in the
active pipeline's construction, scheduling or scoring.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from zoneinfo import ZoneInfo


class HistoricalPipelineStage(str, Enum):
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


class HistoricalStageState(str, Enum):
    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"
    NOT_READY = "not_ready"


class HistoricalSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class HistoricalSourceState(str, Enum):
    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


_STAGES = tuple(HistoricalPipelineStage)
_DIAGNOSTIC_ORDER = (
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
_DIAGNOSTIC_STATES = frozenset({"pending", "running", "completed", "degraded", "not_applicable"})
_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass(frozen=True)
class HistoricalSourceHealth:
    state: HistoricalSourceState
    source_count: int
    healthy_source_count: int
    latest_success_at: datetime | None
    age_seconds: float | None

    def __post_init__(self) -> None:
        if not isinstance(self.state, HistoricalSourceState):
            raise ValueError("historical source health state is invalid")
        if self.source_count < 0 or not 0 <= self.healthy_source_count <= self.source_count:
            raise ValueError("historical source health counts are invalid")
        if self.latest_success_at is not None:
            _require_shanghai(self.latest_success_at)
        if self.age_seconds is not None and (not math.isfinite(self.age_seconds) or self.age_seconds < 0):
            raise ValueError("historical source age is invalid")
        if self.state is HistoricalSourceState.READY and self.source_count != self.healthy_source_count:
            raise ValueError("historical ready sources must all be healthy")
        if self.state is HistoricalSourceState.UNAVAILABLE and self.healthy_source_count:
            raise ValueError("historical unavailable sources cannot be healthy")


@dataclass(frozen=True)
class HistoricalStageReason:
    code: str
    label: str
    count: int
    severity: HistoricalSeverity

    def __post_init__(self) -> None:
        if _KEY.fullmatch(self.code) is None or not self.label.strip() or self.count < 1:
            raise ValueError("historical stage reason is invalid")
        if not isinstance(self.severity, HistoricalSeverity):
            raise ValueError("historical severity is invalid")


@dataclass(frozen=True)
class HistoricalStageSnapshot:
    stage: HistoricalPipelineStage
    stage_order: int
    input_batch_id: str
    output_batch_id: str
    as_of: datetime
    state: HistoricalStageState
    input_count: int
    output_count: int
    rejected_count: int
    pending_count: int
    failed_count: int
    reasons: tuple[HistoricalStageReason, ...]
    source_health: HistoricalSourceHealth
    latency_ms: int
    degraded: bool

    def __post_init__(self) -> None:
        if not isinstance(self.stage, HistoricalPipelineStage) or self.stage_order != _STAGES.index(self.stage) + 1:
            raise ValueError("historical stage order is invalid")
        if not isinstance(self.state, HistoricalStageState):
            raise ValueError("historical stage state is invalid")
        if not isinstance(self.source_health, HistoricalSourceHealth) or any(
            not isinstance(item, HistoricalStageReason) for item in self.reasons
        ):
            raise ValueError("historical stage evidence requires historical types")
        if not self.input_batch_id.strip() or not self.output_batch_id.strip():
            raise ValueError("historical batch identities are required")
        _require_shanghai(self.as_of)
        counts = (
            self.input_count,
            self.output_count,
            self.rejected_count,
            self.pending_count,
            self.failed_count,
            self.latency_ms,
        )
        if any(value < 0 for value in counts):
            raise ValueError("historical stage counts and latency cannot be negative")
        if self.output_count + self.rejected_count + self.pending_count + self.failed_count > self.input_count:
            raise ValueError("historical stage outcomes exceed input count")
        if self.degraded != (self.state is HistoricalStageState.DEGRADED):
            raise ValueError("historical degraded flag does not match stage state")
        if len({item.code for item in self.reasons}) != len(self.reasons):
            raise ValueError("historical stage reasons must be unique")


@dataclass(frozen=True)
class HistoricalMetricRange:
    metric: str
    minimum: float
    maximum: float

    def __post_init__(self) -> None:
        if not self.metric.strip():
            raise ValueError("historical metric is required")
        if not math.isfinite(self.minimum) or not math.isfinite(self.maximum) or self.minimum > self.maximum:
            raise ValueError("historical metric range is invalid")


@dataclass(frozen=True)
class HistoricalFacet:
    key: str
    count: int
    total: int | None

    def __post_init__(self) -> None:
        if _KEY.fullmatch(self.key) is None or self.count < 0:
            raise ValueError("historical facet is invalid")
        if self.total is not None and (self.total < 0 or self.count > self.total):
            raise ValueError("historical facet exceeds its total")


@dataclass(frozen=True)
class HistoricalReasonCount:
    reason: str
    count: int

    def __post_init__(self) -> None:
        if _KEY.fullmatch(self.reason) is None or self.count < 1:
            raise ValueError("historical reason count is invalid")


@dataclass(frozen=True)
class HistoricalStageStatus:
    key: str
    state: str
    input_count: int | None
    output_count: int | None
    metric_ranges: tuple[HistoricalMetricRange, ...]
    threshold: float | None
    facets: tuple[HistoricalFacet, ...]
    reason_counts: tuple[HistoricalReasonCount, ...]

    def __post_init__(self) -> None:
        if self.key not in _DIAGNOSTIC_ORDER or self.state not in _DIAGNOSTIC_STATES:
            raise ValueError("historical diagnostic key or state is invalid")
        if self.input_count is not None and self.input_count < 0:
            raise ValueError("historical diagnostic input count cannot be negative")
        if self.output_count is not None and self.output_count < 0:
            raise ValueError("historical diagnostic output count cannot be negative")
        if self.input_count is not None and self.output_count is not None and self.output_count > self.input_count:
            raise ValueError("historical diagnostic output exceeds input")
        if self.threshold is not None and not math.isfinite(self.threshold):
            raise ValueError("historical diagnostic threshold must be finite")
        if len({item.metric for item in self.metric_ranges}) != len(self.metric_ranges):
            raise ValueError("historical metrics must be unique")
        if len({item.key for item in self.facets}) != len(self.facets):
            raise ValueError("historical facets must be unique")
        if len({item.reason for item in self.reason_counts}) != len(self.reason_counts):
            raise ValueError("historical diagnostic reasons must be unique")


@dataclass(frozen=True)
class HistoricalPipelineAudit:
    current_stage: str
    stages: tuple[HistoricalStageStatus, ...]
    stage_snapshots: tuple[HistoricalStageSnapshot, ...] = ()

    def __post_init__(self) -> None:
        if any(not isinstance(item, HistoricalStageStatus) for item in self.stages):
            raise ValueError("historical audit requires historical diagnostic types")
        if tuple(item.key for item in self.stages) != _DIAGNOSTIC_ORDER or self.current_stage not in _DIAGNOSTIC_ORDER:
            raise ValueError("historical diagnostic stages are invalid")
        if not self.stage_snapshots:
            return
        if any(not isinstance(item, HistoricalStageSnapshot) for item in self.stage_snapshots):
            raise ValueError("historical audit requires historical stage types")
        if tuple(item.stage for item in self.stage_snapshots) != _STAGES:
            raise ValueError("historical audit must contain all fourteen stages in order")
        for previous, current in zip(self.stage_snapshots, self.stage_snapshots[1:], strict=False):
            if previous.output_batch_id != current.input_batch_id or previous.output_count != current.input_count:
                raise ValueError("historical audit handoffs must preserve identities and counts")


def _require_shanghai(value: datetime) -> None:
    if not isinstance(value.tzinfo, ZoneInfo) or value.tzinfo.key != "Asia/Shanghai":
        raise ValueError("historical audit time must use Asia/Shanghai")


__all__ = [
    "HistoricalFacet",
    "HistoricalMetricRange",
    "HistoricalPipelineAudit",
    "HistoricalPipelineStage",
    "HistoricalReasonCount",
    "HistoricalSeverity",
    "HistoricalSourceHealth",
    "HistoricalSourceState",
    "HistoricalStageReason",
    "HistoricalStageSnapshot",
    "HistoricalStageState",
    "HistoricalStageStatus",
]
