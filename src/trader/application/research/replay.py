"""Score-R3 historical production-local baseline replay and report aggregation."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable

from trader.application.research.models import (
    HistoricalEvaluatedCandidate,
    HistoricalExtractedDay,
    ScoreR2HistoricalExtraction,
)
from trader.application.research.ports import HistoricalBaselineReplayEvaluator
from trader.application.research.replay_models import (
    BaselineAggregateMetrics,
    BaselineDayMetrics,
    BaselineReplaySelection,
    ScoreR3BaselineReport,
)
from trader.domain.research.baseline import mean_rank_ic, population_spearman, quantile_bucket, stock_net_contribution
from trader.domain.research.historical import CostSettlementBasis

_COST_RATES = (0.002, 0.005, 0.01)
_MAXIMUM_PER_BOARD = 4
_MAXIMUM_PER_INDUSTRY = 2


class ScoreR3BaselineReplayer:
    """Replay the production baseline through an injected production pure-function adapter."""

    def __init__(self, evaluator: HistoricalBaselineReplayEvaluator) -> None:
        self._evaluator = evaluator

    def replay(self, extraction: ScoreR2HistoricalExtraction) -> ScoreR3BaselineReport:
        days = tuple(self._replay_day(day) for day in extraction.days)
        aggregate = _aggregate(days)
        return ScoreR3BaselineReport(
            "replayed" if extraction.status == "extracted" and len(days) == 40 else "exploratory",
            extraction.content_hash,
            extraction.status,
            days,
            aggregate,
        )

    def _replay_day(self, day: HistoricalExtractedDay) -> BaselineDayMetrics:
        replay = self._evaluator.replay(day)
        _validate_replay(day, replay)
        summary_by_code = {item.code: item for item in day.summary.candidates}
        basis_by_code = {item.basis.code: item.basis for item in day.full_fields.settlements}
        selected = tuple(
            sorted(
                (item for item in replay if item.production_rank is not None),
                key=lambda item: item.production_rank or 0,
            )
        )
        selected_codes = tuple(item.code for item in selected)
        oracle = tuple(
            sorted((item for item in replay if item.oracle_rank is not None), key=lambda item: item.oracle_rank or 0)
        )
        oracle_codes = tuple(item.code for item in oracle)
        weight = 1.0 / len(selected) if selected else 0.0
        net_returns = tuple(_portfolio_return(selected, basis_by_code, weight, cost) for cost in _COST_RATES)
        selected_basis = tuple(basis_by_code[item.code] for item in selected)
        recalled_oracle_count = sum(1 for item in oracle if summary_by_code[item.code].production_top120)
        recall = recalled_oracle_count / len(oracle) if oracle else None
        coverage_values = tuple(
            math.fsum(
                component.weight
                for component in summary_by_code[item.code].final_components
                if component.value is not None
            )
            for item in day.evaluated
        )
        field_coverage = math.fsum(coverage_values) / len(coverage_values) if coverage_values else 0.0
        board_fraction, industry_fraction = _concentration(day, selected_codes)
        rank_ic = population_spearman(
            tuple((item.final_score, basis_by_code[item.code].gross_excess_return) for item in day.evaluated)
        )
        bucket_returns = _bucket_returns(day, basis_by_code)
        return BaselineDayMetrics(
            day.summary.trade_date,
            day.content_hash,
            day.summary.input_hash,
            selected_codes,
            oracle_codes,
            "selected" if selected else "no_decision",
            len(day.evaluated),
            len(oracle),
            recalled_oracle_count,
            (net_returns[0], net_returns[1], net_returns[2]),
            _mean(tuple(item.mae_atr20 for item in selected_basis)),
            _mean(tuple(1.0 if item.mae_atr20 <= -1.5 else 0.0 for item in selected_basis)),
            recall,
            field_coverage,
            board_fraction,
            industry_fraction,
            rank_ic,
            bucket_returns,
        )


def _validate_replay(day: HistoricalExtractedDay, replay: tuple[BaselineReplaySelection, ...]) -> None:
    codes = tuple(item.code for item in replay)
    expected = {item.code for item in day.evaluated}
    if len(codes) != len(set(codes)) or set(codes) != expected:
        raise ValueError("baseline replay must exactly cover the evaluated active set")
    evaluated_by_code = {item.code: item for item in day.evaluated}
    production = _validate_ranked_pool(replay, evaluated_by_code, lambda item: item.production_rank)
    oracle = _validate_ranked_pool(replay, evaluated_by_code, lambda item: item.oracle_rank)
    summary_by_code = {item.code: item for item in day.summary.candidates}
    if any(not summary_by_code[item.code].production_top120 for item in production):
        raise ValueError("production baseline selected an item outside the production Top120")
    _validate_concentration(production, evaluated_by_code, "production")
    _validate_concentration(oracle, evaluated_by_code, "oracle")


def _validate_ranked_pool(
    replay: tuple[BaselineReplaySelection, ...],
    evaluated_by_code: dict[str, HistoricalEvaluatedCandidate],
    rank_getter: Callable[[BaselineReplaySelection], int | None],
) -> tuple[BaselineReplaySelection, ...]:
    selected = tuple(item for item in replay if rank_getter(item) is not None)
    ranks = sorted(rank for item in selected if (rank := rank_getter(item)) is not None)
    if ranks != list(range(1, len(selected) + 1)):
        raise ValueError("baseline replay selected ranks must be contiguous")
    if len(selected) > 6:
        raise ValueError("baseline replay cannot select more than Top6")
    if any("formal" not in evaluated_by_code[item.code].eligible_pools for item in selected):
        raise ValueError("baseline replay selected an item outside the formal pool")
    expected_order = tuple(
        item.code
        for item in sorted(
            (evaluated_by_code[item.code] for item in selected),
            key=lambda value: (-value.final_score, -value.local_score, value.code),
        )
    )
    actual_order = tuple(item.code for item in sorted(selected, key=lambda item: rank_getter(item) or 0))
    if actual_order != expected_order:
        raise ValueError("baseline replay rank order must match the production stable score order")
    return selected


def _validate_concentration(
    selected: tuple[BaselineReplaySelection, ...],
    evaluated_by_code: dict[str, HistoricalEvaluatedCandidate],
    label: str,
) -> None:
    board_counts = Counter(evaluated_by_code[item.code].board for item in selected)
    industry_counts = Counter(evaluated_by_code[item.code].industry or "unknown" for item in selected)
    if any(value > _MAXIMUM_PER_BOARD for value in board_counts.values()):
        raise ValueError(f"baseline {label} replay exceeds the board concentration limit")
    if any(value > _MAXIMUM_PER_INDUSTRY for value in industry_counts.values()):
        raise ValueError(f"baseline {label} replay exceeds the industry concentration limit")


def _concentration(day: HistoricalExtractedDay, selected_codes: tuple[str, ...]) -> tuple[float, float]:
    if not selected_codes:
        return 0.0, 0.0
    by_code = {item.code: item for item in day.evaluated}
    board_counts = Counter(by_code[code].board for code in selected_codes)
    industry_counts = Counter(by_code[code].industry or "unknown" for code in selected_codes)
    return max(board_counts.values()) / len(selected_codes), max(industry_counts.values()) / len(selected_codes)


def _bucket_returns(
    day: HistoricalExtractedDay,
    basis_by_code: dict[str, CostSettlementBasis],
) -> tuple[float | None, float | None, float | None, float | None, float | None]:
    if len(day.evaluated) < 5:
        return (None, None, None, None, None)
    buckets = quantile_bucket(tuple((item.code, item.final_score) for item in day.evaluated))
    results: list[float | None] = []
    for bucket in range(1, 6):
        codes = tuple(code for code, assigned in buckets.items() if assigned == bucket)
        if not codes:
            results.append(None)
            continue
        values = tuple(
            basis_by_code[code].gross_excess_return - basis_by_code[code].turnover * _COST_RATES[0] for code in codes
        )
        results.append(math.fsum(values) / len(values))
    return tuple(results)  # type: ignore[return-value]


def _aggregate(days: tuple[BaselineDayMetrics, ...]) -> BaselineAggregateMetrics:
    if not days:
        return BaselineAggregateMetrics(
            (0.0, 0.0, 0.0), None, None, None, 0.0, 0.0, 0.0, None, (None, None, None, None, None)
        )
    net_returns = tuple(math.fsum(day.net_excess_returns[index] for day in days) / len(days) for index in range(3))
    selected_count = sum(len(day.selected_codes) for day in days)
    oracle_count = sum(day.oracle_selected_count for day in days)
    evaluated_count = sum(day.evaluated_count for day in days)
    return BaselineAggregateMetrics(
        (net_returns[0], net_returns[1], net_returns[2]),
        _weighted_selected_mean(days, lambda day: day.mean_mae_atr20, selected_count),
        _weighted_selected_mean(days, lambda day: day.severe_drawdown_rate, selected_count),
        sum(day.recalled_oracle_count for day in days) / oracle_count if oracle_count else None,
        math.fsum(day.field_coverage * day.evaluated_count for day in days) / evaluated_count
        if evaluated_count
        else 0.0,
        math.fsum(day.maximum_board_fraction for day in days) / len(days),
        math.fsum(day.maximum_industry_fraction for day in days) / len(days),
        mean_rank_ic(tuple(day.rank_ic for day in days)),
        tuple(_optional_mean(tuple(day.score_bucket_net_excess_20bp[index] for day in days)) for index in range(5)),  # type: ignore[arg-type]
    )


def _mean(values: tuple[float, ...]) -> float | None:
    return math.fsum(values) / len(values) if values else None


def _optional_mean(values: tuple[float | None, ...]) -> float | None:
    known = tuple(value for value in values if value is not None)
    return _mean(known)


def _weighted_selected_mean(
    days: tuple[BaselineDayMetrics, ...],
    value_getter: Callable[[BaselineDayMetrics], float | None],
    selected_count: int,
) -> float | None:
    if not selected_count:
        return None
    return (
        math.fsum(value * len(day.selected_codes) for day in days if (value := value_getter(day)) is not None)
        / selected_count
    )


def _portfolio_return(
    selected: tuple[BaselineReplaySelection, ...],
    basis_by_code: dict[str, CostSettlementBasis],
    weight: float,
    cost: float,
) -> float:
    return math.fsum(
        stock_net_contribution(
            weight,
            basis_by_code[item.code].gross_excess_return,
            basis_by_code[item.code].turnover,
            cost,
        )
        for item in selected
    )


__all__ = ["ScoreR3BaselineReplayer"]
