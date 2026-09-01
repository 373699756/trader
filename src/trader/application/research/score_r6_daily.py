"""Offline risk-adjusted daily trend selection with a single held-out evaluation."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from datetime import date
from typing import Literal, Protocol

from trader.application.research.historical_screening import HistoricalArchiveManifest, HistoricalArchiveStatus
from trader.application.research.score_r6_daily_models import ScoreR6DailyReport, ScoreR6DailyRow
from trader.application.research.score_r6_models import ScoreR6Metrics
from trader.domain.research.historical_screening import SCORE_H0_V1_SPEC, HistoricalScreeningSpec
from trader.domain.research.score_r6_daily import (
    ScoreR6DailyCandidate,
    ScoreR6DailySpec,
    iter_score_r6_daily_candidates,
)


class ScoreR6DailyEvidence(Protocol):
    def inspect(self, research_identity: str) -> HistoricalArchiveStatus: ...

    def manifest(self, spec: HistoricalScreeningSpec) -> HistoricalArchiveManifest: ...

    def score_r6_daily_rows(self, spec: HistoricalScreeningSpec) -> Sequence[ScoreR6DailyRow]: ...


class ScoreR6DailyScreeningService:
    def __init__(self, evidence: ScoreR6DailyEvidence, *, minimum_split_days: int | None = None) -> None:
        self._evidence = evidence
        self._minimum_split_days = minimum_split_days

    def execute(self, spec: ScoreR6DailySpec) -> ScoreR6DailyReport:
        archive = self._evidence.inspect(spec.parent_research_identity)
        manifest = self._evidence.manifest(SCORE_H0_V1_SPEC)
        coverage = archive.completed_codes / archive.universe_count if archive.universe_count else 0.0
        if archive.spec_hash != spec.parent_research_spec_hash or coverage < spec.minimum_archive_coverage:
            return _report(
                spec,
                archive,
                manifest,
                ("insufficient_coverage", ("score_h0_archive_coverage_incomplete",)),
            )
        rows = tuple(sorted(self._evidence.score_r6_daily_rows(SCORE_H0_V1_SPEC)))
        training = tuple(
            row for row in rows if SCORE_H0_V1_SPEC.training_start <= row.trade_date <= SCORE_H0_V1_SPEC.training_end
        )
        validation = tuple(
            row
            for row in rows
            if SCORE_H0_V1_SPEC.validation_start <= row.trade_date <= SCORE_H0_V1_SPEC.validation_end
        )
        minimum_days = self._minimum_split_days or spec.minimum_split_days
        if _date_count(training) < minimum_days or _date_count(validation) < minimum_days:
            return _report(spec, archive, manifest, ("insufficient_coverage", ("daily_trend_split_days_incomplete",)))
        baseline_training = _evaluate_baseline(training, spec)
        baseline_validation = _evaluate_baseline(validation, spec)
        eligible: list[tuple[ScoreR6DailyCandidate, ScoreR6Metrics]] = []
        for candidate in iter_score_r6_daily_candidates(spec):
            metrics = _evaluate_candidate(candidate, training, spec)
            if _training_eligible(metrics, baseline_training, spec, minimum_days):
                eligible.append((candidate, metrics))
        if not eligible:
            return _report(
                spec,
                archive,
                manifest,
                ("historical_rejected", ("daily_trend_no_training_candidate",)),
                (baseline_training, baseline_validation),
            )
        candidate, training_metrics = min(eligible, key=_candidate_order)
        validation_metrics = _evaluate_candidate(candidate, validation, spec)
        failures = _validation_failures(validation_metrics, baseline_validation, spec, minimum_days)
        passed = not failures
        return ScoreR6DailyReport(
            status="historical_validated" if passed else "historical_rejected",
            research_identity=spec.research_identity,
            research_spec_hash=spec.content_hash,
            archive=archive,
            archive_manifest=manifest,
            selected_candidate=candidate,
            training=training_metrics,
            validation=validation_metrics,
            baseline_training=baseline_training,
            baseline_validation=baseline_validation,
            historical_gate_passed=passed,
            failure_reasons=failures,
            limitations=_LIMITATIONS,
            promotion_authority=False,
            schema_version=spec.report_schema_version,
        )


def _evaluate_candidate(
    candidate: ScoreR6DailyCandidate,
    rows: tuple[ScoreR6DailyRow, ...],
    spec: ScoreR6DailySpec,
) -> ScoreR6Metrics:
    def select(
        population: tuple[ScoreR6DailyRow, ...], _prior_codes: frozenset[str] | None
    ) -> tuple[ScoreR6DailyRow, ...]:
        scored = tuple(
            (value, row) for row in population if (value := score_r6_daily_candidate_row(candidate, row)) is not None
        )
        return select_score_r6_daily_top(scored, spec.selection_limit, spec.maximum_per_board)

    return evaluate_score_r6_daily_selections(rows, spec, select)


def _evaluate_baseline(rows: tuple[ScoreR6DailyRow, ...], spec: ScoreR6DailySpec) -> ScoreR6Metrics:
    def select(
        population: tuple[ScoreR6DailyRow, ...], _prior_codes: frozenset[str] | None
    ) -> tuple[ScoreR6DailyRow, ...]:
        scored = tuple((value, row) for row in population if (value := score_r6_daily_proxy_row(row)) is not None)
        return select_score_r6_daily_top(scored, spec.selection_limit, spec.maximum_per_board)

    return evaluate_score_r6_daily_selections(rows, spec, select)


def evaluate_score_r6_daily_selections(
    rows: tuple[ScoreR6DailyRow, ...],
    spec: ScoreR6DailySpec,
    select_day: Callable[[tuple[ScoreR6DailyRow, ...], frozenset[str] | None], tuple[ScoreR6DailyRow, ...]],
) -> ScoreR6Metrics:
    grouped: dict[date, list[ScoreR6DailyRow]] = defaultdict(list)
    for row in rows:
        grouped[row.trade_date].append(row)
    daily_excess: list[float] = []
    turnovers: list[float] = []
    severe_count = selected_count = recalled = oracle_count = 0
    prior_codes: frozenset[str] | None = None
    positive_by_code: dict[str, float] = defaultdict(float)
    maximum_board_fraction = 0.0
    for trade_date in sorted(grouped):
        population = tuple(sorted(grouped[trade_date], key=lambda item: item.code))
        if len(population) < 30:
            continue
        selected = select_day(population, prior_codes)
        if not selected:
            continue
        benchmark = _mean(tuple(row.return_5d_pct for row in population))
        selected_codes = frozenset(row.code for row in selected)
        if prior_codes is not None:
            turnovers.append(1.0 - len(selected_codes & prior_codes) / max(len(selected_codes), len(prior_codes)))
        prior_codes = selected_codes
        selected_return = _mean(tuple(row.return_5d_pct for row in selected)) - spec.round_trip_cost_bps / 100.0
        excess = selected_return - benchmark
        daily_excess.append(excess)
        selected_count += len(selected)
        severe_count += sum(row.return_5d_pct <= spec.severe_loss_threshold_pct for row in selected)
        oracle = {row.code for row in sorted(population, key=lambda item: (-item.return_5d_pct, item.code))[:6]}
        recalled += len(selected_codes & oracle)
        oracle_count += len(oracle)
        board_counts = Counter(row.board for row in selected)
        maximum_board_fraction = max(maximum_board_fraction, max(board_counts.values()) / len(selected))
        for row in selected:
            contribution = row.return_5d_pct - benchmark - spec.round_trip_cost_bps / 100.0
            if contribution > 0.0:
                positive_by_code[row.code] += contribution / len(selected)
    if not daily_excess:
        return _empty_metrics(len(grouped))
    severe = severe_count / selected_count
    turnover = _mean(tuple(turnovers)) if turnovers else 0.0
    stability = _stddev(tuple(daily_excess))
    recall = recalled / oracle_count if oracle_count else 0.0
    positive_total = math.fsum(positive_by_code.values())
    concentration = max(positive_by_code.values(), default=0.0) / positive_total if positive_total > 0.0 else 0.0
    net_excess = _mean(tuple(daily_excess))
    objective = (
        net_excess
        - spec.objective_severe_coefficient * severe
        - spec.objective_turnover_coefficient * turnover
        - spec.objective_stability_coefficient * stability
        + spec.objective_recall_coefficient * recall
    )
    return ScoreR6Metrics(
        trade_dates=len(grouped),
        selected_days=len(daily_excess),
        pair_count=selected_count,
        mean_net_excess_5d_pct=net_excess,
        severe_loss_rate=severe,
        mean_turnover=turnover,
        daily_net_excess_stddev=stability,
        oracle_recall=recall,
        maximum_stock_positive_contribution_fraction=concentration,
        maximum_board_fraction=maximum_board_fraction,
        objective_value=objective,
    )


def raw_score_r6_daily_candidate_row(candidate: ScoreR6DailyCandidate, row: ScoreR6DailyRow) -> float | None:
    if (
        row.residual_return_60_5_pct <= 0.0
        or row.close_ma20_spread_pct <= 0.0
        or row.recent_return_5d_pct > candidate.recent_return_cap_pct
        or row.drawdown_60d_pct < candidate.drawdown_floor_pct
    ):
        return None
    return math.fsum(
        component * weight
        for component, weight in zip(
            (
                row.residual_momentum_score,
                row.trend_efficiency_score,
                row.downside_stability_score,
                row.drawdown_recovery_score,
                row.liquidity_score,
            ),
            candidate.weights,
            strict=True,
        )
    )


def score_r6_daily_candidate_row(candidate: ScoreR6DailyCandidate, row: ScoreR6DailyRow) -> float | None:
    value = raw_score_r6_daily_candidate_row(candidate, row)
    return value if value is not None and value >= candidate.action_threshold else None


def score_r6_daily_proxy_row(row: ScoreR6DailyRow) -> float | None:
    value = (
        row.momentum_20_score * 0.50
        + row.downside_stability_score * 0.30
        + row.liquidity_score * 0.20
        - (4.0 if row.volatility_20d_pct >= 4.0 else 0.0)
    )
    return value if value >= 78.0 else None


def select_score_r6_daily_top(
    scored: tuple[tuple[float, ScoreR6DailyRow], ...], limit: int, maximum_per_board: int
) -> tuple[ScoreR6DailyRow, ...]:
    selected: list[ScoreR6DailyRow] = []
    board_counts: Counter[str] = Counter()
    for _score, row in sorted(scored, key=lambda item: (-item[0], item[1].code)):
        if board_counts[row.board] >= maximum_per_board:
            continue
        selected.append(row)
        board_counts[row.board] += 1
        if len(selected) == limit:
            break
    return tuple(selected)


def _training_eligible(
    candidate: ScoreR6Metrics,
    baseline: ScoreR6Metrics,
    spec: ScoreR6DailySpec,
    minimum_days: int,
) -> bool:
    required_days = min(spec.minimum_selected_days, minimum_days)
    return bool(
        candidate.selected_days >= required_days
        and candidate.mean_net_excess_5d_pct is not None
        and baseline.mean_net_excess_5d_pct is not None
        and candidate.mean_net_excess_5d_pct >= baseline.mean_net_excess_5d_pct
        and candidate.severe_loss_rate is not None
        and baseline.severe_loss_rate is not None
        and candidate.severe_loss_rate <= baseline.severe_loss_rate
    )


def _candidate_order(item: tuple[ScoreR6DailyCandidate, ScoreR6Metrics]) -> tuple[float, float, int, str]:
    candidate, metrics = item
    return (
        -(metrics.objective_value if metrics.objective_value is not None else -math.inf),
        metrics.severe_loss_rate if metrics.severe_loss_rate is not None else math.inf,
        abs(candidate.action_threshold - 75),
        candidate.content_hash,
    )


def _validation_failures(
    candidate: ScoreR6Metrics,
    baseline: ScoreR6Metrics,
    spec: ScoreR6DailySpec,
    minimum_days: int,
) -> tuple[str, ...]:
    failures: list[str] = []
    if candidate.selected_days < min(spec.minimum_selected_days, minimum_days):
        failures.append("daily_trend_validation_days_incomplete")
    comparisons = (
        (
            candidate.mean_net_excess_5d_pct,
            baseline.mean_net_excess_5d_pct,
            spec.minimum_validation_gain_pct,
            False,
            "daily_trend_validation_gain_failed",
        ),
        (candidate.severe_loss_rate, baseline.severe_loss_rate, 0.0, True, "daily_trend_validation_severe_failed"),
        (
            candidate.mean_turnover,
            baseline.mean_turnover,
            spec.validation_turnover_tolerance,
            True,
            "daily_trend_validation_turnover_failed",
        ),
        (
            candidate.daily_net_excess_stddev,
            baseline.daily_net_excess_stddev,
            spec.validation_stability_tolerance,
            True,
            "daily_trend_validation_stability_failed",
        ),
        (
            candidate.oracle_recall,
            baseline.oracle_recall,
            spec.validation_recall_tolerance,
            False,
            "daily_trend_validation_recall_failed",
        ),
        (
            candidate.maximum_stock_positive_contribution_fraction,
            baseline.maximum_stock_positive_contribution_fraction,
            spec.validation_stock_concentration_tolerance,
            True,
            "daily_trend_validation_concentration_failed",
        ),
    )
    for actual, control, tolerance, lower_is_better, reason in comparisons:
        if actual is None or control is None:
            failures.append(reason)
        elif lower_is_better and actual > control + tolerance:
            failures.append(reason)
        elif not lower_is_better and actual < control + tolerance:
            failures.append(reason)
    if (
        candidate.maximum_board_fraction is None
        or candidate.maximum_board_fraction > spec.maximum_per_board / spec.selection_limit
    ):
        failures.append("daily_trend_validation_board_fraction_failed")
    return tuple(sorted(set(failures)))


def _report(
    spec: ScoreR6DailySpec,
    archive: HistoricalArchiveStatus,
    manifest: HistoricalArchiveManifest,
    failure: tuple[Literal["insufficient_coverage", "historical_rejected"], tuple[str, ...]],
    baselines: tuple[ScoreR6Metrics, ScoreR6Metrics] | None = None,
) -> ScoreR6DailyReport:
    empty = _empty_metrics(0)
    status, reasons = failure
    baseline_training, baseline_validation = baselines or (empty, empty)
    return ScoreR6DailyReport(
        status=status,
        research_identity=spec.research_identity,
        research_spec_hash=spec.content_hash,
        archive=archive,
        archive_manifest=manifest,
        selected_candidate=None,
        training=empty,
        validation=empty,
        baseline_training=baseline_training,
        baseline_validation=baseline_validation,
        historical_gate_passed=False,
        failure_reasons=reasons,
        limitations=_LIMITATIONS,
        promotion_authority=False,
        schema_version=spec.report_schema_version,
    )


def _empty_metrics(trade_dates: int) -> ScoreR6Metrics:
    return ScoreR6Metrics(trade_dates, 0, 0, None, None, None, None, None, None, None, None)


def _date_count(rows: tuple[ScoreR6DailyRow, ...]) -> int:
    return len({row.trade_date for row in rows})


def _mean(values: tuple[float, ...]) -> float:
    return math.fsum(values) / len(values) if values else 0.0


def _stddev(values: tuple[float, ...]) -> float:
    if len(values) < 2:
        return 0.0
    mean = _mean(values)
    return math.sqrt(math.fsum((value - mean) ** 2 for value in values) / len(values))


_LIMITATIONS = (
    "current_universe_survivorship_bias",
    "historical_st_status_not_reconstructed",
    "historical_industry_not_reconstructed",
    "intraday_tail_not_reconstructed",
    "corporate_risk_not_reconstructed",
    "deepseek_facts_not_reconstructed",
)

__all__ = [
    "ScoreR6DailyEvidence",
    "ScoreR6DailyScreeningService",
    "evaluate_score_r6_daily_selections",
    "raw_score_r6_daily_candidate_row",
    "score_r6_daily_candidate_row",
    "score_r6_daily_proxy_row",
    "select_score_r6_daily_top",
]
