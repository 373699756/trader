"""Paired diagnostics on four company replays and their common matured outcomes."""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from datetime import date
from typing import Literal

from trader.training.domain.evaluation.selection_experiment import SELECTION_COST_BPS

CompanyReplayArm = Literal["baseline", "risk", "financial", "combined"]
CompanyReplaySeats = Literal["fixed", "executable"]
CompanyReplayCosts = tuple[float, float, float]
CompanyReplayComparison = Literal["risk_minus_baseline", "financial_minus_baseline", "combined_minus_baseline", "interaction"]
CompanyReplayRiskComparison = Literal["risk_minus_baseline", "combined_minus_financial"]

COMPANY_REPLAY_ARMS: tuple[CompanyReplayArm, ...] = ("baseline", "risk", "financial", "combined")
_SEAT_GROUPS: tuple[CompanyReplaySeats, ...] = ("fixed", "executable")
_SEATS = 6


@dataclass(frozen=True)
class CompanyReplayRejection:
    code: str
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.reasons or tuple(sorted(set(self.reasons))) != self.reasons:
            raise ValueError("company replay rejection requires unique ordered reasons")


@dataclass(frozen=True)
class CompanyReplaySelection:
    arm: CompanyReplayArm
    cost_reference: str | None
    fixed: tuple[str, ...]
    executable: tuple[str, ...]
    rejections: tuple[CompanyReplayRejection, ...] = ()

    def __post_init__(self) -> None:
        if (
            self.arm not in COMPANY_REPLAY_ARMS
            or self.cost_reference == ""
            or len(self.fixed) > _SEATS
            or len(set(self.fixed)) != len(self.fixed)
            or len(set(self.executable)) != len(self.executable)
            or not set(self.executable).issubset(self.fixed)
            or any(len(code) != 6 or not code.isascii() or not code.isdigit() for code in self.fixed)
        ):
            raise ValueError("company replay must retain ordered fixed seats and their executable subset")
        if self.executable != tuple(code for code in self.fixed if code in self.executable):
            raise ValueError("company replay executable order must follow fixed seats")
        rejected = tuple(item.code for item in self.rejections)
        if set(rejected).intersection(self.fixed) or len(set(rejected)) != len(rejected):
            raise ValueError("company replay hard rejection cannot share a fixed seat")

    def seats(self, seat_group: CompanyReplaySeats) -> tuple[str, ...]:
        return self.fixed if seat_group == "fixed" else self.executable


@dataclass(frozen=True)
class CompanyReplayOutcome:
    code: str
    hit: bool | None
    entry_tradable: bool | None
    exit_untradable: bool | None
    severe_loss: bool | None
    pre_cost_excess_returns: tuple[float, ...] | None

    def __post_init__(self) -> None:
        if len(self.code) != 6 or not self.code.isascii() or not self.code.isdigit():
            raise ValueError("company replay outcome code is invalid")
        if any(
            value is not None and type(value) is not bool
            for value in (self.hit, self.entry_tradable, self.exit_untradable, self.severe_loss)
        ):
            raise ValueError("company replay outcome flags require explicit evidence")
        if self.pre_cost_excess_returns is not None and (
            len(self.pre_cost_excess_returns) not in {1, 4}
            or any(not math.isfinite(value) for value in self.pre_cost_excess_returns)
        ):
            raise ValueError("company replay outcome horizons are invalid")

    @property
    def pre_cost_return(self) -> float | None:
        values = self.pre_cost_excess_returns
        return None if values is None else math.fsum(values) / len(values)


@dataclass(frozen=True)
class CompanyReplayDay:
    trade_date: date
    selections: tuple[CompanyReplaySelection, ...]
    outcomes: tuple[CompanyReplayOutcome, ...]

    def __post_init__(self) -> None:
        if tuple(item.arm for item in self.selections) != COMPANY_REPLAY_ARMS:
            raise ValueError("company replay requires the same four arms on every trading day")
        selected = {code for item in self.selections for code in item.fixed}
        if any(rejection.code not in selected for selection in self.selections for rejection in selection.rejections):
            raise ValueError("company replay rejections must reference a fixed seat from another arm")
        outcome_codes = {item.code for item in self.outcomes}
        if len(outcome_codes) != len(self.outcomes) or not outcome_codes.issubset(selected):
            raise ValueError("company replay outcomes must uniquely reference the union of fixed seats")


@dataclass(frozen=True)
class CompanyReplaySeatMetrics:
    arm: CompanyReplayArm
    seat_group: CompanyReplaySeats
    seats: int
    empty_days: int
    known_targets: int
    hits: int
    known_risk_outcomes: int
    severe_losses: int
    known_entries: int
    untradable_entries: int
    known_exits: int
    untradable_exits: int
    known_return_days: int
    mean_six_slot_net_excess_returns: CompanyReplayCosts | None


@dataclass(frozen=True)
class CompanyReplayEffect:
    comparison: CompanyReplayComparison
    seat_group: CompanyReplaySeats
    paired_days: int
    mean_six_slot_net_excess_delta: CompanyReplayCosts | None


@dataclass(frozen=True)
class CompanyReplayRiskEffect:
    comparison: CompanyReplayRiskComparison
    newly_rejected_seats: int
    known_profit_outcomes: int
    profitable_at_20bp: int
    known_risk_outcomes: int
    severe_losses: int
    reason_counts: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class CompanyReplayMetrics:
    trade_dates: tuple[date, ...]
    seats: tuple[CompanyReplaySeatMetrics, ...]
    effects: tuple[CompanyReplayEffect, ...]
    risk_effects: tuple[CompanyReplayRiskEffect, ...]


@dataclass(frozen=True)
class _SeatDay:
    selected: int
    outcomes: tuple[CompanyReplayOutcome, ...]
    net_excess: CompanyReplayCosts | None


def EvaluateCompanyReplay(days: tuple[CompanyReplayDay, ...]) -> CompanyReplayMetrics:
    dates = tuple(day.trade_date for day in days)
    if not dates or dates != tuple(sorted(set(dates))):
        raise ValueError("company replay dates must be nonempty, sorted and unique")

    metrics: list[CompanyReplaySeatMetrics] = []
    effects: list[CompanyReplayEffect] = []
    outcomes_by_day = tuple({outcome.code: outcome for outcome in day.outcomes} for day in days)
    for seat_group in _SEAT_GROUPS:
        nets: list[tuple[CompanyReplayCosts | None, ...]] = []
        for index, arm in enumerate(COMPANY_REPLAY_ARMS):
            seat_days = tuple(
                _seat_day(day.selections[index].seats(seat_group), outcomes)
                for day, outcomes in zip(days, outcomes_by_day, strict=True)
            )
            metrics.append(_seat_metrics(arm, seat_group, seat_days))
            nets.append(tuple(item.net_excess for item in seat_days))
        effects.extend(_paired_effects(seat_group, nets))
    return CompanyReplayMetrics(
        dates,
        tuple(metrics),
        tuple(effects),
        (
            _risk_effect("risk_minus_baseline", days, outcomes_by_day, previous=0, candidate=1),
            _risk_effect("combined_minus_financial", days, outcomes_by_day, previous=2, candidate=3),
        ),
    )


def _seat_day(codes: tuple[str, ...], outcomes: dict[str, CompanyReplayOutcome]) -> _SeatDay:
    known = tuple(outcomes[code] for code in codes if code in outcomes)
    if len(known) != len(codes):
        return _SeatDay(len(codes), known, None)
    pre_cost_returns: list[float] = []
    for outcome in known:
        if outcome.entry_tradable is False:
            continue
        pre_cost = outcome.pre_cost_return
        if outcome.entry_tradable is None or pre_cost is None or outcome.exit_untradable is None:
            return _SeatDay(len(codes), known, None)
        # The established exit valuation remains in the outcome even on an untradable exit.
        pre_cost_returns.append(pre_cost)
    return _SeatDay(len(codes), known, _six_slot_returns(pre_cost_returns))


def _six_slot_returns(pre_cost_returns: list[float]) -> CompanyReplayCosts:
    gross = math.fsum(pre_cost_returns)
    count = len(pre_cost_returns)
    return (
        (gross - count * SELECTION_COST_BPS[0] / 10_000) / _SEATS,
        (gross - count * SELECTION_COST_BPS[1] / 10_000) / _SEATS,
        (gross - count * SELECTION_COST_BPS[2] / 10_000) / _SEATS,
    )


def _seat_metrics(
    arm: CompanyReplayArm, seat_group: CompanyReplaySeats, days: tuple[_SeatDay, ...]
) -> CompanyReplaySeatMetrics:
    outcomes = tuple(outcome for day in days for outcome in day.outcomes)
    returns = tuple(day.net_excess for day in days if day.net_excess is not None)
    return CompanyReplaySeatMetrics(
        arm=arm,
        seat_group=seat_group,
        seats=sum(day.selected for day in days),
        empty_days=sum(day.selected == 0 for day in days),
        known_targets=sum(outcome.hit is not None for outcome in outcomes),
        hits=sum(outcome.hit is True for outcome in outcomes),
        known_risk_outcomes=sum(outcome.severe_loss is not None for outcome in outcomes),
        severe_losses=sum(outcome.severe_loss is True for outcome in outcomes),
        known_entries=sum(outcome.entry_tradable is not None for outcome in outcomes),
        untradable_entries=sum(outcome.entry_tradable is False for outcome in outcomes),
        known_exits=sum(outcome.exit_untradable is not None for outcome in outcomes),
        untradable_exits=sum(outcome.exit_untradable is True for outcome in outcomes),
        known_return_days=len(returns),
        mean_six_slot_net_excess_returns=_mean_costs(returns),
    )


def _paired_effects(
    seat_group: CompanyReplaySeats, nets: list[tuple[CompanyReplayCosts | None, ...]]
) -> tuple[CompanyReplayEffect, ...]:
    comparisons: tuple[tuple[CompanyReplayComparison, tuple[float, ...]], ...] = (
        ("risk_minus_baseline", (-1.0, 1.0, 0.0, 0.0)),
        ("financial_minus_baseline", (-1.0, 0.0, 1.0, 0.0)),
        ("combined_minus_baseline", (-1.0, 0.0, 0.0, 1.0)),
        ("interaction", (1.0, -1.0, -1.0, 1.0)),
    )
    result: list[CompanyReplayEffect] = []
    for comparison, weights in comparisons:
        selected = tuple((weight, net) for weight, net in zip(weights, nets, strict=True) if weight)
        day_deltas: list[CompanyReplayCosts] = []
        for terms in zip(*(net for _, net in selected), strict=True):
            present = tuple(term for term in terms if term is not None)
            if len(present) != len(selected):
                continue
            paired = tuple(zip((weight for weight, _ in selected), present, strict=True))
            day_deltas.append(
                (
                    math.fsum(weight * value[0] for weight, value in paired),
                    math.fsum(weight * value[1] for weight, value in paired),
                    math.fsum(weight * value[2] for weight, value in paired),
                )
            )
        result.append(CompanyReplayEffect(comparison, seat_group, len(day_deltas), _mean_costs(tuple(day_deltas))))
    return tuple(result)


def _mean_costs(values: tuple[CompanyReplayCosts, ...]) -> CompanyReplayCosts | None:
    if not values:
        return None
    return (
        math.fsum(value[0] for value in values) / len(values),
        math.fsum(value[1] for value in values) / len(values),
        math.fsum(value[2] for value in values) / len(values),
    )


def _risk_effect(
    comparison: CompanyReplayRiskComparison,
    days: tuple[CompanyReplayDay, ...],
    outcomes_by_day: tuple[dict[str, CompanyReplayOutcome], ...],
    *,
    previous: int,
    candidate: int,
) -> CompanyReplayRiskEffect:
    rejected = 0
    observed: list[CompanyReplayOutcome] = []
    reasons: Counter[str] = Counter()
    for day, outcomes in zip(days, outcomes_by_day, strict=True):
        previous_fixed = frozenset(day.selections[previous].fixed)
        for rejection in day.selections[candidate].rejections:
            if rejection.code in previous_fixed:
                rejected += 1
                reasons.update(rejection.reasons)
                if rejection.code in outcomes:
                    observed.append(outcomes[rejection.code])
    comparable_returns = tuple(
        pre_cost
        for outcome in observed
        if outcome.entry_tradable is True
        and outcome.exit_untradable is False
        and (pre_cost := outcome.pre_cost_return) is not None
    )
    return CompanyReplayRiskEffect(
        comparison=comparison,
        newly_rejected_seats=rejected,
        known_profit_outcomes=len(comparable_returns),
        profitable_at_20bp=sum(value > SELECTION_COST_BPS[0] / 10_000 for value in comparable_returns),
        known_risk_outcomes=sum(outcome.severe_loss is not None for outcome in observed),
        severe_losses=sum(outcome.severe_loss is True for outcome in observed),
        reason_counts=tuple(sorted(reasons.items())),
    )


__all__ = [
    "COMPANY_REPLAY_ARMS",
    "CompanyReplayArm",
    "CompanyReplayCosts",
    "CompanyReplayDay",
    "CompanyReplayEffect",
    "CompanyReplayMetrics",
    "CompanyReplayOutcome",
    "CompanyReplayRejection",
    "CompanyReplayRiskEffect",
    "CompanyReplaySeatMetrics",
    "CompanyReplaySelection",
    "EvaluateCompanyReplay",
]
