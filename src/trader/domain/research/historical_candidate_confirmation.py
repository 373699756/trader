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

    def __post_init__(self) -> None:
        n = len(self.trade_dates)
        if not self.candidate_id or n < 1 or tuple(sorted(set(self.trade_dates))) != self.trade_dates:
            raise ValueError("confirmation series dates or identity are invalid")
        vectors = (self.paired_increment_20bp, self.paired_increment_50bp, self.severe_loss_rate_delta)
        if any(len(vector) != n for vector in vectors):
            raise ValueError("confirmation series vectors must align by date")
        for vector in (*vectors, self.turnover_delta, self.capacity_delta, self.concentration_delta):
            if vector and len(vector) != n:
                raise ValueError("confirmation diagnostic vectors must align by date")
            if any(not math.isfinite(value) for value in vector):
                raise ValueError("confirmation series values must be finite")


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

    def __post_init__(self) -> None:
        if not self.candidate_id or any(not math.isfinite(value) for value in (self.mean_increment_20bp, self.mean_increment_50bp, self.severe_loss_rate_delta, self.turnover_delta, self.capacity_delta, self.concentration_delta)):
            raise ValueError("confirmation evidence is invalid")
        if self.passed == bool(self.failure_reasons):
            raise ValueError("confirmation evidence status and failures disagree")


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
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        if not self.strategy or not _SHA256.fullmatch(self.family_hash) or not self.confirmation_dates:
            raise ValueError("confirmation report identity is invalid")
        if tuple(sorted(set(self.confirmation_dates))) != self.confirmation_dates:
            raise ValueError("confirmation dates must be strictly ordered")
        if self.status != "historical_data_insufficient" and tuple(item.candidate_id for item in self.evidence) != self.holm_family:
            raise ValueError("confirmation evidence must retain the preregistered Holm family")
        if self.status == "historical_candidate_ready" and self.selected_candidate_id is None:
            raise ValueError("ready confirmation requires one selected candidate")
        if self.status != "historical_candidate_ready" and self.selected_candidate_id is not None:
            raise ValueError("rejected confirmation cannot select a candidate")
        if self.production_authority or self.terminal_holdout_status != "terminal_holdout_not_opened":
            raise ValueError("confirmation report cannot open a terminal holdout or authorize production")
        object.__setattr__(self, "content_hash", _hash(self))


def confirm_transparent_candidates(
    family: TransparentCandidateFamily,
    series: tuple[CandidateConfirmationSeries, ...],
    *,
    additional_series: tuple[CandidateConfirmationSeries, ...] = (),
    alpha: float = 0.05,
    master_seed: int = 20260901,
    repetitions: int = 10_000,
    block_days: int | None = None,
) -> HistoricalCandidateConfirmationReport:
    """Evaluate the fixed confirmation segment once with one joint Holm family."""

    if not series:
        raise ValueError("confirmation requires candidate series")
    selected_block_days = block_days if block_days is not None else (10 if family.strategy == "d25" else 5)
    if selected_block_days < 5 or selected_block_days < (10 if family.strategy == "d25" else 2):
        raise ValueError("confirmation block length does not cover the registered label horizon")
    ids = tuple(candidate.candidate_id for candidate in family.candidates)
    all_series = (*series, *additional_series)
    supplied = {item.candidate_id: item for item in all_series}
    if len(supplied) != len(all_series) or set(supplied).intersection(ids) != set(ids):
        raise ValueError("confirmation must include control and every preregistered candidate")
    dates = supplied[ids[0]].trade_dates
    if any(item.trade_dates != dates for item in all_series):
        raise ValueError("confirmation candidates must share identical dates")
    challenger_ids = tuple(item for item in supplied if item != ids[0])
    holm_family = (ids[0], *challenger_ids)
    if len(dates) < 5:
        return _insufficient(family, dates, holm_family)
    plans: dict[str, PreregisteredBootstrapResult] = {}
    evidence: list[CandidateConfirmationEvidence] = []
    p_values: dict[str, float | None] = {}
    for candidate_id in challenger_ids:
        item = supplied[candidate_id]
        bootstrap_20 = paired_moving_block_statistics(item.paired_increment_20bp, plan=PreregisteredBootstrapPlan("historical_candidate_confirmation_20bp_v1", master_seed, candidate_id, selected_block_days, repetitions))
        bootstrap_50 = paired_moving_block_statistics(item.paired_increment_50bp, plan=PreregisteredBootstrapPlan("historical_candidate_confirmation_50bp_v1", master_seed, candidate_id, selected_block_days, repetitions))
        plans[candidate_id] = bootstrap_20
        p_values[candidate_id] = bootstrap_20.p_value
    # Control is retained in the family and receives a deterministic null entry.
    p_values[ids[0]] = None
    holm = fixed_family_holm(p_values, family=holm_family, alpha=alpha)
    holm_by_id = {item.challenger_id: item for item in holm}
    control = supplied[ids[0]]
    for candidate_id in ids[1:]:
        item = supplied[candidate_id]
        b20 = plans[candidate_id]
        b50 = paired_moving_block_statistics(item.paired_increment_50bp, plan=PreregisteredBootstrapPlan("historical_candidate_confirmation_50bp_v1", master_seed, candidate_id, selected_block_days, repetitions))
        failures: list[str] = []
        if not holm_by_id[candidate_id].rejected_null:
            failures.append("holm_not_significant")
        if b20.confidence_lower is None or b20.confidence_lower <= 0.0:
            failures.append("bootstrap_20bp_lower_not_positive")
        if b50.confidence_lower is None or b50.confidence_lower <= 0.0:
            failures.append("bootstrap_50bp_lower_not_positive")
        if _mean(item.paired_increment_20bp) <= 0.0:
            failures.append("paired_20bp_not_positive")
        if _mean(item.paired_increment_50bp) <= 0.0:
            failures.append("paired_50bp_not_positive")
        if _mean(item.severe_loss_rate_delta) > 0.0:
            failures.append("severe_loss_rate_worse_than_control")
        if item.turnover_delta and _mean(item.turnover_delta) > 0.0:
            failures.append("turnover_worse_than_control")
        if item.capacity_delta and _mean(item.capacity_delta) < 0.0:
            failures.append("capacity_worse_than_control")
        if item.concentration_delta and _mean(item.concentration_delta) > 0.0:
            failures.append("concentration_worse_than_control")
        failures = sorted(set(failures))
        evidence.append(CandidateConfirmationEvidence(candidate_id, b20, b50, _mean(item.paired_increment_20bp), _mean(item.paired_increment_50bp), _mean(item.severe_loss_rate_delta), _mean(item.turnover_delta) if item.turnover_delta else 0.0, _mean(item.capacity_delta) if item.capacity_delta else 0.0, _mean(item.concentration_delta) if item.concentration_delta else 0.0, holm_by_id[candidate_id], not failures, tuple(failures)))
    selected = max((item for item in evidence if item.passed), key=lambda item: (item.mean_increment_20bp, item.mean_increment_50bp, item.candidate_id), default=None)
    status: ConfirmationStatus = "historical_candidate_ready" if selected else "historical_rejected"
    control_evidence = CandidateConfirmationEvidence(ids[0], paired_moving_block_statistics(control.paired_increment_20bp, plan=PreregisteredBootstrapPlan("historical_candidate_confirmation_20bp_v1", master_seed, ids[0], selected_block_days, repetitions)), paired_moving_block_statistics(control.paired_increment_50bp, plan=PreregisteredBootstrapPlan("historical_candidate_confirmation_50bp_v1", master_seed, ids[0], selected_block_days, repetitions)), 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, holm_by_id[ids[0]], False, ("control",))
    return HistoricalCandidateConfirmationReport(family.strategy, family.content_hash, dates, holm_family, (control_evidence, *evidence), selected.candidate_id if selected else None, status)


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


def _insufficient(family: TransparentCandidateFamily, dates: tuple[date, ...], ids: tuple[str, ...]) -> HistoricalCandidateConfirmationReport:
    return HistoricalCandidateConfirmationReport(family.strategy, family.content_hash, dates, ids, (), None, "historical_data_insufficient")


def _mean(values: tuple[float, ...]) -> float:
    return math.fsum(values) / len(values)


def _hash(value: object) -> str:
    from trader.domain.research.transparent_candidate import _hash as candidate_hash
    return candidate_hash(value)


__all__ = ["CandidateConfirmationSeries", "CandidateConfirmationEvidence", "HistoricalCandidateConfirmationReport", "build_confirmation_folds", "confirm_transparent_candidates"]
