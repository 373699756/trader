"""Immutable, research-only decision trace values for tomorrow."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from trader.application.tomorrow_shadow_projection import TomorrowShadowProjection

UpperBoundStatus = Literal["not_computed"]
ShadowMode = Literal["control_copy", "reused_facts"]
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class TomorrowResearchTraceCapture:
    projection: TomorrowShadowProjection
    baseline_snapshot_id: str

    def __post_init__(self) -> None:
        if not self.baseline_snapshot_id:
            raise ValueError("paired research capture requires a baseline snapshot identity")


@dataclass(frozen=True)
class TomorrowResearchTraceRecorderStatus:
    attempts: int
    queued: int
    queue_full: int
    payload_too_large: int
    completed: int
    write_rejected: int
    worker_failed: int
    last_failure: str


@dataclass(frozen=True)
class TomorrowHardFilterAggregate:
    board: str
    reason: str
    count: int

    def __post_init__(self) -> None:
        if not self.board or not self.reason:
            raise ValueError("research hard-filter aggregate identity must not be empty")
        if self.count < 1:
            raise ValueError("research hard-filter aggregate count must be positive")


@dataclass(frozen=True)
class TomorrowCandidateResearchTrace:
    code: str
    board: str
    industry: str
    feature_input_hash: str
    candidate_components: tuple[tuple[str, float], ...]
    missing_mask: tuple[str, ...]
    coverage_ratio: float
    board_reliability: float
    candidate_score: float | None
    candidate_rank: int
    production_top120: bool
    optimistic_upper_bound: float | None
    upper_bound_status: UpperBoundStatus
    upper_bound_protected: bool
    pruning_reason: str

    def __post_init__(self) -> None:
        if not all((self.code, self.board, self.industry, self.feature_input_hash)):
            raise ValueError("research candidate identity must not be empty")
        if _SHA256.fullmatch(self.feature_input_hash) is None:
            raise ValueError("research candidate input identity must be SHA-256")
        components = _normalized_components(self.candidate_components, "candidate")
        if not components:
            raise ValueError("research candidate components must not be empty")
        missing_mask = tuple(sorted(set(self.missing_mask)))
        _validate_ratio(self.coverage_ratio, "candidate coverage")
        _validate_ratio(self.board_reliability, "board reliability")
        _validate_score(self.candidate_score, "candidate score")
        if self.candidate_rank < 0:
            raise ValueError("research candidate rank cannot be negative")
        if self.optimistic_upper_bound is not None or self.upper_bound_status != "not_computed":
            raise ValueError("P1 must not manufacture a candidate upper bound")
        if self.upper_bound_protected:
            raise ValueError("P1 cannot mark an upper-bound protection result")
        object.__setattr__(self, "candidate_components", components)
        object.__setattr__(self, "missing_mask", missing_mask)


@dataclass(frozen=True)
class TomorrowDecisionCandidateTrace:
    code: str
    components: tuple[tuple[str, float], ...]
    component_coverage_ratio: float
    base_score: float
    local_risk_codes: tuple[str, ...]
    local_risk_penalty: float
    local_score: float
    reused_deepseek_facts: bool
    fusion_applied: bool
    deepseek_risk_codes: tuple[str, ...]
    deepseek_risk_penalty: float
    final_score: float
    action: str
    downside_status: str
    downside_reasons: tuple[str, ...]
    setup_type: str
    selected: bool
    rank: int
    board_rank: int
    skip_reason: str

    def __post_init__(self) -> None:
        if not all((self.code, self.action, self.downside_status, self.setup_type)):
            raise ValueError("research decision candidate identity must not be empty")
        components = _normalized_components(self.components, "local")
        _validate_ratio(self.component_coverage_ratio, "local component coverage")
        for value, name in (
            (self.base_score, "base score"),
            (self.local_risk_penalty, "local risk penalty"),
            (self.local_score, "local score"),
            (self.deepseek_risk_penalty, "DeepSeek risk penalty"),
            (self.final_score, "final score"),
        ):
            _validate_score(value, name)
        if self.rank < 0 or self.board_rank < 0:
            raise ValueError("research decision ranks cannot be negative")
        if self.selected != (self.rank > 0):
            raise ValueError("research decision selected identity must match rank")
        if self.fusion_applied and not self.reused_deepseek_facts:
            raise ValueError("research fusion requires reusable DeepSeek facts")
        object.__setattr__(self, "components", components)
        object.__setattr__(self, "local_risk_codes", tuple(sorted(set(self.local_risk_codes))))
        object.__setattr__(self, "deepseek_risk_codes", tuple(sorted(set(self.deepseek_risk_codes))))
        object.__setattr__(self, "downside_reasons", tuple(sorted(set(self.downside_reasons))))


@dataclass(frozen=True)
class TomorrowDecisionSetTrace:
    variant: Literal["production_local", "research_shadow"]
    decision_version: str
    schema_version: str
    strategy_version: str
    fusion_version: str
    candidates: tuple[TomorrowDecisionCandidateTrace, ...]

    def __post_init__(self) -> None:
        if self.variant not in {"production_local", "research_shadow"}:
            raise ValueError("unsupported research decision variant")
        if not all((self.decision_version, self.schema_version, self.strategy_version, self.fusion_version)):
            raise ValueError("research decision version identity must not be empty")
        candidates = tuple(sorted(self.candidates, key=lambda item: item.code))
        if len({item.code for item in candidates}) != len(candidates):
            raise ValueError("research decision candidates must be unique")
        object.__setattr__(self, "candidates", candidates)


@dataclass(frozen=True)
class TomorrowResearchTrace:
    evaluated_at: datetime
    trade_date: date
    phase: str
    input_version: str
    input_manifest_hash: str
    data_version: str
    config_version: str
    rule_versions: tuple[str, ...]
    hard_filter_aggregate_hash: str
    received_population_by_board: tuple[tuple[str, int], ...]
    hard_filter_aggregates: tuple[TomorrowHardFilterAggregate, ...]
    source_coverage_status: str
    source_failure_categories: tuple[str, ...]
    passed_candidates: tuple[TomorrowCandidateResearchTrace, ...]
    production_local: TomorrowDecisionSetTrace
    research_shadow: TomorrowDecisionSetTrace
    shadow_mode: ShadowMode
    baseline_snapshot_id: str
    deepseek_request_delta: int
    schema_version: str = "tomorrow_research_trace_v1"
    engine_version: str = "tomorrow_research_trace_engine_v1"

    def __post_init__(self) -> None:
        _validate_trace_identity(self)
        populations = _normalized_populations(self.received_population_by_board)
        aggregates = _normalized_aggregates(self.hard_filter_aggregates)
        candidates = _normalized_candidates(self.passed_candidates)
        _validate_trace_decisions(self, candidates)
        object.__setattr__(self, "rule_versions", tuple(sorted(set(self.rule_versions))))
        object.__setattr__(self, "received_population_by_board", populations)
        object.__setattr__(self, "hard_filter_aggregates", aggregates)
        object.__setattr__(self, "source_failure_categories", tuple(sorted(set(self.source_failure_categories))))
        object.__setattr__(self, "passed_candidates", candidates)


def _validate_trace_identity(trace: TomorrowResearchTrace) -> None:
    if not trace.baseline_snapshot_id:
        raise ValueError("paired research trace requires a baseline snapshot identity")
    _require_shanghai(trace.evaluated_at)
    if trace.trade_date != trace.evaluated_at.date():
        raise ValueError("research trace trade date must match evaluation time")
    identity = (
        trace.phase,
        trace.input_version,
        trace.input_manifest_hash,
        trace.data_version,
        trace.config_version,
        trace.hard_filter_aggregate_hash,
        trace.source_coverage_status,
        trace.schema_version,
        trace.engine_version,
    )
    if not all(identity):
        raise ValueError("research trace identity must not be empty")
    if _SHA256.fullmatch(trace.input_manifest_hash) is None:
        raise ValueError("research input manifest identity must be SHA-256")
    if _SHA256.fullmatch(trace.hard_filter_aggregate_hash) is None:
        raise ValueError("research hard-filter aggregate identity must be SHA-256")
    if not trace.rule_versions or any(not item for item in trace.rule_versions):
        raise ValueError("research rule versions must not be empty")
    if trace.deepseek_request_delta != 0:
        raise ValueError("research trace cannot add DeepSeek requests")


def _normalized_populations(values: tuple[tuple[str, int], ...]) -> tuple[tuple[str, int], ...]:
    populations = tuple(sorted(values))
    if any(not board or count < 0 for board, count in populations):
        raise ValueError("research population counts must be non-negative")
    if len({board for board, _count in populations}) != len(populations):
        raise ValueError("research population boards must be unique")
    return populations


def _normalized_aggregates(
    values: tuple[TomorrowHardFilterAggregate, ...],
) -> tuple[TomorrowHardFilterAggregate, ...]:
    aggregates = tuple(sorted(values, key=lambda item: (item.board, item.reason)))
    if len({(item.board, item.reason) for item in aggregates}) != len(aggregates):
        raise ValueError("research hard-filter aggregates must be unique")
    return aggregates


def _normalized_candidates(
    values: tuple[TomorrowCandidateResearchTrace, ...],
) -> tuple[TomorrowCandidateResearchTrace, ...]:
    candidates = tuple(sorted(values, key=lambda item: item.code))
    if len({item.code for item in candidates}) != len(candidates):
        raise ValueError("research passed candidates must be unique")
    return candidates


def _validate_trace_decisions(
    trace: TomorrowResearchTrace,
    candidates: tuple[TomorrowCandidateResearchTrace, ...],
) -> None:
    if trace.production_local.variant != "production_local" or trace.research_shadow.variant != "research_shadow":
        raise ValueError("research trace decision variants are inconsistent")
    passed_codes = {item.code for item in candidates}
    decision_codes = {
        item.code
        for decision_set in (trace.production_local, trace.research_shadow)
        for item in decision_set.candidates
    }
    if not decision_codes.issubset(passed_codes):
        raise ValueError("research decisions must belong to the hard-filter passed population")
    if trace.shadow_mode == "control_copy" and (
        trace.production_local.decision_version != trace.research_shadow.decision_version
        or trace.production_local.candidates != trace.research_shadow.candidates
    ):
        raise ValueError("research control copy must preserve the production decision")
    if trace.shadow_mode == "reused_facts" and not any(
        item.reused_deepseek_facts for item in trace.research_shadow.candidates
    ):
        raise ValueError("research reused-facts shadow must reference existing facts")


def _normalized_components(values: tuple[tuple[str, float], ...], label: str) -> tuple[tuple[str, float], ...]:
    normalized = tuple(sorted(values))
    if any(not name or not math.isfinite(value) or not 0.0 <= value <= 100.0 for name, value in normalized):
        raise ValueError(f"research {label} components must contain finite scores")
    if len({name for name, _value in normalized}) != len(normalized):
        raise ValueError(f"research {label} component names must be unique")
    return normalized


def _validate_ratio(value: float, label: str) -> None:
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"research {label} must be in [0, 1]")


def _validate_score(value: float | None, label: str) -> None:
    if value is not None and (not math.isfinite(value) or not 0.0 <= value <= 100.0):
        raise ValueError(f"research {label} must be in [0, 100]")


def _require_shanghai(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None or getattr(value.tzinfo, "key", None) != "Asia/Shanghai":
        raise ValueError("research trace time must use Asia/Shanghai")


__all__ = [
    "ShadowMode",
    "TomorrowCandidateResearchTrace",
    "TomorrowDecisionCandidateTrace",
    "TomorrowDecisionSetTrace",
    "TomorrowHardFilterAggregate",
    "TomorrowResearchTrace",
    "TomorrowResearchTraceCapture",
    "TomorrowResearchTraceRecorderStatus",
    "UpperBoundStatus",
]
