"""Immutable label, benchmark, cost, and temporal split preregistration."""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

from trader.domain.research.h1_point_in_time import (
    H1_SOURCE_CUTOFF,
    H1CoverageState,
    H1Strategy,
    canonical_hash,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_STRATEGY_ORDER = {"today": 0, "tomorrow": 1, "d25": 2}
_MINIMUM_COMMON_DAYS = 1_000
_MINIMUM_TERMINAL_DAYS = 200
_EMBARGO_DAYS = 5

HistoricalPreregistrationStatus = Literal["preregistered", "historical_data_insufficient"]
HistoricalLabelAggregate = Literal["single_horizon", "arithmetic_mean"]
HistoricalAnchor = Literal["11:20", "14:50"]


@dataclass(frozen=True)
class H1CoverageMetadata:
    strategy: H1Strategy
    coverage_state: H1CoverageState
    common_trading_dates: tuple[date, ...]
    universe_hash: str
    h1_manifest_hash: str
    source_cutoff: date
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        if self.strategy not in _STRATEGY_ORDER:
            raise ValueError("historical label strategy is invalid")
        if self.coverage_state not in {"coverage_ready", "historical_data_insufficient"}:
            raise ValueError("historical label H1 coverage state is invalid")
        _hash(self.universe_hash, "historical label universe")
        _hash(self.h1_manifest_hash, "historical label H1 manifest")
        if self.source_cutoff != H1_SOURCE_CUTOFF:
            raise ValueError("historical label source cutoff is invalid")
        _strict_dates(self.common_trading_dates)
        if self.common_trading_dates and self.common_trading_dates[-1] > self.source_cutoff:
            raise ValueError("historical label dates exceed source cutoff")
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class HistoricalLabelContract:
    strategy: H1Strategy
    anchor: HistoricalAnchor
    label_version: str
    horizons: tuple[int, ...]
    aggregate: HistoricalLabelAggregate
    benchmark_version: str
    cost_version: str
    cost_bps: tuple[int, int, int]
    gate_cost_bps: tuple[int, int]
    stress_cost_bps: int
    required_metrics: tuple[str, ...]
    parity_dimensions: tuple[str, ...] = (
        "trade_date",
        "code",
        "anchor",
        "hard_filter_eligibility",
        "cost",
        "benchmark_market_data",
    )
    same_population_required: bool = True
    cash_days_in_denominator: bool = True
    deepseek_history_allowed: bool = False
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        expected = _label_values(self.strategy)
        actual = (self.anchor, self.label_version, self.horizons, self.aggregate, self.required_metrics)
        if actual != expected:
            raise ValueError("historical label contract does not match its strategy")
        if self.benchmark_version != "point_in_time_local_only_equal_weight_v1":
            raise ValueError("historical label benchmark identity is invalid")
        if (
            self.cost_version != "round_trip_20_50_100bp_v1"
            or self.cost_bps != (20, 50, 100)
            or self.gate_cost_bps != (20, 50)
            or self.stress_cost_bps != 100
        ):
            raise ValueError("historical label cost identity is invalid")
        if not self.same_population_required or not self.cash_days_in_denominator or self.deepseek_history_allowed:
            raise ValueError("historical label point-in-time parity is invalid")
        if self.parity_dimensions != (
            "trade_date",
            "code",
            "anchor",
            "hard_filter_eligibility",
            "cost",
            "benchmark_market_data",
        ):
            raise ValueError("historical label parity dimensions are invalid")
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class HistoricalTemporalSplit:
    training_dates: tuple[date, ...]
    first_embargo_dates: tuple[date, ...]
    confirmation_dates: tuple[date, ...]
    second_embargo_dates: tuple[date, ...]
    terminal_holdout_dates: tuple[date, ...]
    first_trade_date: date
    last_trade_date: date
    date_set_hash: str
    embargo_days_per_boundary: int = _EMBARGO_DAYS
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        groups = (
            self.training_dates,
            self.first_embargo_dates,
            self.confirmation_dates,
            self.second_embargo_dates,
            self.terminal_holdout_dates,
        )
        if any(not group for group in groups):
            raise ValueError("historical label split groups must be non-empty")
        if self.embargo_days_per_boundary != _EMBARGO_DAYS or any(
            len(group) != _EMBARGO_DAYS for group in (self.first_embargo_dates, self.second_embargo_dates)
        ):
            raise ValueError("historical label split requires two five-day embargoes")
        _strict_dates(self.all_dates)
        if self.first_trade_date != self.all_dates[0] or self.last_trade_date != self.all_dates[-1]:
            raise ValueError("historical label split date bounds are inconsistent")
        if len(self.terminal_holdout_dates) < _MINIMUM_TERMINAL_DAYS:
            raise ValueError("historical label terminal holdout must retain at least 200 dates")
        if self.date_set_hash != canonical_hash(self.all_dates):
            raise ValueError("historical label date set hash is invalid")
        object.__setattr__(self, "content_hash", canonical_hash(self))

    @property
    def all_dates(self) -> tuple[date, ...]:
        return (
            self.training_dates
            + self.first_embargo_dates
            + self.confirmation_dates
            + self.second_embargo_dates
            + self.terminal_holdout_dates
        )


@dataclass(frozen=True)
class HistoricalLabelPreregistration:
    strategy: H1Strategy
    status: HistoricalPreregistrationStatus
    h1_metadata_hash: str
    h1_manifest_hash: str
    universe_hash: str
    source_cutoff: date
    label: HistoricalLabelContract
    split: HistoricalTemporalSplit | None
    failure_reasons: tuple[str, ...]
    terminal_holdout_status: Literal["terminal_holdout_not_opened"] = "terminal_holdout_not_opened"
    candidate_results_generated: bool = False
    production_authority: bool = False
    schema_version: str = "historical_label_preregistration_v1"
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        for value, label in (
            (self.h1_metadata_hash, "historical label metadata"),
            (self.h1_manifest_hash, "historical label H1 manifest"),
            (self.universe_hash, "historical label universe"),
        ):
            _hash(value, label)
        if self.strategy != self.label.strategy or self.source_cutoff != H1_SOURCE_CUTOFF:
            raise ValueError("historical label preregistration identity is invalid")
        reasons = tuple(sorted(set(self.failure_reasons)))
        if self.status == "preregistered":
            if self.split is None or reasons:
                raise ValueError("preregistered historical label requires one clean split")
        elif self.status == "historical_data_insufficient":
            if self.split is not None or not reasons:
                raise ValueError("insufficient historical label requires bounded reasons and no split")
        else:
            raise ValueError("historical label preregistration status is invalid")
        if (
            self.terminal_holdout_status != "terminal_holdout_not_opened"
            or self.candidate_results_generated
            or self.production_authority
        ):
            raise ValueError("historical label preregistration cannot open holdout or production authority")
        if self.schema_version != "historical_label_preregistration_v1":
            raise ValueError("historical label preregistration schema is invalid")
        object.__setattr__(self, "failure_reasons", reasons)
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class HistoricalLabelPreregistrationBatch:
    strategies: tuple[HistoricalLabelPreregistration, ...]
    schema_version: str = "historical_label_preregistration_batch_v1"
    production_authority: bool = False
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        ordered = tuple(sorted(self.strategies, key=lambda item: _STRATEGY_ORDER[item.strategy]))
        if tuple(item.strategy for item in ordered) != ("today", "tomorrow", "d25"):
            raise ValueError("historical label batch requires each strategy exactly once")
        if self.schema_version != "historical_label_preregistration_batch_v1" or self.production_authority:
            raise ValueError("historical label batch cannot authorize production")
        object.__setattr__(self, "strategies", ordered)
        object.__setattr__(self, "content_hash", canonical_hash(self))


def preregister_historical_label(metadata: H1CoverageMetadata) -> HistoricalLabelPreregistration:
    label = _label_contract(metadata.strategy)
    reasons: list[str] = []
    if metadata.coverage_state != "coverage_ready":
        reasons.append("h1_historical_data_insufficient")
    if len(metadata.common_trading_dates) < _MINIMUM_COMMON_DAYS:
        reasons.append("common_trading_days_below_1000")
    terminal_start = len(metadata.common_trading_dates) * 80 // 100
    if len(metadata.common_trading_dates) - terminal_start < _MINIMUM_TERMINAL_DAYS:
        reasons.append("terminal_holdout_below_200")
    split = None if reasons else _split(metadata.common_trading_dates)
    return HistoricalLabelPreregistration(
        strategy=metadata.strategy,
        status="historical_data_insufficient" if reasons else "preregistered",
        h1_metadata_hash=metadata.content_hash,
        h1_manifest_hash=metadata.h1_manifest_hash,
        universe_hash=metadata.universe_hash,
        source_cutoff=metadata.source_cutoff,
        label=label,
        split=split,
        failure_reasons=tuple(reasons),
    )


def preregister_historical_labels(
    metadata: tuple[H1CoverageMetadata, ...],
) -> HistoricalLabelPreregistrationBatch:
    if len(metadata) != 3 or len({item.strategy for item in metadata}) != 3:
        raise ValueError("historical label metadata requires each strategy exactly once")
    return HistoricalLabelPreregistrationBatch(tuple(preregister_historical_label(item) for item in metadata))


def _split(dates: tuple[date, ...]) -> HistoricalTemporalSplit:
    first_boundary = len(dates) * 60 // 100
    second_boundary = len(dates) * 80 // 100
    split = HistoricalTemporalSplit(
        training_dates=dates[: first_boundary - _EMBARGO_DAYS],
        first_embargo_dates=dates[first_boundary - _EMBARGO_DAYS : first_boundary],
        confirmation_dates=dates[first_boundary : second_boundary - _EMBARGO_DAYS],
        second_embargo_dates=dates[second_boundary - _EMBARGO_DAYS : second_boundary],
        terminal_holdout_dates=dates[second_boundary:],
        first_trade_date=dates[0],
        last_trade_date=dates[-1],
        date_set_hash=canonical_hash(dates),
    )
    if split.all_dates != dates:
        raise ValueError("historical label split must retain every date exactly once")
    return split


def _label_contract(strategy: H1Strategy) -> HistoricalLabelContract:
    anchor, label_version, horizons, aggregate, metrics = _label_values(strategy)
    return HistoricalLabelContract(
        strategy=strategy,
        anchor=anchor,
        label_version=label_version,
        horizons=horizons,
        aggregate=aggregate,
        benchmark_version="point_in_time_local_only_equal_weight_v1",
        cost_version="round_trip_20_50_100bp_v1",
        cost_bps=(20, 50, 100),
        gate_cost_bps=(20, 50),
        stress_cost_bps=100,
        required_metrics=metrics,
    )


def _label_values(
    strategy: H1Strategy,
) -> tuple[HistoricalAnchor, str, tuple[int, ...], HistoricalLabelAggregate, tuple[str, ...]]:
    common = (
        "daily_portfolio_net_excess_mean",
        "moving_block_bootstrap_95_lower_bound",
        "severe_loss_rate",
        "turnover",
        "capacity",
        "rank_ic",
        "q5_q1",
        "board_concentration",
        "industry_concentration",
    )
    if strategy == "today":
        return (
            "11:20",
            "today_1120_to_t1_close_market_excess_after_cost_v1",
            (1,),
            "single_horizon",
            (*common, "t1_low_mae_atr20"),
        )
    if strategy == "tomorrow":
        return (
            "14:50",
            "tomorrow_1450_to_t1_close_market_excess_after_cost_v1",
            (1,),
            "single_horizon",
            (*common, "t1_low_mae_atr20", "risk_fact_coverage"),
        )
    if strategy == "d25":
        return (
            "14:50",
            "d25_1450_to_t2_t5_mean_market_excess_after_cost_v1",
            (2, 3, 4, 5),
            "arithmetic_mean",
            (*common, "four_horizon_net_excess", "worst_interval_mae_atr20", "overlapping_holding_turnover"),
        )
    raise ValueError("historical label strategy is invalid")


def _strict_dates(values: tuple[date, ...]) -> None:
    if values and any(left >= right for left, right in zip(values, values[1:], strict=False)):
        raise ValueError("historical label dates must be strictly increasing")


def _hash(value: str, label: str) -> None:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} identity must be SHA-256")


__all__ = [
    "H1CoverageMetadata",
    "HistoricalLabelContract",
    "HistoricalLabelPreregistration",
    "HistoricalLabelPreregistrationBatch",
    "HistoricalTemporalSplit",
    "preregister_historical_label",
    "preregister_historical_labels",
]
