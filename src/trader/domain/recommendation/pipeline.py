"""Immutable recommendation-pipeline audit carried by scored decisions."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Literal

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
PIPELINE_STAGE_ORDER: tuple[PipelineStageKey, ...] = (
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
        if keys != PIPELINE_STAGE_ORDER or self.current_stage not in keys:
            raise ValueError("recommendation pipeline stages are invalid")

    def stage(self, key: PipelineStageKey) -> PipelineStageStatus:
        return next(item for item in self.stages if item.key == key)


__all__ = [
    "PIPELINE_STAGE_ORDER",
    "PipelineFacet",
    "PipelineMetricName",
    "PipelineMetricRange",
    "PipelineReasonCount",
    "PipelineStageKey",
    "PipelineStageState",
    "PipelineStageStatus",
    "RecommendationPipelineStatus",
]
