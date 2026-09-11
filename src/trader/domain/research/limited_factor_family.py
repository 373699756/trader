"""Point-in-time, preregistered evaluation for one limited factor family."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from trader.domain.market.feature_contracts import TOMORROW_RAW_ALPHA_FEATURE_MANIFEST
from trader.domain.research.artifact_identity import canonical_artifact_hash
from trader.domain.research.paired_statistics import (
    PreregisteredBootstrapPlan,
    PreregisteredBootstrapResult,
    PreregisteredHolmDecision,
    fixed_family_holm,
    paired_moving_block_statistics,
)

LimitedFactorFamilyState = Literal[
    "factor_family_confirmed",
    "historical_rejected",
    "historical_data_insufficient",
]
LimitedFactorChangeKind = Literal["control", "candidate"]

_HASH = re.compile(r"^[0-9a-f]{64}$")
_IDENTITY = re.compile(r"^[a-z0-9_]{1,96}$")
_COST_BPS = (20, 50, 100)


@dataclass(frozen=True)
class LimitedFactorCandidate:
    candidate_id: str
    formula_id: str
    unit: str
    change_kind: LimitedFactorChangeKind = "candidate"

    def __post_init__(self) -> None:
        if any(_IDENTITY.fullmatch(value) is None for value in (self.candidate_id, self.formula_id, self.unit)):
            raise ValueError("limited factor candidate identity is invalid")
        if self.change_kind not in {"control", "candidate"}:
            raise ValueError("limited factor candidate change kind is invalid")
        if (self.formula_id == "control") != (self.change_kind == "control"):
            raise ValueError("limited factor control identity is inconsistent")


LIMITED_FACTOR_CONTROL_FEATURES = TOMORROW_RAW_ALPHA_FEATURE_MANIFEST.names
LIMITED_INTRADAY_CANDIDATES = (
    LimitedFactorCandidate("existing_six_alpha", "control", "unitless", "control"),
    LimitedFactorCandidate("overnight_gap", "overnight_gap", "decimal_return"),
    LimitedFactorCandidate("open_to_anchor_return", "open_to_anchor_return", "decimal_return"),
    LimitedFactorCandidate("return_5m", "return_5m", "decimal_return"),
    LimitedFactorCandidate("return_15m", "return_15m", "decimal_return"),
    LimitedFactorCandidate("return_30m", "return_30m", "decimal_return"),
    LimitedFactorCandidate("vwap_deviation", "vwap_deviation", "decimal_return"),
    LimitedFactorCandidate("tail_volume_share", "tail_volume_share", "ratio"),
)


@dataclass(frozen=True)
class LimitedFactorFamilySpec:
    family_id: str
    control_feature_ids: tuple[str, ...]
    candidates: tuple[LimitedFactorCandidate, ...]
    selected_candidate_id: str
    development_dates: tuple[date, ...]
    confirmation_dates: tuple[date, ...]
    minimum_coverage: float = 0.95
    holm_alpha: float = 0.05
    bootstrap_master_seed: int = 20260909
    bootstrap_repetitions: int = 10_000
    bootstrap_block_days: int = 5
    cost_bps: tuple[int, int, int] = _COST_BPS
    anchor: Literal["14:50_point_in_time"] = "14:50_point_in_time"
    terminal_holdout_opened: bool = False
    production_authority: bool = False
    schema_version: str = "limited_factor_family_spec"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        candidates = tuple(self.candidates)
        control_features = tuple(self.control_feature_ids)
        development = tuple(self.development_dates)
        confirmation = tuple(self.confirmation_dates)
        if _IDENTITY.fullmatch(self.family_id) is None or len(control_features) != 6:
            raise ValueError("limited factor family identity or control is invalid")
        if any(_IDENTITY.fullmatch(value) is None for value in control_features):
            raise ValueError("limited factor control feature identity is invalid")
        if not 2 <= len(candidates) <= 8 or candidates[0].change_kind != "control":
            raise ValueError("limited factor family requires one control and one to seven candidates")
        ids = tuple(item.candidate_id for item in candidates)
        if len(ids) != len(set(ids)) or self.selected_candidate_id not in ids[1:]:
            raise ValueError("limited factor selected candidate must be preregistered")
        _ordered_dates(development, "development")
        _ordered_dates(confirmation, "confirmation")
        if development[-1] >= confirmation[0]:
            raise ValueError("limited factor development must precede confirmation")
        if (
            not math.isclose(self.minimum_coverage, 0.95)
            or not math.isclose(self.holm_alpha, 0.05)
            or self.bootstrap_master_seed < 1
            or self.bootstrap_repetitions < 100
            or self.bootstrap_block_days != 5
            or self.cost_bps != _COST_BPS
            or self.anchor != "14:50_point_in_time"
            or self.terminal_holdout_opened
            or self.production_authority
            or self.schema_version != "limited_factor_family_spec"
        ):
            raise ValueError("limited factor family fixed research boundary is invalid")
        object.__setattr__(self, "control_feature_ids", control_features)
        object.__setattr__(self, "candidates", candidates)
        object.__setattr__(self, "development_dates", development)
        object.__setattr__(self, "confirmation_dates", confirmation)
        object.__setattr__(self, "content_hash", canonical_artifact_hash(self))

    @property
    def research_dates(self) -> tuple[date, ...]:
        return (*self.development_dates, *self.confirmation_dates)


def build_intraday_price_volume_family_spec(
    *,
    development_dates: tuple[date, ...],
    confirmation_dates: tuple[date, ...],
    bootstrap_repetitions: int = 10_000,
) -> LimitedFactorFamilySpec:
    """Return the one sealed family authorized by the current research chapter."""

    return LimitedFactorFamilySpec(
        family_id="intraday_price_volume_path",
        control_feature_ids=LIMITED_FACTOR_CONTROL_FEATURES,
        candidates=LIMITED_INTRADAY_CANDIDATES,
        selected_candidate_id="tail_volume_share",
        development_dates=development_dates,
        confirmation_dates=confirmation_dates,
        bootstrap_repetitions=bootstrap_repetitions,
    )


@dataclass(frozen=True)
class FactorFamilyCandidateSeries:
    candidate_id: str
    trade_dates: tuple[date, ...]
    overall_coverage: float
    board_coverages: tuple[float, float, float]
    paired_increment_20bp: tuple[float, ...]
    paired_increment_50bp: tuple[float, ...]
    paired_increment_100bp: tuple[float, ...]
    rank_ic_delta: tuple[float, ...]
    top10_increment: tuple[float, ...]
    top20_increment: tuple[float, ...]
    top50_increment: tuple[float, ...]
    quintile_spread: tuple[float, ...]
    severe_loss_rate_delta: tuple[float, ...]
    maximum_drawdown_delta: tuple[float, ...]
    turnover_delta: tuple[float, ...]
    capacity_shortfall_delta: tuple[float, ...]
    market_state_directions: tuple[int, ...]

    def __post_init__(self) -> None:
        dates = tuple(self.trade_dates)
        if _IDENTITY.fullmatch(self.candidate_id) is None:
            raise ValueError("limited factor series identity is invalid")
        _ordered_dates(dates, "series")
        vectors = (
            self.paired_increment_20bp,
            self.paired_increment_50bp,
            self.paired_increment_100bp,
            self.rank_ic_delta,
            self.top10_increment,
            self.top20_increment,
            self.top50_increment,
            self.quintile_spread,
            self.severe_loss_rate_delta,
            self.maximum_drawdown_delta,
            self.turnover_delta,
            self.capacity_shortfall_delta,
        )
        if any(len(vector) != len(dates) for vector in vectors):
            raise ValueError("limited factor series vectors must align by date")
        if any(not math.isfinite(value) for vector in vectors for value in vector):
            raise ValueError("limited factor series values must be finite")
        coverages = (self.overall_coverage, *self.board_coverages)
        if any(not math.isfinite(value) or not 0.0 <= value <= 1.0 for value in coverages):
            raise ValueError("limited factor coverage must be in [0, 1]")
        if not self.market_state_directions or any(value not in {-1, 0, 1} for value in self.market_state_directions):
            raise ValueError("limited factor market-state directions are invalid")
        object.__setattr__(self, "trade_dates", dates)


@dataclass(frozen=True)
class LimitedFactorCandidateEvidence:
    candidate_id: str
    bootstrap_20bp: PreregisteredBootstrapResult
    bootstrap_50bp: PreregisteredBootstrapResult
    holm: PreregisteredHolmDecision
    mean_increment_20bp: float
    mean_increment_50bp: float
    mean_increment_100bp: float
    mean_rank_ic_delta: float
    mean_top10_increment: float
    mean_top20_increment: float
    mean_top50_increment: float
    mean_quintile_spread: float
    mean_severe_loss_rate_delta: float
    mean_maximum_drawdown_delta: float
    mean_turnover_delta: float
    mean_capacity_shortfall_delta: float
    overall_coverage: float
    board_coverages: tuple[float, float, float]
    market_state_directions: tuple[int, ...]
    passed: bool
    failure_reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if _IDENTITY.fullmatch(self.candidate_id) is None:
            raise ValueError("limited factor evidence identity is invalid")
        if self.bootstrap_20bp.sample_count != self.bootstrap_50bp.sample_count:
            raise ValueError("limited factor bootstrap evidence must share the confirmation dates")
        if self.holm.challenger_id != self.candidate_id:
            raise ValueError("limited factor Holm evidence belongs to another candidate")
        metrics = (
            self.mean_increment_20bp,
            self.mean_increment_50bp,
            self.mean_increment_100bp,
            self.mean_rank_ic_delta,
            self.mean_top10_increment,
            self.mean_top20_increment,
            self.mean_top50_increment,
            self.mean_quintile_spread,
            self.mean_severe_loss_rate_delta,
            self.mean_maximum_drawdown_delta,
            self.mean_turnover_delta,
            self.mean_capacity_shortfall_delta,
        )
        if any(not math.isfinite(value) for value in metrics):
            raise ValueError("limited factor evidence metrics must be finite")
        if any(not 0.0 <= value <= 1.0 for value in (self.overall_coverage, *self.board_coverages)):
            raise ValueError("limited factor evidence coverage must be in [0, 1]")
        if not self.market_state_directions or any(value not in {-1, 0, 1} for value in self.market_state_directions):
            raise ValueError("limited factor evidence market states are invalid")
        if self.passed == bool(self.failure_reasons):
            raise ValueError("limited factor evidence status and failures disagree")


@dataclass(frozen=True)
class LimitedFactorFamilyReport:
    state: LimitedFactorFamilyState
    spec_hash: str
    dataset_report_hash: str
    dataset_manifest_hash: str | None
    recall_report_hash: str
    evidence: tuple[LimitedFactorCandidateEvidence, ...]
    selected_candidate_id: str | None
    failure_reasons: tuple[str, ...]
    terminal_holdout_opened: bool = False
    production_authority: bool = False
    schema_version: str = "limited_factor_family_report"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        evidence = tuple(self.evidence)
        reasons = tuple(sorted(set(self.failure_reasons)))
        _validate_report_identity(self, reasons)
        _validate_report_outcome(self, evidence, reasons)
        if self.terminal_holdout_opened or self.production_authority:
            raise ValueError("limited factor report cannot open holdout or authorize production")
        if self.schema_version != "limited_factor_family_report":
            raise ValueError("limited factor report schema is invalid")
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "failure_reasons", reasons)
        object.__setattr__(self, "content_hash", canonical_artifact_hash(self))


def _validate_report_identity(report: LimitedFactorFamilyReport, reasons: tuple[str, ...]) -> None:
    hashes = (report.spec_hash, report.dataset_report_hash, report.recall_report_hash)
    if any(_HASH.fullmatch(value) is None for value in hashes):
        raise ValueError("limited factor report parent identity is invalid")
    if report.dataset_manifest_hash is not None and _HASH.fullmatch(report.dataset_manifest_hash) is None:
        raise ValueError("limited factor report manifest identity is invalid")
    if any(_IDENTITY.fullmatch(value) is None for value in reasons):
        raise ValueError("limited factor report failure reason is invalid")


def _validate_report_outcome(
    report: LimitedFactorFamilyReport,
    evidence: tuple[LimitedFactorCandidateEvidence, ...],
    reasons: tuple[str, ...],
) -> None:
    ids = tuple(item.candidate_id for item in evidence)
    if len(ids) != len(set(ids)):
        raise ValueError("limited factor report evidence must contain unique candidates")
    if report.state == "factor_family_confirmed":
        selected = tuple(item for item in evidence if item.candidate_id == report.selected_candidate_id)
        if report.dataset_manifest_hash is None or len(selected) != 1 or not selected[0].passed or reasons:
            raise ValueError("confirmed limited factor report is incomplete")
    elif report.state == "historical_rejected":
        if (
            report.dataset_manifest_hash is None
            or not evidence
            or report.selected_candidate_id is not None
            or not reasons
        ):
            raise ValueError("rejected limited factor report is incomplete")
    elif report.state == "historical_data_insufficient":
        if evidence or report.selected_candidate_id is not None or not reasons:
            raise ValueError("insufficient limited factor report must fail closed")
    else:
        raise ValueError("limited factor report state is invalid")


def evaluate_limited_factor_family(
    spec: LimitedFactorFamilySpec,
    *,
    dataset_report_hash: str,
    dataset_manifest_hash: str,
    recall_report_hash: str,
    series: tuple[FactorFamilyCandidateSeries, ...],
) -> LimitedFactorFamilyReport:
    """Evaluate the sealed family without selecting a different winner after outcomes."""

    expected_ids = tuple(item.candidate_id for item in spec.candidates[1:])
    supplied = tuple(series)
    if tuple(item.candidate_id for item in supplied) != expected_ids:
        raise ValueError("limited factor evidence must contain the complete preregistered challenger family")
    if any(item.trade_dates != spec.research_dates for item in supplied):
        raise ValueError("limited factor evidence dates must match the sealed split")
    bootstraps_20 = {
        item.candidate_id: _bootstrap(
            spec,
            item.candidate_id,
            "20bp",
            _confirmation_values(spec, item.trade_dates, item.paired_increment_20bp),
        )
        for item in supplied
    }
    holm = fixed_family_holm(
        {candidate_id: result.p_value for candidate_id, result in bootstraps_20.items()},
        family=expected_ids,
        alpha=spec.holm_alpha,
    )
    holm_by_id = {item.challenger_id: item for item in holm}
    evidence = tuple(
        _candidate_evidence(spec, item, bootstraps_20[item.candidate_id], holm_by_id[item.candidate_id])
        for item in supplied
    )
    selected = next(item for item in evidence if item.candidate_id == spec.selected_candidate_id)
    state: LimitedFactorFamilyState = "factor_family_confirmed" if selected.passed else "historical_rejected"
    return LimitedFactorFamilyReport(
        state=state,
        spec_hash=spec.content_hash,
        dataset_report_hash=dataset_report_hash,
        dataset_manifest_hash=dataset_manifest_hash,
        recall_report_hash=recall_report_hash,
        evidence=evidence,
        selected_candidate_id=spec.selected_candidate_id if selected.passed else None,
        failure_reasons=() if selected.passed else selected.failure_reasons,
    )


def insufficient_limited_factor_family_report(
    spec: LimitedFactorFamilySpec,
    *,
    dataset_report_hash: str,
    dataset_manifest_hash: str | None,
    recall_report_hash: str,
    reason: str,
) -> LimitedFactorFamilyReport:
    return LimitedFactorFamilyReport(
        state="historical_data_insufficient",
        spec_hash=spec.content_hash,
        dataset_report_hash=dataset_report_hash,
        dataset_manifest_hash=dataset_manifest_hash,
        recall_report_hash=recall_report_hash,
        evidence=(),
        selected_candidate_id=None,
        failure_reasons=(reason,),
    )


def _candidate_evidence(
    spec: LimitedFactorFamilySpec,
    series: FactorFamilyCandidateSeries,
    bootstrap_20bp: PreregisteredBootstrapResult,
    holm: PreregisteredHolmDecision,
) -> LimitedFactorCandidateEvidence:
    confirmation = _confirmation_series(spec, series)
    bootstrap_50bp = _bootstrap(spec, series.candidate_id, "50bp", confirmation.paired_increment_50bp)
    failures = _gate_failures(spec, confirmation, bootstrap_20bp, bootstrap_50bp, holm)
    return LimitedFactorCandidateEvidence(
        candidate_id=series.candidate_id,
        bootstrap_20bp=bootstrap_20bp,
        bootstrap_50bp=bootstrap_50bp,
        holm=holm,
        mean_increment_20bp=_mean(confirmation.paired_increment_20bp),
        mean_increment_50bp=_mean(confirmation.paired_increment_50bp),
        mean_increment_100bp=_mean(confirmation.paired_increment_100bp),
        mean_rank_ic_delta=_mean(confirmation.rank_ic_delta),
        mean_top10_increment=_mean(confirmation.top10_increment),
        mean_top20_increment=_mean(confirmation.top20_increment),
        mean_top50_increment=_mean(confirmation.top50_increment),
        mean_quintile_spread=_mean(confirmation.quintile_spread),
        mean_severe_loss_rate_delta=_mean(confirmation.severe_loss_rate_delta),
        mean_maximum_drawdown_delta=_mean(confirmation.maximum_drawdown_delta),
        mean_turnover_delta=_mean(confirmation.turnover_delta),
        mean_capacity_shortfall_delta=_mean(confirmation.capacity_shortfall_delta),
        overall_coverage=series.overall_coverage,
        board_coverages=series.board_coverages,
        market_state_directions=series.market_state_directions,
        passed=not failures,
        failure_reasons=failures,
    )


def _gate_failures(
    spec: LimitedFactorFamilySpec,
    series: FactorFamilyCandidateSeries,
    bootstrap_20bp: PreregisteredBootstrapResult,
    bootstrap_50bp: PreregisteredBootstrapResult,
    holm: PreregisteredHolmDecision,
) -> tuple[str, ...]:
    failures: list[str] = []
    if min(series.overall_coverage, *series.board_coverages) < spec.minimum_coverage:
        failures.append("coverage_below_required")
    if not holm.rejected_null:
        failures.append("holm_not_significant")
    if bootstrap_20bp.confidence_lower is None or bootstrap_20bp.confidence_lower <= 0.0:
        failures.append("bootstrap_20bp_lower_not_positive")
    if bootstrap_50bp.confidence_lower is None or bootstrap_50bp.confidence_lower <= 0.0:
        failures.append("bootstrap_50bp_lower_not_positive")
    for values, reason in (
        (series.rank_ic_delta, "rank_ic_not_improved"),
        (series.top10_increment, "top10_not_improved"),
        (series.top20_increment, "top20_not_improved"),
        (series.top50_increment, "top50_not_improved"),
        (series.quintile_spread, "quintile_spread_not_positive"),
    ):
        if _mean(values) <= 0.0:
            failures.append(reason)
    for values, reason in (
        (series.severe_loss_rate_delta, "severe_loss_worsened"),
        (series.maximum_drawdown_delta, "maximum_drawdown_worsened"),
        (series.turnover_delta, "turnover_worsened"),
        (series.capacity_shortfall_delta, "capacity_worsened"),
    ):
        if _mean(values) > 0.0:
            failures.append(reason)
    if any(value < 0 for value in series.market_state_directions):
        failures.append("market_state_direction_reversed")
    return tuple(sorted(failures))


def _confirmation_series(
    spec: LimitedFactorFamilySpec,
    series: FactorFamilyCandidateSeries,
) -> FactorFamilyCandidateSeries:
    positions = tuple(index for index, value in enumerate(series.trade_dates) if value in set(spec.confirmation_dates))
    return FactorFamilyCandidateSeries(
        candidate_id=series.candidate_id,
        trade_dates=spec.confirmation_dates,
        overall_coverage=series.overall_coverage,
        board_coverages=series.board_coverages,
        paired_increment_20bp=tuple(series.paired_increment_20bp[index] for index in positions),
        paired_increment_50bp=tuple(series.paired_increment_50bp[index] for index in positions),
        paired_increment_100bp=tuple(series.paired_increment_100bp[index] for index in positions),
        rank_ic_delta=tuple(series.rank_ic_delta[index] for index in positions),
        top10_increment=tuple(series.top10_increment[index] for index in positions),
        top20_increment=tuple(series.top20_increment[index] for index in positions),
        top50_increment=tuple(series.top50_increment[index] for index in positions),
        quintile_spread=tuple(series.quintile_spread[index] for index in positions),
        severe_loss_rate_delta=tuple(series.severe_loss_rate_delta[index] for index in positions),
        maximum_drawdown_delta=tuple(series.maximum_drawdown_delta[index] for index in positions),
        turnover_delta=tuple(series.turnover_delta[index] for index in positions),
        capacity_shortfall_delta=tuple(series.capacity_shortfall_delta[index] for index in positions),
        market_state_directions=series.market_state_directions,
    )


def _confirmation_values(
    spec: LimitedFactorFamilySpec,
    dates: tuple[date, ...],
    values: tuple[float, ...],
) -> tuple[float, ...]:
    confirmation_dates = set(spec.confirmation_dates)
    return tuple(value for trade_date, value in zip(dates, values, strict=True) if trade_date in confirmation_dates)


def _bootstrap(
    spec: LimitedFactorFamilySpec,
    candidate_id: str,
    metric: str,
    values: tuple[float, ...],
) -> PreregisteredBootstrapResult:
    return paired_moving_block_statistics(
        values,
        plan=PreregisteredBootstrapPlan(
            identity=f"limited_factor_{spec.family_id}_{metric}",
            master_seed=spec.bootstrap_master_seed,
            challenger_id=candidate_id,
            block_days=spec.bootstrap_block_days,
            repetitions=spec.bootstrap_repetitions,
        ),
    )


def _ordered_dates(values: tuple[date, ...], date_set_name: str) -> None:
    if not values or tuple(sorted(set(values))) != values:
        raise ValueError(f"limited factor {date_set_name} dates must be strictly ordered")


def _mean(values: tuple[float, ...]) -> float:
    return math.fsum(values) / len(values)


__all__ = [
    "FactorFamilyCandidateSeries",
    "LIMITED_FACTOR_CONTROL_FEATURES",
    "LIMITED_INTRADAY_CANDIDATES",
    "LimitedFactorCandidate",
    "LimitedFactorCandidateEvidence",
    "LimitedFactorFamilyReport",
    "LimitedFactorFamilySpec",
    "LimitedFactorFamilyState",
    "build_intraday_price_volume_family_spec",
    "evaluate_limited_factor_family",
    "insufficient_limited_factor_family_report",
]
