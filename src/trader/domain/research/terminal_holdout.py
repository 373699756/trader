"""Pure, one-shot terminal holdout evaluation for the three score strategies."""

from __future__ import annotations

import math
import re
import dataclasses
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from trader.domain.research.baseline import population_spearman
from trader.domain.research.paired_statistics import PreregisteredBootstrapPlan, paired_moving_block_statistics

TerminalStrategy = Literal["today", "tomorrow", "d25"]
TerminalStatus = Literal["historical_data_insufficient", "historical_rejected", "historical_validated"]
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_BOARDS = {"main", "chinext", "star"}
_MIN_DATES = 200


@dataclass(frozen=True)
class TerminalHoldoutRow:
    """One point-in-time candidate row and its matched local-only control row."""

    trade_date: date
    code: str
    board: str
    industry: str
    market_state: str
    volatility_state: str
    liquidity_state: str
    predicted_net_excess_return: float
    actual_net_excess_returns: tuple[float, float, float]
    baseline_net_excess_returns: tuple[float, float, float]
    selected: bool
    baseline_selected: bool
    severe_loss: bool
    baseline_severe_loss: bool
    mae_atr20: float
    baseline_mae_atr20: float
    point_in_time_parity: bool
    horizon_net_excess_returns: tuple[float, ...] = ()
    baseline_horizon_net_excess_returns: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        if len(self.code) != 6 or not self.code.isdigit():
            raise ValueError("terminal holdout code must contain six digits")
        if self.board not in _BOARDS or not self.industry.strip():
            raise ValueError("terminal holdout identity is invalid")
        if not self.market_state or not self.volatility_state or not self.liquidity_state:
            raise ValueError("terminal holdout state labels are required")
        if len(self.actual_net_excess_returns) != 3 or len(self.baseline_net_excess_returns) != 3:
            raise ValueError("terminal holdout requires 20bp, 50bp and 100bp returns")
        values: tuple[float, ...] = (*self.actual_net_excess_returns, *self.baseline_net_excess_returns, self.predicted_net_excess_return)
        values += (*self.horizon_net_excess_returns, *self.baseline_horizon_net_excess_returns, self.mae_atr20, self.baseline_mae_atr20)
        if any(not math.isfinite(value) for value in values):
            raise ValueError("terminal holdout numeric values must be finite")
        if self.baseline_horizon_net_excess_returns and len(self.horizon_net_excess_returns) != len(self.baseline_horizon_net_excess_returns):
            raise ValueError("terminal holdout horizon returns must be paired")
        if self.horizon_net_excess_returns and len(self.horizon_net_excess_returns) not in {4}:
            raise ValueError("terminal holdout horizon returns must contain T+2 through T+5")


@dataclass(frozen=True)
class TerminalHoldoutMetrics:
    evaluated_trade_dates: int
    evaluated_rows: int
    selected_rows: int
    baseline_selected_rows: int
    mean_net_excess_returns: tuple[float, float, float]
    baseline_mean_net_excess_returns: tuple[float, float, float]
    paired_net_increments: tuple[float, float, float]
    bootstrap_lower_bounds: tuple[float | None, float | None, float | None]
    severe_loss_rate: float | None
    baseline_severe_loss_rate: float | None
    turnover: float
    baseline_turnover: float
    rank_ic: float | None
    top_bottom_quintile_spread: float | None
    maximum_stock_positive_fraction: float
    top_five_positive_fraction: float
    maximum_board_fraction: float
    maximum_industry_count: int
    capacity: float
    baseline_capacity: float
    horizon_mean_net_excess_returns: tuple[float, ...] = ()
    baseline_horizon_mean_net_excess_returns: tuple[float, ...] = ()
    state_sample_counts: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        if min(self.evaluated_trade_dates, self.evaluated_rows, self.selected_rows, self.baseline_selected_rows) < 0:
            raise ValueError("terminal holdout counts cannot be negative")
        for values in (
            self.mean_net_excess_returns,
            self.baseline_mean_net_excess_returns,
            self.paired_net_increments,
            self.bootstrap_lower_bounds,
        ):
            if len(values) != 3:
                raise ValueError("terminal holdout cost metrics require three rates")
        all_values: tuple[float, ...] = (
            *self.mean_net_excess_returns,
            *self.baseline_mean_net_excess_returns,
            *self.paired_net_increments,
            self.turnover,
            self.baseline_turnover,
            self.capacity,
            self.baseline_capacity,
        )
        all_values += tuple(value for value in self.bootstrap_lower_bounds if value is not None)
        all_values += tuple(value for value in (self.severe_loss_rate, self.baseline_severe_loss_rate, self.rank_ic, self.top_bottom_quintile_spread) if value is not None)
        if any(not math.isfinite(value) for value in all_values):
            raise ValueError("terminal holdout metrics must be finite")
        rates = (self.severe_loss_rate, self.baseline_severe_loss_rate, self.maximum_stock_positive_fraction, self.top_five_positive_fraction, self.maximum_board_fraction)
        if any(value is not None and not 0.0 <= value <= 1.0 for value in rates):
            raise ValueError("terminal holdout rates must be in [0, 1]")
        if self.maximum_industry_count < 0:
            raise ValueError("terminal holdout industry count cannot be negative")


@dataclass(frozen=True)
class TerminalHoldoutReport:
    strategy: TerminalStrategy
    research_identity: str
    parent_hash: str
    candidate_hash: str
    anchor: str
    terminal_holdout_opened: bool
    status: TerminalStatus
    metrics: TerminalHoldoutMetrics
    failure_reasons: tuple[str, ...]
    terminal_trade_dates: tuple[date, ...]
    production_authority: bool = False
    schema_version: str = "historical_terminal_holdout_v1"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.strategy not in {"today", "tomorrow", "d25"} or not self.research_identity:
            raise ValueError("terminal holdout strategy identity is invalid")
        if _SHA256.fullmatch(self.parent_hash) is None or _SHA256.fullmatch(self.candidate_hash) is None:
            raise ValueError("terminal holdout hashes must be SHA-256")
        if self.status not in {"historical_data_insufficient", "historical_rejected", "historical_validated"}:
            raise ValueError("terminal holdout status is invalid")
        if self.production_authority:
            raise ValueError("terminal holdout cannot authorize production")
        reasons = tuple(sorted(set(self.failure_reasons)))
        if self.status == "historical_validated" and reasons:
            raise ValueError("validated terminal holdout cannot contain failure reasons")
        if self.status != "historical_validated" and not reasons:
            raise ValueError("non-validated terminal holdout requires a failure reason")
        if self.terminal_holdout_opened != bool(self.terminal_trade_dates):
            raise ValueError("terminal holdout opened state must match dates")
        object.__setattr__(self, "failure_reasons", reasons)
        object.__setattr__(self, "terminal_trade_dates", tuple(self.terminal_trade_dates))
        object.__setattr__(self, "content_hash", _canonical_hash(self))


def evaluate_terminal_holdout(
    *,
    strategy: TerminalStrategy,
    research_identity: str,
    parent_hash: str,
    candidate_hash: str,
    rows: tuple[TerminalHoldoutRow, ...] | list[TerminalHoldoutRow],
    parent_status: str = "historical_candidate_ready",
    parent_failure_reasons: tuple[str, ...] = (),
    anchor: str | None = None,
    bootstrap_block_days: int | None = None,
) -> TerminalHoldoutReport:
    """Run exactly one deterministic terminal evaluation over an already sealed candidate."""

    if parent_status not in {"historical_candidate_ready", "historical_daily_close_proxy_validated"}:
        parent_result: TerminalStatus = (
            "historical_data_insufficient" if parent_status == "historical_data_insufficient" else "historical_rejected"
        )
        return _closed_report(strategy, research_identity, parent_hash, candidate_hash, anchor, parent_result, parent_failure_reasons or (parent_status,))
    ordered = tuple(sorted(rows, key=lambda item: (item.trade_date, item.code)))
    if len({(item.trade_date, item.code) for item in ordered}) != len(ordered):
        raise ValueError("terminal holdout rows must be unique by date and code")
    if any(not item.point_in_time_parity for item in ordered):
        raise ValueError("terminal holdout point-in-time parity is incomplete")
    if strategy == "d25" and any(
        len(item.horizon_net_excess_returns) != 4 or len(item.baseline_horizon_net_excess_returns) != 4
        for item in ordered
    ):
        raise ValueError("D25 terminal holdout requires paired T+2 through T+5 returns")
    dates = tuple(sorted({item.trade_date for item in ordered}))
    if len(dates) < _MIN_DATES:
        return _closed_report(strategy, research_identity, parent_hash, candidate_hash, anchor, "historical_data_insufficient", ("terminal_trade_dates_below_200",))
    metrics = _metrics(ordered, strategy=strategy, bootstrap_block_days=bootstrap_block_days or (10 if strategy == "d25" else 5))
    failures = _gate_failures(metrics, strategy)
    insufficient_state = any(count < 40 for _label, count in metrics.state_sample_counts)
    if insufficient_state:
        result_status: TerminalStatus = "historical_data_insufficient"
    else:
        result_status = "historical_validated" if not failures else "historical_rejected"
    return TerminalHoldoutReport(
        strategy,
        research_identity,
        parent_hash,
        candidate_hash,
        anchor or _anchor(strategy),
        True,
        result_status,
        metrics,
        failures,
        dates,
    )


def _closed_report(strategy: TerminalStrategy, identity: str, parent_hash: str, candidate_hash: str, anchor: str | None, status: TerminalStatus, reasons: tuple[str, ...]) -> TerminalHoldoutReport:
    return TerminalHoldoutReport(strategy, identity, parent_hash, candidate_hash, anchor or _anchor(strategy), False, status, _empty_metrics(), reasons, ())


def _metrics(rows: tuple[TerminalHoldoutRow, ...], *, strategy: TerminalStrategy, bootstrap_block_days: int) -> TerminalHoldoutMetrics:
    grouped: dict[date, list[TerminalHoldoutRow]] = defaultdict(list)
    for row in rows:
        grouped[row.trade_date].append(row)
    candidate_daily: list[tuple[float, float, float]] = []
    baseline_daily: list[tuple[float, float, float]] = []
    candidate_sets: list[frozenset[str]] = []
    baseline_sets: list[frozenset[str]] = []
    rank_ics: list[float] = []
    spreads: list[float] = []
    positive_by_code: dict[str, float] = defaultdict(float)
    severe_values: list[float] = []
    baseline_severe_values: list[float] = []
    state_counts: dict[str, int] = defaultdict(int)
    horizon: list[tuple[float, ...]] = []
    baseline_horizon: list[tuple[float, ...]] = []
    for day in sorted(grouped):
        day_rows = tuple(sorted(grouped[day], key=lambda item: item.code))
        selected = tuple(row for row in day_rows if row.selected)
        baseline = tuple(row for row in day_rows if row.baseline_selected)
        candidate_sets.append(frozenset(row.code for row in selected))
        baseline_sets.append(frozenset(row.code for row in baseline))
        candidate_daily.append(_cost_means(tuple(row.actual_net_excess_returns for row in selected)))
        baseline_daily.append(_cost_means(tuple(row.baseline_net_excess_returns for row in baseline)))
        if selected:
            severe_values.append(sum(row.severe_loss for row in selected) / len(selected))
            for row in selected:
                positive_by_code[row.code] += max(0.0, row.actual_net_excess_returns[0])
        if baseline:
            baseline_severe_values.append(sum(row.baseline_severe_loss for row in baseline) / len(baseline))
        pairs = tuple((row.predicted_net_excess_return, row.actual_net_excess_returns[0]) for row in day_rows)
        if (value := population_spearman(pairs)) is not None:
            rank_ics.append(value)
            ordered = sorted(day_rows, key=lambda row: (-row.predicted_net_excess_return, row.code))
            split = max(1, len(ordered) // 5)
            spreads.append(_mean(tuple(row.actual_net_excess_returns[0] for row in ordered[:split])) - _mean(tuple(row.actual_net_excess_returns[0] for row in ordered[-split:])))
        state_counts[f"market:{day_rows[0].market_state}"] += 1
        state_counts[f"volatility:{day_rows[0].volatility_state}"] += 1
        state_counts[f"liquidity:{day_rows[0].liquidity_state}"] += 1
        if day_rows[0].horizon_net_excess_returns:
            horizon.append(tuple(_mean(tuple(row.horizon_net_excess_returns[index] for row in selected)) for index in range(4)))
            baseline_horizon.append(tuple(_mean(tuple(row.baseline_horizon_net_excess_returns[index] for row in baseline)) for index in range(4)))
    paired: tuple[float, float, float] = tuple(
        _mean(tuple(candidate_daily[day][index] - baseline_daily[day][index] for day in range(len(candidate_daily))))
        for index in range(3)
    )  # type: ignore[assignment]
    lowers = _cost_lowers(
        candidate_daily,
        baseline_daily,
        strategy=strategy,
        bootstrap_block_days=bootstrap_block_days,
    )
    total_positive = math.fsum(positive_by_code.values())
    shares = sorted((value / total_positive for value in positive_by_code.values()), reverse=True) if total_positive else []
    selected_rows = sum(len(values) for values in candidate_sets)
    baseline_rows = sum(len(values) for values in baseline_sets)
    turnover = _turnover(candidate_sets)
    baseline_turnover = _turnover(baseline_sets)
    selected_rows_by_day = tuple(row for row in rows if row.selected)
    board_counts: dict[str, int] = defaultdict(int)
    industry_counts: dict[str, int] = defaultdict(int)
    maximum_industry_count = 0
    for row in selected_rows_by_day:
        board_counts[row.board] += 1
        industry_counts[row.industry] += 1
    for day in sorted(grouped):
        day_industries: dict[str, int] = defaultdict(int)
        for row in grouped[day]:
            if row.selected:
                day_industries[row.industry] += 1
        maximum_industry_count = max(maximum_industry_count, max(day_industries.values(), default=0))
    max_board = max(board_counts.values(), default=0) / selected_rows if selected_rows else 0.0
    return TerminalHoldoutMetrics(
        len(grouped), len(rows), selected_rows, baseline_rows,
        _cost_means(tuple(candidate_daily)),
        _cost_means(tuple(baseline_daily)),
        paired, lowers, _mean(tuple(severe_values)) if severe_values else None, _mean(tuple(baseline_severe_values)) if baseline_severe_values else None,
        turnover, baseline_turnover, _mean(tuple(rank_ics)) if rank_ics else None, _mean(tuple(spreads)) if spreads else None,
        shares[0] if shares else 0.0, math.fsum(shares[:5]), max_board, maximum_industry_count,
        selected_rows / len(grouped) if grouped else 0.0, baseline_rows / len(grouped) if grouped else 0.0,
        tuple(_mean(tuple(item[index] for item in horizon)) for index in range(4)) if horizon else (),
        tuple(_mean(tuple(item[index] for item in baseline_horizon)) for index in range(4)) if baseline_horizon else (),
        tuple(sorted(state_counts.items())),
    )


def _cost_means(values: tuple[tuple[float, float, float], ...]) -> tuple[float, float, float]:
    return (
        _mean(tuple(item[0] for item in values)),
        _mean(tuple(item[1] for item in values)),
        _mean(tuple(item[2] for item in values)),
    )


def _cost_lowers(
    candidate_daily: list[tuple[float, float, float]],
    baseline_daily: list[tuple[float, float, float]],
    *,
    strategy: TerminalStrategy,
    bootstrap_block_days: int,
) -> tuple[float | None, float | None, float | None]:
    values = tuple(
        tuple(candidate_daily[day][index] - baseline_daily[day][index] for day in range(len(candidate_daily)))
        for index in range(3)
    )
    result = tuple(
        paired_moving_block_statistics(
            values[index],
            plan=PreregisteredBootstrapPlan(
                "historical_terminal_holdout_v1",
                20260901,
                f"{strategy}_{index}",
                bootstrap_block_days,
                10_000,
            ),
        ).confidence_lower
        for index in range(3)
    )
    return (result[0], result[1], result[2])


def _gate_failures(metrics: TerminalHoldoutMetrics, strategy: TerminalStrategy) -> tuple[str, ...]:
    failures: list[str] = []
    if metrics.evaluated_trade_dates < _MIN_DATES:
        failures.append("terminal_trade_dates_below_200")
    if metrics.capacity < metrics.baseline_capacity:
        failures.append("capacity_below_production_baseline")
    if any(value <= 0.0 for value in metrics.mean_net_excess_returns[:2]) or any(value <= 0.0 for value in metrics.paired_net_increments[:2]):
        failures.append("net_excess_not_positive")
    if any(value is None or value <= 0.0 for value in metrics.bootstrap_lower_bounds[:2]):
        failures.append("bootstrap_lower_bound_not_positive")
    if metrics.severe_loss_rate is None or metrics.baseline_severe_loss_rate is None or metrics.severe_loss_rate > metrics.baseline_severe_loss_rate:
        failures.append("severe_loss_rate_worse")
    if metrics.turnover - metrics.baseline_turnover > 0.05:
        failures.append("turnover_increase_above_5_percent")
    if metrics.rank_ic is None or metrics.rank_ic <= 0.0:
        failures.append("rank_ic_not_positive")
    if metrics.top_bottom_quintile_spread is None or metrics.top_bottom_quintile_spread <= 0.0:
        failures.append("quintile_spread_not_positive")
    if metrics.maximum_stock_positive_fraction > 0.10 or metrics.top_five_positive_fraction > 0.30:
        failures.append("stock_concentration")
    if metrics.maximum_board_fraction > 0.60:
        failures.append("board_concentration")
    if metrics.maximum_industry_count > 2:
        failures.append("industry_concentration")
    if strategy == "d25" and metrics.horizon_mean_net_excess_returns and sum(value > 0.0 for value in metrics.horizon_mean_net_excess_returns) < 3:
        failures.append("d25_horizon_not_positive")
    return tuple(sorted(set(failures)))


def _empty_metrics() -> TerminalHoldoutMetrics:
    return TerminalHoldoutMetrics(0, 0, 0, 0, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (None, None, None), None, None, 0.0, 0.0, None, None, 0.0, 0.0, 0.0, 0, 0.0, 0.0)


def _turnover(sets: list[frozenset[str]]) -> float:
    if not sets:
        return 0.0
    previous: frozenset[str] = frozenset()
    values: list[float] = []
    for current in sets:
        values.append(len(current - previous) / len(current) if current else 0.0)
        previous = current
    return _mean(tuple(values))


def _mean(values: tuple[float, ...]) -> float:
    return math.fsum(values) / len(values) if values else 0.0


def _anchor(strategy: TerminalStrategy) -> str:
    return "11:20_unadjusted_point_in_time" if strategy == "today" else "14:50_unadjusted_point_in_time"


def _canonical_hash(value: object) -> str:
    def canonical(item: object) -> object:
        if dataclasses.is_dataclass(item):
            return {
                field.name: canonical(getattr(item, field.name))
                for field in dataclasses.fields(item)
                if field.init
            }
        if isinstance(item, date):
            return item.isoformat()
        if isinstance(item, tuple):
            return [canonical(child) for child in item]
        if isinstance(item, list):
            return [canonical(child) for child in item]
        if isinstance(item, dict):
            return {str(key): canonical(child) for key, child in item.items()}
        return item

    payload = json.dumps(canonical(value), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


__all__ = ["TerminalHoldoutMetrics", "TerminalHoldoutReport", "TerminalHoldoutRow", "TerminalStatus", "TerminalStrategy", "evaluate_terminal_holdout"]
