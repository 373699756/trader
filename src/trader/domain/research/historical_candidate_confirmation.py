"""Date-ordered confirmation and Holm control for transparent candidates."""

from __future__ import annotations

import dataclasses
import math
import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

from trader.domain.research.paired_statistics import (
    PreregisteredBootstrapPlan,
    PreregisteredBootstrapResult,
    PreregisteredHolmDecision,
    fixed_family_holm,
    paired_moving_block_statistics,
)
from trader.domain.research.transparent_candidate import TransparentCandidateFamily

ConfirmationStatus = Literal["historical_candidate_ready", "historical_rejected", "historical_data_insufficient"]
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class CandidateConfirmationSeries:
    candidate_id: str
    trade_dates: tuple[date, ...]
    paired_increment_20bp: tuple[float, ...]
    paired_increment_50bp: tuple[float, ...]
    severe_loss_rate_delta: tuple[float, ...]
    turnover_delta: tuple[float, ...] = ()
    capacity_delta: tuple[float, ...] = ()
    concentration_delta: tuple[float, ...] = ()
    candidate_net_excess_20bp: tuple[float, ...] = ()
    candidate_net_excess_50bp: tuple[float, ...] = ()
    development_fold_directions: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        n = len(self.trade_dates)
        if not self.candidate_id or n < 1 or tuple(sorted(set(self.trade_dates))) != self.trade_dates:
            raise ValueError("confirmation series dates or identity are invalid")
        vectors = (self.paired_increment_20bp, self.paired_increment_50bp, self.severe_loss_rate_delta)
        if any(len(vector) != n for vector in vectors):
            raise ValueError("confirmation series vectors must align by date")
        for vector in (
            *vectors,
            self.turnover_delta,
            self.capacity_delta,
            self.concentration_delta,
            self.candidate_net_excess_20bp,
            self.candidate_net_excess_50bp,
        ):
            if vector and len(vector) != n:
                raise ValueError("confirmation diagnostic vectors must align by date")
            if any(not math.isfinite(value) for value in vector):
                raise ValueError("confirmation series values must be finite")
        if self.development_fold_directions and (
            len(self.development_fold_directions) != 5
            or any(value not in {-1, 0, 1} for value in self.development_fold_directions)
        ):
            raise ValueError("confirmation requires five signed development fold directions")


@dataclass(frozen=True)
class CandidateConfirmationEvidence:
    candidate_id: str
    bootstrap_20bp: PreregisteredBootstrapResult
    bootstrap_50bp: PreregisteredBootstrapResult
    mean_increment_20bp: float
    mean_increment_50bp: float
    severe_loss_rate_delta: float
    turnover_delta: float
    capacity_delta: float
    concentration_delta: float
    holm: PreregisteredHolmDecision
    passed: bool
    failure_reasons: tuple[str, ...]
    mean_absolute_20bp: float | None = None
    mean_absolute_50bp: float | None = None
    absolute_bootstrap_20bp: PreregisteredBootstrapResult | None = None
    absolute_bootstrap_50bp: PreregisteredBootstrapResult | None = None
    development_fold_directions: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if not self.candidate_id or any(
            not math.isfinite(value)
            for value in (
                self.mean_increment_20bp,
                self.mean_increment_50bp,
                self.severe_loss_rate_delta,
                self.turnover_delta,
                self.capacity_delta,
                self.concentration_delta,
            )
        ):
            raise ValueError("confirmation evidence is invalid")
        if self.passed == bool(self.failure_reasons):
            raise ValueError("confirmation evidence status and failures disagree")
        if any(
            value is not None and not math.isfinite(value)
            for value in (self.mean_absolute_20bp, self.mean_absolute_50bp)
        ):
            raise ValueError("confirmation absolute evidence must be finite")


@dataclass(frozen=True)
class HistoricalCandidateConfirmationReport:
    strategy: str
    family_hash: str
    confirmation_dates: tuple[date, ...]
    holm_family: tuple[str, ...]
    evidence: tuple[CandidateConfirmationEvidence, ...]
    selected_candidate_id: str | None
    status: ConfirmationStatus
    terminal_holdout_status: Literal["terminal_holdout_not_opened"] = "terminal_holdout_not_opened"
    production_authority: bool = False
    schema_version: str = "historical_candidate_confirmation_report_v1"
    failure_reasons: tuple[str, ...] = ()
    confirmation_evidence: CandidateConfirmationEvidence | None = None
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        _validate_confirmation_report_identity(self)
        _validate_confirmation_report_outcome(self)
        reasons = tuple(sorted(set(self.failure_reasons)))
        if self.status == "historical_candidate_ready" and reasons:
            raise ValueError("ready confirmation cannot contain inherited failure reasons")
        if not self.evidence and self.status != "historical_candidate_ready" and not reasons:
            raise ValueError("inherited confirmation terminal status requires a failure reason")
        if self.production_authority or self.terminal_holdout_status != "terminal_holdout_not_opened":
            raise ValueError("confirmation report cannot open a terminal holdout or authorize production")
        object.__setattr__(self, "failure_reasons", reasons)
        object.__setattr__(self, "content_hash", _hash(self))


def _validate_confirmation_report_identity(report: HistoricalCandidateConfirmationReport) -> None:
    if not report.strategy or not _SHA256.fullmatch(report.family_hash) or not report.confirmation_dates:
        raise ValueError("confirmation report identity is invalid")
    if tuple(sorted(set(report.confirmation_dates))) != report.confirmation_dates:
        raise ValueError("confirmation dates must be strictly ordered")
    if report.evidence and tuple(item.candidate_id for item in report.evidence) != report.holm_family:
        raise ValueError("confirmation evidence must retain the preregistered Holm family")


def _validate_confirmation_report_outcome(report: HistoricalCandidateConfirmationReport) -> None:
    if report.status == "historical_candidate_ready" and report.selected_candidate_id is None:
        raise ValueError("ready confirmation requires one selected candidate")
    if report.status != "historical_candidate_ready" and report.selected_candidate_id is not None:
        raise ValueError("rejected confirmation cannot select a candidate")
    if (
        report.confirmation_evidence is not None
        and report.confirmation_evidence.candidate_id not in report.holm_family[1:]
    ):
        raise ValueError("confirmation evidence must belong to a preregistered challenger")
    if report.status == "historical_candidate_ready" and (
        report.confirmation_evidence is None
        or not report.confirmation_evidence.passed
        or report.confirmation_evidence.candidate_id != report.selected_candidate_id
    ):
        raise ValueError("ready confirmation must bind passing confirmation evidence")


@dataclass(frozen=True)
class CandidateConfirmationPlan:
    selected_candidate_id: str
    alpha: float = 0.05
    master_seed: int = 20260901
    repetitions: int = 10_000
    block_days: int | None = None

    def __post_init__(self) -> None:
        if not self.selected_candidate_id:
            raise ValueError("confirmation plan requires a selected candidate")
        if not math.isfinite(self.alpha) or not 0.0 < self.alpha < 1.0:
            raise ValueError("confirmation plan alpha must be between zero and one")
        if self.repetitions < 1:
            raise ValueError("confirmation plan repetitions must be positive")
        if self.block_days is not None and self.block_days < 5:
            raise ValueError("confirmation plan block length must be at least five sessions")


@dataclass(frozen=True)
class _ConfirmationSegment:
    family: TransparentCandidateFamily
    plan: CandidateConfirmationPlan
    supplied: dict[str, CandidateConfirmationSeries]
    ids: tuple[str, ...]
    challenger_ids: tuple[str, ...]
    registered_challengers: tuple[str, ...]
    holm_family: tuple[str, ...]
    dates: tuple[date, ...]
    block_days: int


@dataclass(frozen=True)
class _ConfirmationStatistics:
    bootstrap_20: PreregisteredBootstrapResult
    bootstrap_50: PreregisteredBootstrapResult
    absolute_20: PreregisteredBootstrapResult | None
    absolute_50: PreregisteredBootstrapResult | None


def _evaluate_candidate_family_segment(
    family: TransparentCandidateFamily,
    series: tuple[CandidateConfirmationSeries, ...],
    plan: CandidateConfirmationPlan,
    additional_series: tuple[CandidateConfirmationSeries, ...] = (),
) -> HistoricalCandidateConfirmationReport:
    """Evaluate the fixed confirmation segment once with one joint Holm family."""

    segment = _prepare_confirmation_segment(family, series, additional_series, plan)
    if len(segment.dates) < 5:
        return _insufficient(family, segment.dates, segment.holm_family)
    plans = {candidate_id: _bootstrap(segment, candidate_id, "20bp") for candidate_id in segment.challenger_ids}
    p_values = {candidate_id: bootstrap.p_value for candidate_id, bootstrap in plans.items()}
    p_values[segment.ids[0]] = None
    holm = fixed_family_holm(p_values, family=segment.holm_family, alpha=plan.alpha)
    holm_by_id = {item.challenger_id: item for item in holm}
    evidence = tuple(
        _candidate_confirmation_evidence(segment, candidate_id, plans[candidate_id], holm_by_id[candidate_id])
        for candidate_id in segment.challenger_ids
    )
    eligible_evidence = tuple(
        item
        for item in evidence
        if item.passed
        and item.candidate_id in segment.registered_challengers
        and item.candidate_id == plan.selected_candidate_id
    )
    selected = max(
        eligible_evidence,
        key=lambda item: (item.mean_increment_20bp, item.mean_increment_50bp, item.candidate_id),
        default=None,
    )
    status: ConfirmationStatus = "historical_candidate_ready" if selected else "historical_rejected"
    control_evidence = _control_confirmation_evidence(segment, holm_by_id[segment.ids[0]])
    return HistoricalCandidateConfirmationReport(
        family.strategy,
        family.content_hash,
        segment.dates,
        segment.holm_family,
        (control_evidence, *evidence),
        selected.candidate_id if selected else None,
        status,
        confirmation_evidence=selected,
    )


def _prepare_confirmation_segment(
    family: TransparentCandidateFamily,
    series: tuple[CandidateConfirmationSeries, ...],
    additional_series: tuple[CandidateConfirmationSeries, ...],
    plan: CandidateConfirmationPlan,
) -> _ConfirmationSegment:
    if not series:
        raise ValueError("confirmation requires candidate series")
    block_days = plan.block_days if plan.block_days is not None else (10 if family.strategy == "d25" else 5)
    if block_days < 5 or block_days < (10 if family.strategy == "d25" else 2):
        raise ValueError("confirmation block length does not cover the registered label horizon")
    ids = tuple(candidate.candidate_id for candidate in family.candidates)
    all_series = (*series, *additional_series)
    supplied = {item.candidate_id: item for item in all_series}
    if len(supplied) != len(all_series) or set(supplied).intersection(ids) != set(ids):
        raise ValueError("confirmation must include control and every preregistered candidate")
    dates = supplied[ids[0]].trade_dates
    if any(item.trade_dates != dates for item in all_series):
        raise ValueError("confirmation candidates must share identical dates")
    registered_challengers = ids[1:]
    if plan.selected_candidate_id not in registered_challengers:
        raise ValueError("confirmation selected candidate must belong to the sealed transparent family")
    challenger_ids = tuple(item for item in supplied if item != ids[0])
    return _ConfirmationSegment(
        family,
        plan,
        supplied,
        ids,
        challenger_ids,
        registered_challengers,
        (ids[0], *challenger_ids),
        dates,
        block_days,
    )


def _bootstrap(
    segment: _ConfirmationSegment,
    candidate_id: str,
    metric: Literal["20bp", "50bp", "absolute_20bp", "absolute_50bp"],
) -> PreregisteredBootstrapResult:
    item = segment.supplied[candidate_id]
    values = {
        "20bp": item.paired_increment_20bp,
        "50bp": item.paired_increment_50bp,
        "absolute_20bp": item.candidate_net_excess_20bp,
        "absolute_50bp": item.candidate_net_excess_50bp,
    }[metric]
    prefix = "historical_candidate_confirmation_"
    return paired_moving_block_statistics(
        values,
        plan=PreregisteredBootstrapPlan(
            f"{prefix}{metric}_v1",
            segment.plan.master_seed,
            candidate_id,
            segment.block_days,
            segment.plan.repetitions,
        ),
    )


def _candidate_confirmation_evidence(
    segment: _ConfirmationSegment,
    candidate_id: str,
    bootstrap_20: PreregisteredBootstrapResult,
    holm: PreregisteredHolmDecision,
) -> CandidateConfirmationEvidence:
    item = segment.supplied[candidate_id]
    statistics = _ConfirmationStatistics(
        bootstrap_20,
        _bootstrap(segment, candidate_id, "50bp"),
        _bootstrap(segment, candidate_id, "absolute_20bp") if item.candidate_net_excess_20bp else None,
        _bootstrap(segment, candidate_id, "absolute_50bp") if item.candidate_net_excess_50bp else None,
    )
    failures = _confirmation_failures(item, holm, statistics)
    return CandidateConfirmationEvidence(
        candidate_id,
        statistics.bootstrap_20,
        statistics.bootstrap_50,
        _mean(item.paired_increment_20bp),
        _mean(item.paired_increment_50bp),
        _mean(item.severe_loss_rate_delta),
        _mean(item.turnover_delta) if item.turnover_delta else 0.0,
        _mean(item.capacity_delta) if item.capacity_delta else 0.0,
        _mean(item.concentration_delta) if item.concentration_delta else 0.0,
        holm,
        not failures,
        failures,
        mean_absolute_20bp=_optional_mean(item.candidate_net_excess_20bp),
        mean_absolute_50bp=_optional_mean(item.candidate_net_excess_50bp),
        absolute_bootstrap_20bp=statistics.absolute_20,
        absolute_bootstrap_50bp=statistics.absolute_50,
        development_fold_directions=item.development_fold_directions,
    )


def _confirmation_failures(
    item: CandidateConfirmationSeries,
    holm: PreregisteredHolmDecision,
    statistics: _ConfirmationStatistics,
) -> tuple[str, ...]:
    failures = [
        *_confirmation_return_failures(item, holm, statistics.bootstrap_20, statistics.bootstrap_50),
        *_confirmation_absolute_failures(item, statistics.absolute_20, statistics.absolute_50),
        *_confirmation_risk_failures(item),
    ]
    return tuple(sorted(set(failures)))


def _confirmation_return_failures(
    item: CandidateConfirmationSeries,
    holm: PreregisteredHolmDecision,
    bootstrap_20: PreregisteredBootstrapResult,
    bootstrap_50: PreregisteredBootstrapResult,
) -> tuple[str, ...]:
    failures: list[str] = []
    if not holm.rejected_null:
        failures.append("holm_not_significant")
    if bootstrap_20.confidence_lower is None or bootstrap_20.confidence_lower <= 0.0:
        failures.append("bootstrap_20bp_lower_not_positive")
    if bootstrap_50.confidence_lower is None or bootstrap_50.confidence_lower <= 0.0:
        failures.append("bootstrap_50bp_lower_not_positive")
    if _mean(item.paired_increment_20bp) <= 0.0:
        failures.append("paired_20bp_not_positive")
    if _mean(item.paired_increment_50bp) <= 0.0:
        failures.append("paired_50bp_not_positive")
    return tuple(failures)


def _confirmation_absolute_failures(
    item: CandidateConfirmationSeries,
    absolute_20: PreregisteredBootstrapResult | None,
    absolute_50: PreregisteredBootstrapResult | None,
) -> tuple[str, ...]:
    failures: list[str] = []
    for label, values, bootstrap in (
        ("20bp", item.candidate_net_excess_20bp, absolute_20),
        ("50bp", item.candidate_net_excess_50bp, absolute_50),
    ):
        if not values:
            failures.append(f"absolute_{label}_evidence_missing")
        elif _mean(values) <= 0.0:
            failures.append(f"absolute_{label}_not_positive")
        if bootstrap is None or bootstrap.confidence_lower is None or bootstrap.confidence_lower <= 0.0:
            failures.append(f"absolute_bootstrap_{label}_lower_not_positive")
    return tuple(failures)


def _confirmation_risk_failures(item: CandidateConfirmationSeries) -> tuple[str, ...]:
    failures: list[str] = []
    if len(item.development_fold_directions) != 5:
        failures.append("development_fold_directions_missing")
    elif any(value <= 0 for value in item.development_fold_directions):
        failures.append("development_fold_direction_inconsistent")
    for failed, reason in (
        (_mean(item.severe_loss_rate_delta) > 0.0, "severe_loss_rate_worse_than_control"),
        (bool(item.turnover_delta) and _mean(item.turnover_delta) > 0.0, "turnover_worse_than_control"),
        (bool(item.capacity_delta) and _mean(item.capacity_delta) < 0.0, "capacity_worse_than_control"),
        (bool(item.concentration_delta) and _mean(item.concentration_delta) > 0.0, "concentration_worse_than_control"),
    ):
        if failed:
            failures.append(reason)
    return tuple(failures)


def _control_confirmation_evidence(
    segment: _ConfirmationSegment,
    holm: PreregisteredHolmDecision,
) -> CandidateConfirmationEvidence:
    control_id = segment.ids[0]
    return CandidateConfirmationEvidence(
        control_id,
        _bootstrap(segment, control_id, "20bp"),
        _bootstrap(segment, control_id, "50bp"),
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        holm,
        False,
        ("control",),
    )


def _optional_mean(values: tuple[float, ...], default: float | None = None) -> float | None:
    return _mean(values) if values else default


def confirm_transparent_candidates(
    family: TransparentCandidateFamily,
    development_series: tuple[CandidateConfirmationSeries, ...],
    confirmation_series: tuple[CandidateConfirmationSeries, ...],
    plan: CandidateConfirmationPlan,
    additional_series: tuple[CandidateConfirmationSeries, ...] = (),
) -> HistoricalCandidateConfirmationReport:
    """Run family-wide development control before one sealed confirmation replay."""

    development = _evaluate_candidate_family_segment(
        family,
        development_series,
        plan,
        additional_series=additional_series,
    )
    confirmation_dates = confirmation_series[0].trade_dates if confirmation_series else development.confirmation_dates
    if development.status != "historical_candidate_ready":
        return HistoricalCandidateConfirmationReport(
            strategy=family.strategy,
            family_hash=family.content_hash,
            confirmation_dates=confirmation_dates,
            holm_family=development.holm_family,
            evidence=development.evidence,
            selected_candidate_id=None,
            status=development.status,
            failure_reasons=development.failure_reasons,
        )
    candidates = {candidate.candidate_id: candidate for candidate in family.candidates}
    control_id = family.candidates[0].candidate_id
    confirmation_by_id = {item.candidate_id: item for item in confirmation_series}
    expected_ids = {control_id, plan.selected_candidate_id}
    if len(confirmation_series) != 2 or set(confirmation_by_id) != expected_ids:
        raise ValueError("confirmation segment must contain only control and the development-selected candidate")
    if any(item.trade_dates != confirmation_dates for item in confirmation_series):
        raise ValueError("confirmation control and selected candidate must share identical dates")
    if confirmation_dates[0] <= development_series[0].trade_dates[-1]:
        raise ValueError("confirmation dates must strictly follow development dates")
    confirmation_family = TransparentCandidateFamily(
        strategy=family.strategy,
        candidates=(candidates[control_id], candidates[plan.selected_candidate_id]),
        source_ablation_hash=family.source_ablation_hash,
        development_dates=family.development_dates,
    )
    confirmation = _evaluate_candidate_family_segment(
        confirmation_family,
        confirmation_series,
        plan,
    )
    selected_development = next(
        item for item in development.evidence if item.candidate_id == plan.selected_candidate_id
    )
    selected_confirmation = next(
        (item for item in confirmation.evidence if item.candidate_id == plan.selected_candidate_id),
        None,
    )
    if selected_confirmation is not None:
        failures = tuple(
            reason
            for reason in selected_confirmation.failure_reasons
            if reason
            not in {
                "holm_not_significant",
                "development_fold_directions_missing",
                "development_fold_direction_inconsistent",
            }
        )
        selected_confirmation = dataclasses.replace(
            selected_confirmation,
            holm=selected_development.holm,
            passed=not failures,
            failure_reasons=failures,
            development_fold_directions=selected_development.development_fold_directions,
        )
    status: ConfirmationStatus = (
        "historical_candidate_ready"
        if selected_confirmation is not None and selected_confirmation.passed
        else confirmation.status
    )
    if status == "historical_candidate_ready" and selected_confirmation is None:
        raise AssertionError("ready confirmation requires selected evidence")
    return HistoricalCandidateConfirmationReport(
        strategy=family.strategy,
        family_hash=family.content_hash,
        confirmation_dates=confirmation_dates,
        holm_family=development.holm_family,
        evidence=development.evidence,
        selected_candidate_id=plan.selected_candidate_id if status == "historical_candidate_ready" else None,
        status=status,
        failure_reasons=confirmation.failure_reasons,
        confirmation_evidence=selected_confirmation,
    )


def build_confirmation_folds(confirmation_dates: tuple[date, ...]) -> tuple[tuple[date, ...], ...]:
    """Return five contiguous, date-ordered folds for diagnostics only."""

    if len(confirmation_dates) < 5 or tuple(sorted(set(confirmation_dates))) != confirmation_dates:
        raise ValueError("confirmation fold dates must be ordered and contain at least five sessions")
    quotient, remainder = divmod(len(confirmation_dates), 5)
    folds: list[tuple[date, ...]] = []
    offset = 0
    for index in range(5):
        size = quotient + (1 if index < remainder else 0)
        folds.append(confirmation_dates[offset : offset + size])
        offset += size
    return tuple(folds)


def _insufficient(
    family: TransparentCandidateFamily, dates: tuple[date, ...], ids: tuple[str, ...]
) -> HistoricalCandidateConfirmationReport:
    return HistoricalCandidateConfirmationReport(
        family.strategy,
        family.content_hash,
        dates,
        ids,
        (),
        None,
        "historical_data_insufficient",
        failure_reasons=("confirmation_dates_below_block_length",),
    )


def inherit_candidate_confirmation(
    family: TransparentCandidateFamily,
    *,
    confirmation_dates: tuple[date, ...],
    status: Literal["historical_rejected", "historical_data_insufficient"],
    failure_reasons: tuple[str, ...],
) -> HistoricalCandidateConfirmationReport:
    if not failure_reasons:
        raise ValueError("inherited candidate confirmation requires parent failure reasons")
    return HistoricalCandidateConfirmationReport(
        strategy=family.strategy,
        family_hash=family.content_hash,
        confirmation_dates=confirmation_dates,
        holm_family=tuple(candidate.candidate_id for candidate in family.candidates),
        evidence=(),
        selected_candidate_id=None,
        status=status,
        failure_reasons=failure_reasons,
    )


def _mean(values: tuple[float, ...]) -> float:
    return math.fsum(values) / len(values)


def _hash(value: object) -> str:
    from trader.domain.research.transparent_candidate import _hash as candidate_hash

    return candidate_hash(value)


__all__ = [
    "CandidateConfirmationPlan",
    "CandidateConfirmationEvidence",
    "CandidateConfirmationSeries",
    "HistoricalCandidateConfirmationReport",
    "build_confirmation_folds",
    "confirm_transparent_candidates",
    "inherit_candidate_confirmation",
]
