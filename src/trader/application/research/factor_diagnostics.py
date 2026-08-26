"""Offline native factor diagnostics over immutable Score-R2/R3 evidence."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import cast

from trader.application.research.factor_diagnostic_models import (
    FactorAggregateDiagnostic,
    FactorCostQuintiles,
    FactorDailyDiagnostic,
    FactorDiagnosticDimensionRecord,
    FactorDiagnosticDimensions,
    FactorLagDiagnostic,
    FactorStratumDiagnostic,
    OracleRecallDay,
    OracleRecallDiagnostic,
    QuintileValues,
    ScoreFactorDiagnosticReport,
    StratumDimension,
)
from trader.application.research.models import ScoreR2HistoricalExtraction
from trader.application.research.replay_models import ScoreR3BaselineReport
from trader.domain.research.baseline import mean_rank_ic, population_spearman, quantile_bucket
from trader.domain.research.factor_diagnostics import (
    factor_concentration,
    information_coefficient_ratio,
    monotonicity,
    population_pearson,
    top_bucket_turnover,
)

_COST_RATES = (0.002, 0.005, 0.01)
_DECAY_LAGS = (1, 3, 5)


@dataclass(frozen=True)
class _FactorRow:
    trade_date: date
    code: str
    board: str
    industry: str
    market_cap: float | None
    liquidity: float | None
    factor_value: float | None
    gross_excess_return: float
    turnover: float
    mae_atr20: float

    def net_excess(self, cost_rate: float) -> float:
        return self.gross_excess_return - self.turnover * cost_rate


class ScoreNativeFactorDiagnostics:
    """Build one deterministic, non-authoritative report from matching R2/R3 parents."""

    def evaluate(
        self,
        extraction: ScoreR2HistoricalExtraction,
        baseline: ScoreR3BaselineReport,
        dimensions: FactorDiagnosticDimensions,
    ) -> ScoreFactorDiagnosticReport:
        _validate_parent_evidence(extraction, baseline, dimensions)
        dimension_by_key = {(item.trade_date, item.code): item for item in dimensions.records}
        factor_names = _factor_names(extraction)
        factors = tuple(_evaluate_factor(extraction, dimension_by_key, factor_name) for factor_name in factor_names)
        oracle = _oracle_recall(extraction, baseline)
        return ScoreFactorDiagnosticReport(
            "evaluated" if len(extraction.days) == 40 else "exploratory",
            extraction.content_hash,
            baseline.report_hash,
            dimensions.content_hash,
            extraction.research_identity,
            extraction.research_spec_hash,
            factors,
            oracle,
        )


def _validate_parent_evidence(
    extraction: ScoreR2HistoricalExtraction,
    baseline: ScoreR3BaselineReport,
    dimensions: FactorDiagnosticDimensions,
) -> None:
    if (
        baseline.extraction_hash != extraction.content_hash
        or baseline.research_identity != extraction.research_identity
        or baseline.research_spec_hash != extraction.research_spec_hash
    ):
        raise ValueError("factor diagnostics require the matching R3 baseline parent")
    if dimensions.extraction_hash != extraction.content_hash:
        raise ValueError("factor diagnostic dimension extraction identity does not match R2")
    baseline_by_date = {item.trade_date: item for item in baseline.days}
    if set(baseline_by_date) != {item.summary.trade_date for item in extraction.days}:
        raise ValueError("factor diagnostics require identical R2/R3 day coverage")
    expected_dimensions: set[tuple[date, str]] = set()
    for day in extraction.days:
        baseline_day = baseline_by_date[day.summary.trade_date]
        if baseline_day.day_hash != day.content_hash or baseline_day.input_hash != day.summary.input_hash:
            raise ValueError("factor diagnostics require matching R2/R3 day and input hashes")
        expected_dimensions.update((day.summary.trade_date, item.code) for item in day.evaluated)
    actual_dimensions = {(item.trade_date, item.code) for item in dimensions.records}
    if actual_dimensions != expected_dimensions:
        raise ValueError("factor diagnostic dimensions must exactly cover every evaluated R2 row")
    day_by_date = {item.summary.trade_date: item for item in extraction.days}
    for record in dimensions.records:
        day = day_by_date[record.trade_date]
        if record.day_hash != day.content_hash or record.input_hash != day.summary.input_hash:
            raise ValueError("factor diagnostic dimension day/input identity does not match R2")


def _factor_names(extraction: ScoreR2HistoricalExtraction) -> tuple[str, ...]:
    names: set[str] = set()
    for day in extraction.days:
        evaluated_codes = {item.code for item in day.evaluated}
        for candidate in day.summary.candidates:
            if candidate.code in evaluated_codes:
                names.update(item.name for item in candidate.final_components)
    return tuple(sorted(names))


def _evaluate_factor(
    extraction: ScoreR2HistoricalExtraction,
    dimensions: dict[tuple[date, str], FactorDiagnosticDimensionRecord],
    factor_name: str,
) -> FactorAggregateDiagnostic:
    rows_by_day = _factor_rows(extraction, dimensions, factor_name)
    daily = tuple(
        _daily_diagnostic(trade_date, day_hash, input_hash, rows)
        for trade_date, day_hash, input_hash, rows in rows_by_day
    )
    all_rows = tuple(row for _trade_date, _day_hash, _input_hash, rows in rows_by_day for row in rows)
    observed = tuple(row for row in all_rows if row.factor_value is not None)
    total_count = len(all_rows)
    observed_count = len(observed)
    coverage = observed_count / total_count if total_count else 0.0
    cost_quintiles = tuple(_aggregate_cost_quintiles(daily, cost) for cost in _COST_RATES)
    severe = _weighted_quintile_mean(daily, "severe")
    mae = _weighted_quintile_mean(daily, "mae")
    contributions = tuple(
        (row.code, row.net_excess(_COST_RATES[0]) / len(day.top_quintile_codes))
        for (_trade_date, _day_hash, _input_hash, rows), day in zip(rows_by_day, daily, strict=True)
        for row in rows
        if row.code in day.top_quintile_codes
    )
    maximum_stock, top_five = factor_concentration(contributions)
    return FactorAggregateDiagnostic(
        factor_name,
        total_count,
        observed_count,
        coverage,
        1.0 - coverage,
        mean_rank_ic(tuple(item.ic for item in daily)),
        mean_rank_ic(tuple(item.rank_ic for item in daily)),
        information_coefficient_ratio(tuple(item.ic for item in daily)),
        cost_quintiles,
        severe,
        mae,
        maximum_stock,
        top_five,
        _lag_diagnostics(rows_by_day, daily),
        _strata(all_rows),
        daily,
    )


def _factor_rows(
    extraction: ScoreR2HistoricalExtraction,
    dimensions: dict[tuple[date, str], FactorDiagnosticDimensionRecord],
    factor_name: str,
) -> tuple[tuple[date, str, str, tuple[_FactorRow, ...]], ...]:
    result: list[tuple[date, str, str, tuple[_FactorRow, ...]]] = []
    for day in extraction.days:
        trade_date = day.summary.trade_date
        summaries = {item.code: item for item in day.summary.candidates}
        settlements = {item.basis.code: item.basis for item in day.full_fields.settlements}
        rows: list[_FactorRow] = []
        for evaluated in day.evaluated:
            summary = summaries[evaluated.code]
            component = next((item for item in summary.final_components if item.name == factor_name), None)
            dimension = dimensions[(trade_date, evaluated.code)]
            basis = settlements[evaluated.code]
            rows.append(
                _FactorRow(
                    trade_date,
                    evaluated.code,
                    evaluated.board,
                    evaluated.industry,
                    dimension.market_cap,
                    dimension.liquidity,
                    component.value if component is not None else None,
                    basis.gross_excess_return,
                    basis.turnover,
                    basis.mae_atr20,
                )
            )
        result.append((trade_date, day.content_hash, day.summary.input_hash, tuple(rows)))
    return tuple(result)


def _daily_diagnostic(
    trade_date: date,
    day_hash: str,
    input_hash: str,
    rows: tuple[_FactorRow, ...],
) -> FactorDailyDiagnostic:
    observed = tuple(row for row in rows if row.factor_value is not None)
    pairs = tuple((cast(float, row.factor_value), row.net_excess(_COST_RATES[0])) for row in observed)
    buckets = quantile_bucket(tuple((row.code, cast(float, row.factor_value)) for row in observed))
    sufficient = len(observed) >= 5
    counts = cast(
        tuple[int, int, int, int, int],
        tuple(sum(1 for bucket in buckets.values() if bucket == index) for index in range(1, 6)),
    )
    costs = tuple(_daily_cost_quintiles(observed, buckets, cost, sufficient) for cost in _COST_RATES)
    severe = _daily_bucket_values(observed, buckets, lambda row: 1.0 if row.mae_atr20 <= -1.5 else 0.0, sufficient)
    mae = _daily_bucket_values(observed, buckets, lambda row: row.mae_atr20, sufficient)
    coverage = len(observed) / len(rows) if rows else 0.0
    return FactorDailyDiagnostic(
        trade_date,
        day_hash,
        input_hash,
        len(rows),
        len(observed),
        coverage,
        1.0 - coverage,
        population_pearson(pairs),
        population_spearman(pairs),
        counts,
        costs,
        severe,
        mae,
        tuple(sorted(row.code for row in observed if sufficient and buckets[row.code] == 5)),
    )


def _daily_cost_quintiles(
    rows: tuple[_FactorRow, ...],
    buckets: dict[str, int],
    cost_rate: float,
    sufficient: bool,
) -> FactorCostQuintiles:
    values = _daily_bucket_values(rows, buckets, lambda row: row.net_excess(cost_rate), sufficient)
    monotonic_fraction, top_minus_bottom = monotonicity(values)
    return FactorCostQuintiles(cost_rate, values, monotonic_fraction, top_minus_bottom)


def _daily_bucket_values(
    rows: tuple[_FactorRow, ...],
    buckets: dict[str, int],
    getter: Callable[[_FactorRow], float],
    sufficient: bool,
) -> QuintileValues:
    if not sufficient:
        return (None, None, None, None, None)
    values: list[float | None] = []
    for bucket in range(1, 6):
        bucket_values = tuple(getter(row) for row in rows if buckets[row.code] == bucket)
        values.append(_mean(bucket_values))
    return cast(QuintileValues, tuple(values))


def _aggregate_cost_quintiles(days: tuple[FactorDailyDiagnostic, ...], cost_rate: float) -> FactorCostQuintiles:
    daily_costs = tuple(next(item for item in day.cost_quintiles if item.cost_rate == cost_rate) for day in days)
    values = cast(
        QuintileValues,
        tuple(_optional_mean(tuple(item.quintile_net_excess[index] for item in daily_costs)) for index in range(5)),
    )
    monotonic_fraction, top_minus_bottom = monotonicity(values)
    return FactorCostQuintiles(cost_rate, values, monotonic_fraction, top_minus_bottom)


def _weighted_quintile_mean(days: tuple[FactorDailyDiagnostic, ...], metric: str) -> QuintileValues:
    values: list[float | None] = []
    for index in range(5):
        numerator = 0.0
        denominator = 0
        for day in days:
            metric_values = day.severe_rate_by_quintile if metric == "severe" else day.mean_mae_atr20_by_quintile
            value = metric_values[index]
            if value is not None:
                numerator += value * day.quintile_counts[index]
                denominator += day.quintile_counts[index]
        values.append(numerator / denominator if denominator else None)
    return cast(QuintileValues, tuple(values))


def _lag_diagnostics(
    rows_by_day: tuple[tuple[date, str, str, tuple[_FactorRow, ...]], ...],
    days: tuple[FactorDailyDiagnostic, ...],
) -> tuple[FactorLagDiagnostic, ...]:
    result: list[FactorLagDiagnostic] = []
    value_maps = tuple(
        {row.code: row.factor_value for row in rows if row.factor_value is not None}
        for _trade_date, _day_hash, _input_hash, rows in rows_by_day
    )
    for lag in _DECAY_LAGS:
        decays: list[float] = []
        turnovers: list[float] = []
        for index in range(lag, len(days)):
            previous = value_maps[index - lag]
            current = value_maps[index]
            codes = tuple(sorted(set(previous).intersection(current)))
            decay = population_spearman(tuple((previous[code], current[code]) for code in codes))
            if decay is not None:
                decays.append(decay)
            turnover = top_bucket_turnover(days[index - lag].top_quintile_codes, days[index].top_quintile_codes)
            if turnover is not None:
                turnovers.append(turnover)
        result.append(
            FactorLagDiagnostic(lag, _mean(tuple(decays)), _mean(tuple(turnovers)), len(decays), len(turnovers))
        )
    return tuple(result)


def _strata(rows: tuple[_FactorRow, ...]) -> tuple[FactorStratumDiagnostic, ...]:
    labels_by_key = _stratum_labels(rows)
    grouped: dict[tuple[str, str], list[_FactorRow]] = defaultdict(list)
    for row in rows:
        key = (row.trade_date, row.code)
        for dimension, label in labels_by_key[key]:
            grouped[(dimension, label)].append(row)
    return tuple(
        _stratum_metric(dimension, label, tuple(grouped[(dimension, label)])) for dimension, label in sorted(grouped)
    )


def _stratum_labels(rows: tuple[_FactorRow, ...]) -> dict[tuple[date, str], tuple[tuple[str, str], ...]]:
    cap_labels: dict[tuple[date, str], str] = {}
    liquidity_labels: dict[tuple[date, str], str] = {}
    by_date: dict[date, list[_FactorRow]] = defaultdict(list)
    for row in rows:
        by_date[row.trade_date].append(row)
    for trade_date, day_rows in by_date.items():
        cap_labels.update(_tertile_labels(trade_date, tuple(day_rows), "market_cap", ("small", "mid", "large")))
        liquidity_labels.update(_tertile_labels(trade_date, tuple(day_rows), "liquidity", ("low", "mid", "high")))
    return {
        (row.trade_date, row.code): (
            ("board", row.board),
            ("industry", row.industry),
            ("market_cap", cap_labels[(row.trade_date, row.code)]),
            ("liquidity", liquidity_labels[(row.trade_date, row.code)]),
        )
        for row in rows
    }


def _tertile_labels(
    trade_date: date,
    rows: tuple[_FactorRow, ...],
    attribute: str,
    labels: tuple[str, str, str],
) -> dict[tuple[date, str], str]:
    known = tuple(
        sorted(
            ((row.code, cast(float, getattr(row, attribute))) for row in rows if getattr(row, attribute) is not None),
            key=lambda item: (item[1], item[0]),
        )
    )
    result = {(trade_date, row.code): "unknown" for row in rows}
    for position, (code, _value) in enumerate(known):
        result[(trade_date, code)] = labels[min(2, position * 3 // len(known))]
    return result


def _stratum_metric(dimension: str, label: str, rows: tuple[_FactorRow, ...]) -> FactorStratumDiagnostic:
    observed = tuple(row for row in rows if row.factor_value is not None)
    by_date: dict[date, list[_FactorRow]] = defaultdict(list)
    for row in observed:
        by_date[row.trade_date].append(row)
    daily_pairs = tuple(
        tuple((cast(float, row.factor_value), row.net_excess(_COST_RATES[0])) for row in day_rows)
        for _trade_date, day_rows in sorted(by_date.items())
    )
    coverage = len(observed) / len(rows) if rows else 0.0
    return FactorStratumDiagnostic(
        cast(StratumDimension, dimension),
        label,
        len(rows),
        len(observed),
        coverage,
        1.0 - coverage,
        mean_rank_ic(tuple(population_pearson(pairs) for pairs in daily_pairs)),
        mean_rank_ic(tuple(population_spearman(pairs) for pairs in daily_pairs)),
        _mean(tuple(row.net_excess(_COST_RATES[0]) for row in observed)),
        _mean(tuple(1.0 if row.mae_atr20 <= -1.5 else 0.0 for row in observed)),
        _mean(tuple(row.mae_atr20 for row in observed)),
    )


def _oracle_recall(extraction: ScoreR2HistoricalExtraction, baseline: ScoreR3BaselineReport) -> OracleRecallDiagnostic:
    baseline_by_date = {item.trade_date: item for item in baseline.days}
    days: list[OracleRecallDay] = []
    for day in extraction.days:
        baseline_day = baseline_by_date[day.summary.trade_date]
        summary_by_code = {item.code: item for item in day.summary.candidates}
        oracle = baseline_day.oracle_codes
        pre = sum(1 for code in oracle if code in summary_by_code)
        post = sum(1 for code in oracle if code in summary_by_code and summary_by_code[code].production_top120)
        if post != baseline_day.recalled_oracle_count:
            raise ValueError("factor diagnostic post-pruning recall does not match R3")
        days.append(
            OracleRecallDay(
                day.summary.trade_date,
                len(oracle),
                pre,
                post,
                pre / len(oracle) if oracle else None,
                post / len(oracle) if oracle else None,
            )
        )
    oracle_count = sum(item.oracle_count for item in days)
    pre = sum(item.pre_pruning_recalled for item in days)
    post = sum(item.post_pruning_recalled for item in days)
    return OracleRecallDiagnostic(
        oracle_count,
        pre,
        post,
        pre / oracle_count if oracle_count else None,
        post / oracle_count if oracle_count else None,
        tuple(days),
    )


def _mean(values: tuple[float, ...]) -> float | None:
    return math.fsum(values) / len(values) if values else None


def _optional_mean(values: tuple[float | None, ...]) -> float | None:
    return _mean(tuple(value for value in values if value is not None))


__all__ = ["ScoreNativeFactorDiagnostics"]
