"""Offline daily ranking stability selection over the bound R6D candidate."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Literal, Protocol

from trader.application.research.historical_screening import HistoricalArchiveManifest, HistoricalArchiveStatus
from trader.application.research.score_r6_daily import (
    ScoreR6DailyEvidence,
    evaluate_score_r6_daily_selections,
    raw_score_r6_daily_candidate_row,
    score_r6_daily_candidate_row,
    score_r6_daily_proxy_row,
    select_score_r6_daily_top,
)
from trader.application.research.score_r6_daily_models import ScoreR6DailyRow
from trader.application.research.score_r6_models import ScoreR6Metrics
from trader.application.research.score_r6_stability_models import ScoreR6StabilityReport
from trader.domain.research.historical_screening import SCORE_H0_V1_SPEC
from trader.domain.research.score_r6_daily import SCORE_R6_DAILY_SPEC
from trader.domain.research.score_r6_stability import (
    ScoreR6StabilityCandidate,
    ScoreR6StabilitySpec,
    iter_score_r6_stability_candidates,
)


class ScoreR6DailyParentArtifact(Protocol):
    def inspect(self) -> dict[str, object]: ...


@dataclass(frozen=True)
class _ReportOutcome:
    status: Literal["insufficient_coverage", "parent_mismatch", "historical_rejected", "diagnostic_passed"]
    selected_candidate: ScoreR6StabilityCandidate | None
    training: ScoreR6Metrics
    diagnostic: ScoreR6Metrics
    parent_training: ScoreR6Metrics
    parent_diagnostic: ScoreR6Metrics
    proxy_diagnostic: ScoreR6Metrics
    failure_reasons: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return self.status == "diagnostic_passed"


class ScoreR6StabilityScreeningService:
    def __init__(
        self,
        evidence: ScoreR6DailyEvidence,
        parent: ScoreR6DailyParentArtifact,
        *,
        minimum_split_days: int | None = None,
    ) -> None:
        self._evidence = evidence
        self._parent = parent
        self._minimum_split_days = minimum_split_days

    def execute(self, spec: ScoreR6StabilitySpec) -> ScoreR6StabilityReport:
        archive = self._evidence.inspect(SCORE_H0_V1_SPEC.research_identity)
        manifest = self._evidence.manifest(SCORE_H0_V1_SPEC)
        try:
            parent_payload = self._parent.inspect()
        except (OSError, RuntimeError, ValueError):
            parent_payload = {}
        if not _parent_matches(parent_payload, spec):
            return _empty_report(spec, archive, manifest, "parent_mismatch", "score_r6_daily_parent_artifact_mismatch")
        coverage = archive.completed_codes / archive.universe_count if archive.universe_count else 0.0
        if archive.spec_hash != SCORE_H0_V1_SPEC.content_hash or coverage < spec.minimum_archive_coverage:
            return _empty_report(
                spec, archive, manifest, "insufficient_coverage", "score_h0_archive_coverage_incomplete"
            )
        rows = tuple(sorted(self._evidence.score_r6_daily_rows(SCORE_H0_V1_SPEC)))
        training = tuple(
            row for row in rows if SCORE_H0_V1_SPEC.training_start <= row.trade_date <= SCORE_H0_V1_SPEC.training_end
        )
        diagnostic = tuple(
            row
            for row in rows
            if SCORE_H0_V1_SPEC.validation_start <= row.trade_date <= SCORE_H0_V1_SPEC.validation_end
        )
        minimum_days = self._minimum_split_days or spec.minimum_split_days
        if _date_count(training) < minimum_days or _date_count(diagnostic) < minimum_days:
            return _empty_report(
                spec, archive, manifest, "insufficient_coverage", "daily_stability_split_days_incomplete"
            )
        parent_training = _evaluate_parent(training, spec)
        parent_diagnostic = _evaluate_parent(diagnostic, spec)
        proxy_diagnostic = _evaluate_proxy(diagnostic)
        eligible: list[tuple[ScoreR6StabilityCandidate, ScoreR6Metrics]] = []
        for candidate in iter_score_r6_stability_candidates(spec):
            metrics = _evaluate_stability(candidate, training, spec)
            if _training_eligible(metrics, parent_training, spec, minimum_days):
                eligible.append((candidate, metrics))
        if not eligible:
            return _report(
                spec,
                archive,
                manifest,
                _ReportOutcome(
                    "historical_rejected",
                    None,
                    _empty_metrics(0),
                    _empty_metrics(0),
                    parent_training,
                    parent_diagnostic,
                    proxy_diagnostic,
                    ("daily_stability_no_training_candidate",),
                ),
            )
        selected, training_metrics = min(eligible, key=_candidate_order)
        diagnostic_metrics = _evaluate_stability(selected, diagnostic, spec)
        failures = _diagnostic_failures(diagnostic_metrics, parent_diagnostic, proxy_diagnostic, spec, minimum_days)
        return _report(
            spec,
            archive,
            manifest,
            _ReportOutcome(
                "historical_rejected" if failures else "diagnostic_passed",
                selected,
                training_metrics,
                diagnostic_metrics,
                parent_training,
                parent_diagnostic,
                proxy_diagnostic,
                failures,
            ),
        )


def _parent_matches(payload: dict[str, object], spec: ScoreR6StabilitySpec) -> bool:
    return bool(
        payload.get("report_hash") == spec.parent_report_hash
        and payload.get("selected_candidate_hash") == spec.parent_candidate_hash
        and payload.get("status") == "historical_rejected"
        and payload.get("failure_reasons") == ["daily_trend_validation_turnover_failed"]
        and payload.get("promotion_authority") is False
    )


def _evaluate_parent(rows: tuple[ScoreR6DailyRow, ...], spec: ScoreR6StabilitySpec) -> ScoreR6Metrics:
    def select(
        population: tuple[ScoreR6DailyRow, ...], _prior_codes: frozenset[str] | None
    ) -> tuple[ScoreR6DailyRow, ...]:
        scored = tuple(
            (value, row)
            for row in population
            if (value := score_r6_daily_candidate_row(spec.parent_candidate, row)) is not None
        )
        return select_score_r6_daily_top(
            scored, SCORE_R6_DAILY_SPEC.selection_limit, SCORE_R6_DAILY_SPEC.maximum_per_board
        )

    return evaluate_score_r6_daily_selections(rows, SCORE_R6_DAILY_SPEC, select)


def _evaluate_proxy(rows: tuple[ScoreR6DailyRow, ...]) -> ScoreR6Metrics:
    def select(
        population: tuple[ScoreR6DailyRow, ...], _prior_codes: frozenset[str] | None
    ) -> tuple[ScoreR6DailyRow, ...]:
        scored = tuple((value, row) for row in population if (value := score_r6_daily_proxy_row(row)) is not None)
        return select_score_r6_daily_top(
            scored, SCORE_R6_DAILY_SPEC.selection_limit, SCORE_R6_DAILY_SPEC.maximum_per_board
        )

    return evaluate_score_r6_daily_selections(rows, SCORE_R6_DAILY_SPEC, select)


def _evaluate_stability(
    candidate: ScoreR6StabilityCandidate,
    rows: tuple[ScoreR6DailyRow, ...],
    spec: ScoreR6StabilitySpec,
) -> ScoreR6Metrics:
    previous_scores: dict[str, float] = {}

    def select(
        population: tuple[ScoreR6DailyRow, ...], prior_codes: frozenset[str] | None
    ) -> tuple[ScoreR6DailyRow, ...]:
        next_scores: dict[str, float] = {}
        scored: list[tuple[float, ScoreR6DailyRow]] = []
        for row in population:
            raw_score = raw_score_r6_daily_candidate_row(spec.parent_candidate, row)
            if raw_score is None:
                continue
            previous = previous_scores.get(row.code)
            smoothed = (
                raw_score
                if previous is None
                else (1.0 - candidate.previous_score_weight) * raw_score + candidate.previous_score_weight * previous
            )
            next_scores[row.code] = smoothed
            adjusted = smoothed
            if prior_codes is not None:
                adjusted += (
                    candidate.rank_persistence_bonus if row.code in prior_codes else -candidate.entrant_turnover_penalty
                )
            if adjusted >= spec.parent_candidate.action_threshold:
                scored.append((adjusted, row))
        previous_scores.clear()
        previous_scores.update(next_scores)
        return select_score_r6_daily_top(
            tuple(scored), SCORE_R6_DAILY_SPEC.selection_limit, SCORE_R6_DAILY_SPEC.maximum_per_board
        )

    metrics = evaluate_score_r6_daily_selections(rows, SCORE_R6_DAILY_SPEC, select)
    if metrics.mean_net_excess_5d_pct is None:
        return metrics
    objective = (
        metrics.mean_net_excess_5d_pct
        - spec.objective_severe_coefficient * _required(metrics.severe_loss_rate)
        - spec.objective_turnover_coefficient * _required(metrics.mean_turnover)
        - spec.objective_stability_coefficient * _required(metrics.daily_net_excess_stddev)
        + spec.objective_recall_coefficient * _required(metrics.oracle_recall)
    )
    return replace(metrics, objective_value=objective)


def _training_eligible(
    candidate: ScoreR6Metrics,
    parent: ScoreR6Metrics,
    spec: ScoreR6StabilitySpec,
    minimum_days: int,
) -> bool:
    required_days = min(spec.minimum_selected_days, minimum_days)
    values = (
        candidate.mean_net_excess_5d_pct,
        candidate.severe_loss_rate,
        candidate.mean_turnover,
        parent.mean_net_excess_5d_pct,
        parent.severe_loss_rate,
        parent.mean_turnover,
    )
    if candidate.selected_days < required_days or any(value is None for value in values):
        return False
    return bool(
        _required(candidate.mean_net_excess_5d_pct)
        >= _required(parent.mean_net_excess_5d_pct) - spec.maximum_net_excess_loss_pct
        and _required(candidate.severe_loss_rate)
        <= _required(parent.severe_loss_rate) + spec.maximum_severe_loss_increase
        and _required(candidate.mean_turnover) <= _required(parent.mean_turnover) - spec.minimum_turnover_reduction
    )


def _candidate_order(
    item: tuple[ScoreR6StabilityCandidate, ScoreR6Metrics],
) -> tuple[float, float, float, str]:
    candidate, metrics = item
    return (
        -(metrics.objective_value if metrics.objective_value is not None else -math.inf),
        metrics.mean_turnover if metrics.mean_turnover is not None else math.inf,
        metrics.severe_loss_rate if metrics.severe_loss_rate is not None else math.inf,
        candidate.content_hash,
    )


def _diagnostic_failures(
    candidate: ScoreR6Metrics,
    parent: ScoreR6Metrics,
    proxy: ScoreR6Metrics,
    spec: ScoreR6StabilitySpec,
    minimum_days: int,
) -> tuple[str, ...]:
    failures: list[str] = []
    if candidate.selected_days < min(spec.minimum_selected_days, minimum_days):
        failures.append("daily_stability_diagnostic_days_incomplete")
    comparisons = (
        (
            candidate.mean_net_excess_5d_pct,
            parent.mean_net_excess_5d_pct,
            -spec.maximum_net_excess_loss_pct,
            False,
            "daily_stability_diagnostic_return_failed",
        ),
        (
            candidate.severe_loss_rate,
            parent.severe_loss_rate,
            spec.maximum_severe_loss_increase,
            True,
            "daily_stability_diagnostic_severe_failed",
        ),
        (
            candidate.mean_turnover,
            parent.mean_turnover,
            -spec.minimum_turnover_reduction,
            True,
            "daily_stability_diagnostic_turnover_reduction_failed",
        ),
        (
            candidate.mean_turnover,
            proxy.mean_turnover,
            spec.proxy_turnover_tolerance,
            True,
            "daily_stability_diagnostic_proxy_turnover_failed",
        ),
        (
            candidate.daily_net_excess_stddev,
            parent.daily_net_excess_stddev,
            spec.stability_tolerance,
            True,
            "daily_stability_diagnostic_stability_failed",
        ),
        (
            candidate.oracle_recall,
            parent.oracle_recall,
            -spec.recall_tolerance,
            False,
            "daily_stability_diagnostic_recall_failed",
        ),
        (
            candidate.maximum_stock_positive_contribution_fraction,
            parent.maximum_stock_positive_contribution_fraction,
            spec.stock_concentration_tolerance,
            True,
            "daily_stability_diagnostic_concentration_failed",
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
        or candidate.maximum_board_fraction
        > SCORE_R6_DAILY_SPEC.maximum_per_board / SCORE_R6_DAILY_SPEC.selection_limit
    ):
        failures.append("daily_stability_diagnostic_board_fraction_failed")
    return tuple(sorted(set(failures)))


def _empty_report(
    spec: ScoreR6StabilitySpec,
    archive: HistoricalArchiveStatus,
    manifest: HistoricalArchiveManifest,
    status: Literal["insufficient_coverage", "parent_mismatch"],
    reason: str,
) -> ScoreR6StabilityReport:
    empty = _empty_metrics(0)
    return _report(
        spec,
        archive,
        manifest,
        _ReportOutcome(status, None, empty, empty, empty, empty, empty, (reason,)),
    )


def _report(
    spec: ScoreR6StabilitySpec,
    archive: HistoricalArchiveStatus,
    manifest: HistoricalArchiveManifest,
    outcome: _ReportOutcome,
) -> ScoreR6StabilityReport:
    return ScoreR6StabilityReport(
        status=outcome.status,
        research_identity=spec.research_identity,
        research_spec_hash=spec.content_hash,
        parent_report_hash=spec.parent_report_hash,
        parent_candidate_hash=spec.parent_candidate_hash,
        archive=archive,
        archive_manifest=manifest,
        selected_candidate=outcome.selected_candidate,
        training=outcome.training,
        diagnostic=outcome.diagnostic,
        parent_training=outcome.parent_training,
        parent_diagnostic=outcome.parent_diagnostic,
        proxy_diagnostic=outcome.proxy_diagnostic,
        diagnostic_gate_passed=outcome.passed,
        failure_reasons=outcome.failure_reasons,
        limitations=_LIMITATIONS,
        evidence_class=spec.evidence_class,
        promotion_authority=False,
        schema_version=spec.report_schema_version,
    )


def _empty_metrics(trade_dates: int) -> ScoreR6Metrics:
    return ScoreR6Metrics(trade_dates, 0, 0, None, None, None, None, None, None, None, None)


def _date_count(rows: tuple[ScoreR6DailyRow, ...]) -> int:
    return len({row.trade_date for row in rows})


def _required(value: float | None) -> float:
    if value is None:
        raise ValueError("daily stability metric is unavailable")
    return value


_LIMITATIONS = (
    "corporate_risk_not_reconstructed",
    "current_universe_survivorship_bias",
    "deepseek_facts_not_reconstructed",
    "historical_industry_not_reconstructed",
    "historical_st_status_not_reconstructed",
    "intraday_tail_not_reconstructed",
    "reused_observed_validation_window",
)

__all__ = ["ScoreR6DailyParentArtifact", "ScoreR6StabilityScreeningService"]
