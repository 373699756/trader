"""Offline historical-only Score-R6 selection and validation."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal, Protocol

from trader.application.research.historical_screening import (
    HistoricalArchiveManifest,
    HistoricalArchiveStatus,
)
from trader.application.research.replay_models import canonical_hash
from trader.application.research.score_r6_models import (
    ScoreR6BoardCandidate,
    ScoreR6FrozenCandidate,
    ScoreR6HistoricalReport,
    ScoreR6HistoricalRow,
    ScoreR6Metrics,
)
from trader.domain.research.historical_screening import SCORE_H0_V1_SPEC, HistoricalScreeningSpec
from trader.domain.research.score_r6 import (
    ScoreR6Candidate,
    ScoreR6HistoricalSpec,
    ScoreR6ProductionCandidate,
    iter_score_r6_candidates,
    materialize_score_r6_production_candidate,
)

_BOARDS = ("main", "chinext", "star")


class ScoreR6HistoricalEvidence(Protocol):
    def inspect(self, research_identity: str) -> HistoricalArchiveStatus: ...

    def manifest(self, spec: HistoricalScreeningSpec) -> HistoricalArchiveManifest: ...

    def score_r6_rows(self, spec: HistoricalScreeningSpec) -> Sequence[ScoreR6HistoricalRow]: ...


@dataclass(frozen=True)
class _BoardFitContext:
    training: tuple[ScoreR6HistoricalRow, ...]
    validation: tuple[ScoreR6HistoricalRow, ...]
    candidates: tuple[ScoreR6Candidate, ...]
    global_candidate: ScoreR6FrozenCandidate
    spec: ScoreR6HistoricalSpec
    minimum_days: int
    minimum_selected_days: int


class ScoreR6HistoricalScreeningService:
    def __init__(
        self,
        evidence: ScoreR6HistoricalEvidence,
        *,
        minimum_split_days: int | None = None,
        minimum_board_rows: int | None = None,
    ) -> None:
        self._evidence = evidence
        self._minimum_split_days = minimum_split_days
        self._minimum_board_rows = minimum_board_rows

    def execute(self, spec: ScoreR6HistoricalSpec) -> ScoreR6HistoricalReport:
        archive = self._evidence.inspect(spec.parent_research_identity)
        manifest = self._evidence.manifest(SCORE_H0_V1_SPEC)
        coverage = archive.completed_codes / archive.universe_count if archive.universe_count else 0.0
        if archive.spec_hash != spec.parent_research_spec_hash or coverage < spec.minimum_archive_coverage:
            return _rejected_report(
                spec,
                archive,
                manifest,
                "score_h0_archive_coverage_incomplete",
            )
        rows = tuple(sorted(self._evidence.score_r6_rows(SCORE_H0_V1_SPEC)))
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
            return _rejected_report(
                spec,
                archive,
                manifest,
                "score_r6_split_days_incomplete",
            )
        minimum_selected_days = min(spec.minimum_selected_days, minimum_days)
        candidates = iter_score_r6_candidates(spec)
        selected_candidate, training_metrics = _select_candidate(
            candidates,
            training,
            spec,
            minimum_selected_days=minimum_selected_days,
        )
        if selected_candidate is None or training_metrics is None:
            return _rejected_report(
                spec,
                archive,
                manifest,
                "score_r6_no_candidate_met_training_coverage",
            )
        validation_metrics = _evaluate(selected_candidate, validation, spec, regularize=False)
        baseline = ScoreR6Candidate(spec.current_weight_units, 78, 4)
        baseline_validation = _evaluate(baseline, validation, spec, regularize=False)
        failures = _validation_failures(validation_metrics, baseline_validation, minimum_selected_days, spec)
        frozen = ScoreR6FrozenCandidate(
            selected_candidate,
            materialize_score_r6_production_candidate(selected_candidate, spec),
            training_metrics.content_hash,
            validation_metrics.content_hash,
            training_metrics.objective_value if training_metrics.objective_value is not None else -math.inf,
        )
        fit_context = _BoardFitContext(
            training,
            validation,
            candidates,
            frozen,
            spec,
            minimum_days,
            minimum_selected_days,
        )
        board_candidates = tuple(self._fit_board(board, fit_context) for board in _BOARDS)
        validated_candidate = ScoreR6ProductionCandidate(
            canonical_hash(tuple(item.candidate_hash for item in board_candidates)),
            tuple(item.production_weights for item in board_candidates),
            selected_candidate.action_threshold,
            selected_candidate.risk_penalty,
        )
        return ScoreR6HistoricalReport(
            status="historical_screened",
            research_identity=spec.research_identity,
            research_spec_hash=spec.content_hash,
            parent_archive=archive,
            parent_manifest=manifest,
            global_candidate=frozen,
            validated_candidate=validated_candidate,
            training=training_metrics,
            validation=validation_metrics,
            baseline_validation=baseline_validation,
            board_candidates=board_candidates,
            historical_gate_passed=not failures,
            failure_reasons=failures,
            validation_mode="historical_only",
            promotion_authority=False,
            limitations=_LIMITATIONS,
        )

    def _fit_board(
        self,
        board: str,
        context: _BoardFitContext,
    ) -> ScoreR6BoardCandidate:
        board_training = tuple(row for row in context.training if row.board == board)
        board_validation = tuple(row for row in context.validation if row.board == board)
        sample_rows = len(board_training)
        sample_days = _date_count(board_training)
        minimum_rows = self._minimum_board_rows or context.spec.minimum_board_rows
        can_fit = sample_rows >= minimum_rows and sample_days >= min(
            context.spec.minimum_board_days, context.minimum_days
        )
        selected: ScoreR6Candidate | None = None
        training_metrics: ScoreR6Metrics | None = None
        if can_fit:
            board_grid = tuple(
                candidate
                for candidate in context.candidates
                if candidate.action_threshold == context.global_candidate.candidate.action_threshold
                and candidate.risk_penalty == context.global_candidate.candidate.risk_penalty
            )
            selected, training_metrics = _select_candidate(
                board_grid,
                board_training,
                context.spec,
                minimum_selected_days=context.minimum_selected_days,
            )
        source: Literal["board_fit", "global_fallback"] = (
            "board_fit" if selected is not None and training_metrics is not None else "global_fallback"
        )
        if selected is None or training_metrics is None:
            selected = context.global_candidate.candidate
            training_metrics = _evaluate(selected, board_training, context.spec, regularize=True)
        validation_metrics = _evaluate(selected, board_validation, context.spec, regularize=False)
        production = materialize_score_r6_production_candidate(selected, context.spec)
        production_weights = next(item for item in production.boards if item.board == board)
        return ScoreR6BoardCandidate(
            board=board,  # type: ignore[arg-type]
            source=source,
            sample_rows=sample_rows,
            sample_days=sample_days,
            candidate_hash=selected.content_hash,
            candidate=selected,
            production_weights=production_weights,
            training_metrics_hash=training_metrics.content_hash,
            validation_metrics_hash=validation_metrics.content_hash,
        )


def _select_candidate(
    candidates: tuple[ScoreR6Candidate, ...],
    rows: tuple[ScoreR6HistoricalRow, ...],
    spec: ScoreR6HistoricalSpec,
    *,
    minimum_selected_days: int,
) -> tuple[ScoreR6Candidate | None, ScoreR6Metrics | None]:
    evaluated = tuple((candidate, _evaluate(candidate, rows, spec, regularize=True)) for candidate in candidates)
    eligible = tuple(item for item in evaluated if item[1].selected_days >= minimum_selected_days)
    if not eligible:
        return None, None
    return min(eligible, key=lambda item: _candidate_order(item[0], item[1], spec))


def _candidate_order(
    candidate: ScoreR6Candidate,
    metrics: ScoreR6Metrics,
    spec: ScoreR6HistoricalSpec,
) -> tuple[float, int, int, int, str]:
    objective = metrics.objective_value if metrics.objective_value is not None else -math.inf
    offset = sum(
        abs(value - current) for value, current in zip(candidate.weight_units, spec.current_weight_units, strict=True)
    )
    return (
        -objective,
        offset,
        abs(candidate.action_threshold - 78),
        abs(candidate.risk_penalty - 4),
        candidate.content_hash,
    )


def _evaluate(
    candidate: ScoreR6Candidate,
    rows: tuple[ScoreR6HistoricalRow, ...],
    spec: ScoreR6HistoricalSpec,
    *,
    regularize: bool,
) -> ScoreR6Metrics:
    grouped: dict[date, list[ScoreR6HistoricalRow]] = defaultdict(list)
    for row in rows:
        grouped[row.trade_date].append(row)
    daily_excess: list[float] = []
    severe_count = selected_count = recalled = oracle_count = 0
    turnovers: list[float] = []
    prior_codes: frozenset[str] | None = None
    positive_by_code: dict[str, float] = defaultdict(float)
    maximum_board_fraction = 0.0
    weights = candidate.weights
    for trade_date in sorted(grouped):
        population = tuple(sorted(grouped[trade_date], key=lambda item: item.code))
        if len(population) < 30:
            continue
        benchmark = _mean(tuple(row.return_5d_pct for row in population))
        scored = tuple(
            (
                math.fsum(
                    (
                        row.momentum_score * weights[0],
                        row.stability_score * weights[1],
                        row.liquidity_score * weights[2],
                    )
                )
                - (candidate.risk_penalty if row.volatility_20d_pct >= spec.high_volatility_threshold_pct else 0.0),
                row,
            )
            for row in population
        )
        selected = tuple(row for score, row in scored if score >= candidate.action_threshold)
        if not selected:
            continue
        selected_codes = frozenset(row.code for row in selected)
        if prior_codes is not None:
            turnovers.append(
                1.0 - len(selected_codes.intersection(prior_codes)) / max(len(selected_codes), len(prior_codes))
            )
        prior_codes = selected_codes
        selected_return = _mean(tuple(row.return_5d_pct for row in selected)) - spec.round_trip_cost_bps / 100.0
        excess = selected_return - benchmark
        daily_excess.append(excess)
        selected_count += len(selected)
        severe_count += sum(row.return_5d_pct <= spec.severe_loss_threshold_pct for row in selected)
        oracle_size = max(3, math.ceil(len(population) * 0.10))
        oracle = {
            row.code for row in sorted(population, key=lambda item: (-item.return_5d_pct, item.code))[:oracle_size]
        }
        recalled += len(selected_codes.intersection(oracle))
        oracle_count += len(oracle)
        board_counts: dict[str, int] = defaultdict(int)
        for row in selected:
            board_counts[row.board] += 1
            contribution = row.return_5d_pct - benchmark - spec.round_trip_cost_bps / 100.0
            if contribution > 0.0:
                positive_by_code[row.code] += contribution / len(selected)
        maximum_board_fraction = max(maximum_board_fraction, max(board_counts.values()) / len(selected))
    if not daily_excess:
        return _empty_metrics(trade_dates=len(grouped))
    net_excess = _mean(tuple(daily_excess))
    severe = severe_count / selected_count
    turnover = _mean(tuple(turnovers)) if turnovers else 0.0
    stability = _stddev(tuple(daily_excess))
    recall = recalled / oracle_count if oracle_count else 0.0
    total_positive = math.fsum(positive_by_code.values())
    stock_concentration = max(positive_by_code.values(), default=0.0) / total_positive if total_positive > 0.0 else 0.0
    offset = (
        sum(
            abs(value - current)
            for value, current in zip(candidate.weight_units, spec.current_weight_units, strict=True)
        )
        / 10_000
    )
    objective = (
        net_excess
        - spec.objective_severe_coefficient * severe
        - spec.objective_turnover_coefficient * turnover
        - spec.objective_stability_coefficient * stability
        + spec.objective_recall_coefficient * recall
    )
    if regularize:
        objective -= spec.regularization_strength * offset
    return ScoreR6Metrics(
        trade_dates=len(grouped),
        selected_days=len(daily_excess),
        pair_count=selected_count,
        mean_net_excess_5d_pct=net_excess,
        severe_loss_rate=severe,
        mean_turnover=turnover,
        daily_net_excess_stddev=stability,
        oracle_recall=recall,
        maximum_stock_positive_contribution_fraction=stock_concentration,
        maximum_board_fraction=maximum_board_fraction,
        objective_value=objective,
    )


def _validation_failures(
    candidate: ScoreR6Metrics,
    baseline: ScoreR6Metrics,
    minimum_selected_days: int,
    spec: ScoreR6HistoricalSpec,
) -> tuple[str, ...]:
    failures: list[str] = []
    if candidate.selected_days < minimum_selected_days:
        failures.append("validation_selected_days_incomplete")
    comparisons = (
        (candidate.mean_net_excess_5d_pct, baseline.mean_net_excess_5d_pct, 0.0, "validation_net_excess_failed", False),
        (
            candidate.severe_loss_rate,
            baseline.severe_loss_rate,
            spec.validation_severe_tolerance,
            "validation_severe_loss_failed",
            True,
        ),
        (
            candidate.mean_turnover,
            baseline.mean_turnover,
            spec.validation_turnover_tolerance,
            "validation_turnover_failed",
            True,
        ),
        (
            candidate.daily_net_excess_stddev,
            baseline.daily_net_excess_stddev,
            spec.validation_stability_tolerance,
            "validation_stability_failed",
            True,
        ),
        (
            candidate.oracle_recall,
            baseline.oracle_recall,
            spec.validation_recall_tolerance,
            "validation_recall_failed",
            False,
        ),
        (
            candidate.maximum_stock_positive_contribution_fraction,
            baseline.maximum_stock_positive_contribution_fraction,
            spec.validation_stock_concentration_tolerance,
            "validation_stock_concentration_failed",
            True,
        ),
        (
            candidate.maximum_board_fraction,
            baseline.maximum_board_fraction,
            spec.validation_board_concentration_tolerance,
            "validation_board_concentration_failed",
            True,
        ),
    )
    for actual, control, tolerance, reason, lower_is_better in comparisons:
        if actual is None or control is None:
            failures.append(reason)
        elif lower_is_better and actual > control + tolerance:
            failures.append(reason)
        elif not lower_is_better and actual < control - tolerance:
            failures.append(reason)
    return tuple(sorted(set(failures)))


def _rejected_report(
    spec: ScoreR6HistoricalSpec,
    archive: HistoricalArchiveStatus,
    manifest: HistoricalArchiveManifest,
    reason: str,
) -> ScoreR6HistoricalReport:
    status: Literal["insufficient_coverage", "historical_rejected"] = (
        "insufficient_coverage"
        if reason in {"score_h0_archive_coverage_incomplete", "score_r6_split_days_incomplete"}
        else "historical_rejected"
    )
    return ScoreR6HistoricalReport(
        status=status,
        research_identity=spec.research_identity,
        research_spec_hash=spec.content_hash,
        parent_archive=archive,
        parent_manifest=manifest,
        global_candidate=None,
        validated_candidate=None,
        training=_empty_metrics(),
        validation=_empty_metrics(),
        baseline_validation=_empty_metrics(),
        board_candidates=(),
        historical_gate_passed=False,
        failure_reasons=(reason,),
        validation_mode="historical_only",
        promotion_authority=False,
        limitations=_LIMITATIONS,
    )


def _date_count(rows: tuple[ScoreR6HistoricalRow, ...]) -> int:
    return len({row.trade_date for row in rows})


def _empty_metrics(*, trade_dates: int = 0) -> ScoreR6Metrics:
    return ScoreR6Metrics(trade_dates, 0, 0, None, None, None, None, None, None, None, None)


def _mean(values: tuple[float, ...]) -> float:
    return math.fsum(values) / len(values) if values else 0.0


def _stddev(values: tuple[float, ...]) -> float:
    if len(values) < 2:
        return 0.0
    mean = _mean(values)
    return math.sqrt(math.fsum((value - mean) ** 2 for value in values) / (len(values) - 1))


_LIMITATIONS = (
    "current_universe_survivorship_bias",
    "historical_st_status_not_reconstructed",
    "historical_industry_not_reconstructed",
    "intraday_tail_not_reconstructed",
    "corporate_risk_not_reconstructed",
    "deepseek_facts_not_reconstructed",
    "only_high_volatility_risk_reconstructed",
)


__all__ = [
    "ScoreR6HistoricalEvidence",
    "ScoreR6HistoricalScreeningService",
]
