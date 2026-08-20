"""Immutable Score-R5 gate, forward evidence, and final-report values."""

from __future__ import annotations

import dataclasses
import math
import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

from trader.application.research.challenger_models import ChallengerDayReplay
from trader.application.research.replay_models import canonical_hash
from trader.domain.research.challengers import ChallengerVariantId
from trader.domain.research.specification import (
    SCORE_P0_V1_SPEC,
    ScoreResearchSpec,
    get_score_research_spec,
)
from trader.domain.research.statistics import VARIANT_FAMILY, HolmDecision, PairedBootstrapResult, bootstrap_seed

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REASON_CODE = re.compile(r"^[a-z0-9_]{1,64}$")
R5VariantState = Literal[
    "historical_rejected", "historical_passed", "forward_collecting", "forward_rejected", "promotion_eligible"
]
ForwardRecordStatus = Literal["valid", "failed", "no_decision"]


def score_r5_forward_dates(spec: ScoreResearchSpec = SCORE_P0_V1_SPEC) -> tuple[date, ...]:
    return spec.forward_dates


@dataclass(frozen=True)
class ScoreR5TrackMetrics:
    track: Literal["local_only", "hybrid"]
    day_count: int
    pair_count: int
    cost_mean_differences: tuple[float | None, float | None, float | None]
    bootstrap: tuple[PairedBootstrapResult, ...]
    baseline_severe_drawdown_rate: float | None
    challenger_severe_drawdown_rate: float | None
    candidate_recall: float | None
    delete_best_month_difference: float | None
    delete_best_month_status: Literal["deleted_positive_group", "no_positive_group", "insufficient_data"]
    delete_best_board_difference: float | None
    delete_best_board_status: Literal["deleted_positive_group", "no_positive_group", "insufficient_data"]
    maximum_stock_positive_contribution_fraction: float | None
    top_five_positive_contribution_fraction: float | None
    top_quintile_net_excess_20bp: float | None
    bottom_quintile_net_excess_20bp: float | None
    mean_rank_ic: float | None
    high_minus_low_severe_ci_lower: float | None
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        if self.day_count < 0 or self.pair_count < 0:
            raise ValueError("Score-R5 track counts cannot be negative")
        if tuple(item.block_days for item in self.bootstrap) != (3, 5, 10):
            raise ValueError("Score-R5 track must report fixed 3/5/10 day blocks")
        _optional_finite(
            (
                *self.cost_mean_differences,
                self.baseline_severe_drawdown_rate,
                self.challenger_severe_drawdown_rate,
                self.candidate_recall,
                self.delete_best_month_difference,
                self.delete_best_board_difference,
                self.maximum_stock_positive_contribution_fraction,
                self.top_five_positive_contribution_fraction,
                self.top_quintile_net_excess_20bp,
                self.bottom_quintile_net_excess_20bp,
                self.mean_rank_ic,
                self.high_minus_low_severe_ci_lower,
            )
        )
        for value in (
            self.baseline_severe_drawdown_rate,
            self.challenger_severe_drawdown_rate,
            self.candidate_recall,
            self.maximum_stock_positive_contribution_fraction,
            self.top_five_positive_contribution_fraction,
        ):
            if value is not None and not 0.0 <= value <= 1.0:
                raise ValueError("Score-R5 track rates must be in [0, 1]")
        object.__setattr__(self, "content_hash", canonical_hash(self))

    @property
    def primary_bootstrap(self) -> PairedBootstrapResult:
        return self.bootstrap[1]


@dataclass(frozen=True)
class ScoreR5HybridIncrement:
    cost_mean_differences: tuple[float | None, float | None, float | None]
    bootstrap: tuple[PairedBootstrapResult, ...]
    independent_gain_passed: bool
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        if tuple(item.block_days for item in self.bootstrap) != (3, 5, 10):
            raise ValueError("Score-R5 hybrid increment must report fixed 3/5/10 day blocks")
        _optional_finite(self.cost_mean_differences)
        primary = self.bootstrap[1]
        expected = bool(
            primary.valid
            and primary.confidence_lower is not None
            and primary.confidence_lower > 0.0
            and primary.p_value is not None
            and primary.p_value <= 0.05
        )
        if self.independent_gain_passed != expected:
            raise ValueError("Score-R5 hybrid gain status must match its primary bootstrap")
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class ScoreR5VariantGate:
    variant_id: ChallengerVariantId
    variant_version: str
    state: R5VariantState
    failure_reasons: tuple[str, ...]
    holm: HolmDecision
    local_track: ScoreR5TrackMetrics
    hybrid_track: ScoreR5TrackMetrics
    hybrid_increment: ScoreR5HybridIncrement
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        if self.holm.variant_id != self.variant_id:
            raise ValueError("Score-R5 variant must bind its own Holm decision")
        expected_version = {
            "continuous_entry": "continuous_entry_v1",
            "coverage_shrink": "coverage_shrink_v1",
            "candidate_upper_bound": "candidate_upper_bound_v1",
            "heat_weak_structure": "heat_weak_structure_v1",
            "combined_v1": "combined_v1",
        }[self.variant_id]
        if self.variant_version != expected_version:
            raise ValueError("Score-R5 variant identity and version do not match")
        if any(
            result.sample_count != self.local_track.day_count
            for result in (*self.local_track.bootstrap, *self.hybrid_increment.bootstrap)
        ) or any(result.sample_count != self.hybrid_track.day_count for result in self.hybrid_track.bootstrap):
            raise ValueError("Score-R5 bootstrap sample counts must match their tracks")
        reasons = tuple(sorted(set(self.failure_reasons)))
        if any(_REASON_CODE.fullmatch(reason) is None for reason in reasons):
            raise ValueError("Score-R5 failure reasons must be bounded reason codes")
        if self.state in {"historical_passed", "promotion_eligible"} and reasons:
            raise ValueError("Score-R5 passing state cannot carry failures")
        if self.state in {"historical_rejected", "forward_rejected"} and not reasons:
            raise ValueError("Score-R5 rejected state requires failures")
        object.__setattr__(self, "failure_reasons", reasons)
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class ScoreR5HistoricalReport:
    status: Literal["evaluated", "exploratory"]
    baseline_report_hash: str
    challenger_report_hash: str
    parameter_manifest_hash: str
    historical_day_count: int
    variants: tuple[ScoreR5VariantGate, ...]
    scope: Literal["historical", "forward", "combined"] = "historical"
    forward_dates: tuple[date, ...] = dataclasses.field(default_factory=score_r5_forward_dates)
    research_identity: str = dataclasses.field(
        default=SCORE_P0_V1_SPEC.research_identity,
        metadata={"exclude_from_v1_hash": True},
    )
    research_spec_hash: str = dataclasses.field(
        default=SCORE_P0_V1_SPEC.content_hash,
        metadata={"exclude_from_v1_hash": True},
    )
    schema_version: str = "score_r5_statistical_gate_v1"
    statistics_version: str = "score_r5_paired_mbb_holm_v1"
    report_version: str = "score_r5_final_report_v1"
    deepseek_http_request_delta: Literal[0] = 0
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        spec = get_score_research_spec(self.research_identity)
        _validate_r5_report_identity(self, spec)
        maximum_days = {"historical": 40, "forward": 20, "combined": 60}[self.scope]
        expected_days = maximum_days
        if self.historical_day_count < 0 or self.historical_day_count > maximum_days:
            raise ValueError("Score-R5 report day count exceeds its fixed scope")
        expected = "evaluated" if self.historical_day_count == expected_days else "exploratory"
        if self.status != expected or self.deepseek_http_request_delta != 0:
            raise ValueError("Score-R5 historical status or DeepSeek isolation is invalid")
        object.__setattr__(self, "content_hash", canonical_hash(self))


def _validate_r5_report_identity(report: ScoreR5HistoricalReport, spec: ScoreResearchSpec) -> None:
    generation = "v2" if report.research_identity == "score_p0_v2" else "v1"
    if (
        report.schema_version != f"score_r5_statistical_gate_{generation}"
        or report.statistics_version != f"score_r5_paired_mbb_holm_{generation}"
        or report.report_version != f"score_r5_final_report_{generation}"
    ):
        raise ValueError("Score-R5 report identity is invalid")
    for value in (report.baseline_report_hash, report.challenger_report_hash, report.parameter_manifest_hash):
        _hash(value)
    if report.research_spec_hash != spec.content_hash:
        raise ValueError("Score-R5 report research spec hash is invalid")
    if tuple(item.variant_id for item in report.variants) != VARIANT_FAMILY:
        raise ValueError("Score-R5 historical report requires the fixed five-variant family")
    if report.forward_dates != score_r5_forward_dates(spec):
        raise ValueError("Score-R5 forward dates cannot be shifted or replaced")
    for gate in report.variants:
        _validate_r5_gate_seeds(gate, spec)


def _validate_r5_gate_seeds(gate: ScoreR5VariantGate, spec: ScoreResearchSpec) -> None:
    results = (*gate.local_track.bootstrap, *gate.hybrid_track.bootstrap, *gate.hybrid_increment.bootstrap)
    if any(result.seed != bootstrap_seed(gate.variant_id, result.block_days, spec=spec) for result in results):
        raise ValueError("Score-R5 bootstrap result does not bind the preregistered seed")


@dataclass(frozen=True)
class ScoreR5ForwardBindings:
    historical_gate_hash: str
    variant_id: ChallengerVariantId
    variant_version: str
    parameter_manifest_hash: str
    data_identity_hash: str
    rule_identity_hash: str
    config_strategy_identity_hash: str
    research_identity: str = dataclasses.field(
        default=SCORE_P0_V1_SPEC.research_identity,
        metadata={"exclude_from_v1_hash": True},
    )
    research_spec_hash: str = dataclasses.field(
        default=SCORE_P0_V1_SPEC.content_hash,
        metadata={"exclude_from_v1_hash": True},
    )
    strategy_version: str = "strategy_review30_top6_observe6_2026_07"
    fusion_version: str = "fusion_local68_deepseek32"
    statistics_version: str = "score_r5_paired_mbb_holm_v1"
    report_version: str = "score_r5_final_report_v1"
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        for value in (
            self.historical_gate_hash,
            self.parameter_manifest_hash,
            self.data_identity_hash,
            self.rule_identity_hash,
            self.config_strategy_identity_hash,
            self.research_spec_hash,
        ):
            _hash(value)
        spec = get_score_research_spec(self.research_identity)
        if self.research_spec_hash != spec.content_hash:
            raise ValueError("Score-R5 forward binding research spec hash is invalid")
        expected_version = {
            "continuous_entry": "continuous_entry_v1",
            "coverage_shrink": "coverage_shrink_v1",
            "candidate_upper_bound": "candidate_upper_bound_v1",
            "heat_weak_structure": "heat_weak_structure_v1",
            "combined_v1": "combined_v1",
        }[self.variant_id]
        if self.variant_version != expected_version:
            raise ValueError("Score-R5 forward binding variant version is invalid")
        generation = "v2" if self.research_identity == "score_p0_v2" else "v1"
        if (
            self.statistics_version != f"score_r5_paired_mbb_holm_{generation}"
            or self.report_version != f"score_r5_final_report_{generation}"
        ):
            raise ValueError("Score-R5 forward binding report versions are invalid")
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class ScoreR5ForwardDayRecord:
    bindings: ScoreR5ForwardBindings
    planned_trade_date: date
    status: ForwardRecordStatus
    day: ChallengerDayReplay | None
    oracle_codes: tuple[str, ...]
    failure_reason: str | None
    schema_version: str = "score_r5_forward_day_v1"
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        generation = "v2" if self.bindings.research_identity == "score_p0_v2" else "v1"
        if self.schema_version != f"score_r5_forward_day_{generation}":
            raise ValueError("Score-R5 forward record schema is invalid")
        spec = get_score_research_spec(self.bindings.research_identity)
        if self.planned_trade_date not in score_r5_forward_dates(spec):
            raise ValueError("Score-R5 forward record date is outside the fixed window")
        if self.status == "failed":
            if (
                self.day is not None
                or self.oracle_codes
                or self.failure_reason is None
                or _REASON_CODE.fullmatch(self.failure_reason) is None
            ):
                raise ValueError("failed Score-R5 forward record requires only a failure reason")
        else:
            if self.day is None or self.failure_reason is not None or self.day.trade_date != self.planned_trade_date:
                raise ValueError("valid Score-R5 forward record requires a matching complete replay day")
            expected_status = "no_decision" if self.day.local_status == "no_decision" else "valid"
            if self.status != expected_status:
                raise ValueError("Score-R5 forward status must match the challenger selection")
            if len(set(self.oracle_codes)) != len(self.oracle_codes):
                raise ValueError("Score-R5 forward oracle codes must be unique")
            day_codes = {item.code for item in self.day.overrides}
            if not set(self.oracle_codes).issubset(day_codes):
                raise ValueError("Score-R5 forward oracle codes must belong to the replay active set")
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class ScoreR5FinalReport:
    state: Literal["forward_collecting", "forward_rejected", "promotion_eligible"]
    historical_report_hash: str
    forward_record_hashes: tuple[str, ...]
    forward_gate_report: ScoreR5HistoricalReport | None
    final_gate_report: ScoreR5HistoricalReport | None
    failure_reasons: tuple[str, ...]
    research_identity: str = dataclasses.field(
        default=SCORE_P0_V1_SPEC.research_identity,
        metadata={"exclude_from_v1_hash": True},
    )
    research_spec_hash: str = dataclasses.field(
        default=SCORE_P0_V1_SPEC.content_hash,
        metadata={"exclude_from_v1_hash": True},
    )
    schema_version: str = "score_r5_final_report_v1"
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        spec = get_score_research_spec(self.research_identity)
        generation = "v2" if self.research_identity == "score_p0_v2" else "v1"
        if self.research_spec_hash != spec.content_hash or self.schema_version != f"score_r5_final_report_{generation}":
            raise ValueError("Score-R5 final report schema is invalid")
        _hash(self.historical_report_hash)
        for value in self.forward_record_hashes:
            _hash(value)
        reasons = tuple(sorted(set(self.failure_reasons)))
        if self.state == "promotion_eligible" and (
            reasons or self.forward_gate_report is None or self.final_gate_report is None
        ):
            raise ValueError("Score-R5 promotion eligibility requires a complete passing final report")
        if self.state == "forward_rejected" and not reasons:
            raise ValueError("Score-R5 forward rejection requires failures")
        if self.state == "forward_collecting" and (reasons or self.forward_gate_report or self.final_gate_report):
            raise ValueError("Score-R5 collecting state cannot claim completed gate reports")
        if self.forward_gate_report is not None and self.forward_gate_report.scope != "forward":
            raise ValueError("Score-R5 final seal must bind a forward-only gate report")
        if self.final_gate_report is not None and self.final_gate_report.scope != "combined":
            raise ValueError("Score-R5 final seal must bind a combined 40+20 gate report")
        for report in (self.forward_gate_report, self.final_gate_report):
            if report is not None and (
                report.research_identity != self.research_identity
                or report.research_spec_hash != self.research_spec_hash
            ):
                raise ValueError("Score-R5 final seal research identity is inconsistent")
        object.__setattr__(self, "failure_reasons", reasons)
        object.__setattr__(self, "content_hash", canonical_hash(self))


def _hash(value: str) -> None:
    if _SHA256.fullmatch(value) is None:
        raise ValueError("Score-R5 identity must be SHA-256")


def _optional_finite(values: tuple[float | None, ...]) -> None:
    if any(value is not None and not math.isfinite(value) for value in values):
        raise ValueError("Score-R5 optional metrics must be finite")


__all__ = [
    "ForwardRecordStatus",
    "R5VariantState",
    "ScoreR5FinalReport",
    "ScoreR5ForwardBindings",
    "ScoreR5ForwardDayRecord",
    "ScoreR5HistoricalReport",
    "ScoreR5HybridIncrement",
    "ScoreR5TrackMetrics",
    "ScoreR5VariantGate",
    "score_r5_forward_dates",
]
