"""Single-shot historical screening for the frozen Tomorrow historical candidate."""

from __future__ import annotations

import dataclasses
import hashlib
import math
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal, Protocol

from trader.application.research.historical_screening import (
    HistoricalScreeningArchiveManifest,
    HistoricalScreeningArchiveStatus,
)
from trader.application.research.tomorrow_historical_report import (
    TomorrowHistoricalGateMetrics,
    TomorrowHistoricalReport,
)
from trader.domain.market.feature_contracts import TOMORROW_MODEL_FEATURE_MANIFEST
from trader.domain.recommendation.model_scoring import percentile_ranks
from trader.domain.research.artifact_identity import canonical_artifact_json
from trader.domain.research.baseline import mean_rank_ic, population_spearman, quantile_bucket, stock_net_contribution
from trader.domain.research.historical_screening import HISTORICAL_SCREENING_SPEC, HistoricalScreeningSpec
from trader.domain.research.paired_statistics import (
    PreregisteredBootstrapPlan,
    paired_moving_block_statistics,
)
from trader.domain.research.tomorrow_historical import (
    TOMORROW_HISTORICAL_SPEC,
    TomorrowHistoricalCandidate,
    TomorrowHistoricalModelArtifact,
    TomorrowHistoricalSpec,
)

HistoricalBoard = Literal["main", "chinext", "star"]
TOMORROW_HISTORICAL_ALPHA_FEATURE_IDS = TOMORROW_MODEL_FEATURE_MANIFEST.names


@dataclass(frozen=True, order=True)
class TomorrowHistoricalRow:
    trade_date: date
    code: str
    board: HistoricalBoard
    alpha_features: tuple[float, float, float, float, float, float]
    realized_volatility_20d: float
    downside_semivariance_20d: float
    drawdown_recovery_60d: float
    amihud_20d: float
    average_amount_20d: float
    baseline_score: float
    gross_excess_return: float
    mae_atr20: float

    def __post_init__(self) -> None:
        if len(self.code) != 6 or not self.code.isdigit() or self.board not in {"main", "chinext", "star"}:
            raise ValueError("Tomorrow historical row identity is invalid")
        values = (
            *self.alpha_features,
            self.realized_volatility_20d,
            self.downside_semivariance_20d,
            self.drawdown_recovery_60d,
            self.amihud_20d,
            self.average_amount_20d,
            self.baseline_score,
            self.gross_excess_return,
            self.mae_atr20,
        )
        if any(not math.isfinite(value) for value in values):
            raise ValueError("Tomorrow historical row values must be finite")
        if (
            min(
                self.realized_volatility_20d,
                self.downside_semivariance_20d,
                self.amihud_20d,
                self.average_amount_20d,
            )
            < 0.0
        ):
            raise ValueError("Tomorrow historical risk, cost, and capacity values cannot be negative")


@dataclass(frozen=True)
class TomorrowHistoricalModelFit:
    artifact: TomorrowHistoricalModelArtifact
    training_predictions: tuple[float, ...]
    validation_predictions: tuple[float, ...]
    validation_model_disagreement: tuple[float, ...]

    def __post_init__(self) -> None:
        if (
            not self.training_predictions
            or not self.validation_predictions
            or len(self.validation_predictions) != len(self.validation_model_disagreement)
        ):
            raise ValueError("Tomorrow historical model prediction coverage is incomplete")
        values = (*self.training_predictions, *self.validation_predictions, *self.validation_model_disagreement)
        if any(not math.isfinite(value) for value in values) or any(
            value < 0.0 for value in self.validation_model_disagreement
        ):
            raise ValueError("Tomorrow historical model predictions must be finite")


@dataclass(frozen=True)
class TomorrowHistoricalExecution:
    report: TomorrowHistoricalReport
    model_artifact: TomorrowHistoricalModelArtifact | None

    def __post_init__(self) -> None:
        expected = self.model_artifact.content_hash if self.model_artifact is not None else None
        if self.report.model_artifact_hash != expected:
            raise ValueError("Tomorrow historical execution model binding is invalid")


class TomorrowHistoricalEvidence(Protocol):
    def inspect(self, research_identity: str) -> HistoricalScreeningArchiveStatus: ...

    def manifest(self, spec: HistoricalScreeningSpec) -> HistoricalScreeningArchiveManifest: ...

    def tomorrow_historical_rows(self, spec: HistoricalScreeningSpec) -> Sequence[TomorrowHistoricalRow]: ...


class TomorrowHistoricalModelTrainer(Protocol):
    def fit(
        self,
        training: tuple[TomorrowHistoricalRow, ...],
        validation: tuple[TomorrowHistoricalRow, ...],
        candidate: TomorrowHistoricalCandidate,
    ) -> TomorrowHistoricalModelFit: ...


@dataclass(frozen=True)
class _SelectionRow:
    row: TomorrowHistoricalRow
    candidate_weight: float
    baseline_weight: float
    candidate_turnover: float
    baseline_turnover: float


@dataclass(frozen=True)
class _ReportSource:
    spec: TomorrowHistoricalSpec
    manifest: HistoricalScreeningArchiveManifest
    coverage: float
    training: tuple[TomorrowHistoricalRow, ...]
    validation: tuple[TomorrowHistoricalRow, ...]


class TomorrowHistoricalScreeningService:
    def __init__(self, evidence: TomorrowHistoricalEvidence, trainer: TomorrowHistoricalModelTrainer) -> None:
        self._evidence = evidence
        self._trainer = trainer

    def execute(self, spec: TomorrowHistoricalSpec) -> TomorrowHistoricalExecution:
        if spec != TOMORROW_HISTORICAL_SPEC:
            raise ValueError("Tomorrow historical screening requires the frozen historical spec")
        archive = self._evidence.inspect(spec.source_research_identity)
        manifest = self._evidence.manifest(HISTORICAL_SCREENING_SPEC)
        coverage = archive.completed_codes / archive.universe_count if archive.universe_count else 0.0
        source_valid = (
            archive.spec_hash == spec.source_spec_hash
            and manifest.spec_hash == spec.source_spec_hash
            and coverage >= spec.minimum_archive_coverage
            and all(item.bar_count >= 66 for item in manifest.histories)
        )
        if not source_valid:
            reason = (
                "score_h0_history_too_short"
                if any(item.bar_count < 66 for item in manifest.histories)
                else "score_h0_archive_coverage_incomplete"
            )
            return TomorrowHistoricalExecution(
                _report(_ReportSource(spec, manifest, coverage, (), ()), None, (reason,)),
                None,
            )
        rows = tuple(sorted(self._evidence.tomorrow_historical_rows(HISTORICAL_SCREENING_SPEC)))
        training = tuple(row for row in rows if spec.training_window[0] <= row.trade_date <= spec.training_window[1])
        validation = tuple(
            row for row in rows if spec.validation_window[0] <= row.trade_date <= spec.validation_window[1]
        )
        if not training or not validation:
            return TomorrowHistoricalExecution(
                _report(
                    _ReportSource(spec, manifest, coverage, training, validation),
                    None,
                    ("historical_split_incomplete",),
                ),
                None,
            )
        fit = self._trainer.fit(training, validation, spec.candidate)
        if len(fit.training_predictions) != len(training) or len(fit.validation_predictions) != len(validation):
            raise ValueError("Tomorrow historical trainer returned incomplete split predictions")
        metrics = dataclasses.replace(
            _evaluate(validation, fit.validation_predictions, fit.validation_model_disagreement, coverage, spec),
            training_trade_dates=len({row.trade_date for row in training}),
        )
        failures = _gate_failures(metrics, spec)
        return TomorrowHistoricalExecution(
            _report(
                _ReportSource(spec, manifest, coverage, training, validation),
                fit,
                failures,
                metrics=metrics,
            ),
            fit.artifact,
        )


def _report(
    source: _ReportSource,
    fit: TomorrowHistoricalModelFit | None,
    failures: tuple[str, ...],
    *,
    metrics: TomorrowHistoricalGateMetrics | None = None,
) -> TomorrowHistoricalReport:
    spec = source.spec
    empty = TomorrowHistoricalGateMetrics(
        archive_coverage=source.coverage,
        training_trade_dates=len({row.trade_date for row in source.training}),
        validation_trade_dates=len({row.trade_date for row in source.validation}),
        validation_pairs=len(source.validation),
        mean_net_increment_20bp=None,
        mean_net_increment_50bp=None,
        mean_net_increment_100bp=None,
        bootstrap_lower_bound_20bp=None,
        baseline_severe_loss_rate=None,
        candidate_severe_loss_rate=None,
        turnover_increase=None,
        mean_rank_ic=None,
        top_bottom_quintile_spread=None,
        maximum_stock_positive_fraction=None,
        top_five_positive_fraction=None,
        maximum_board_fraction=None,
    )
    return TomorrowHistoricalReport(
        research_spec_hash=spec.content_hash,
        source_spec_hash=spec.source_spec_hash,
        source_manifest_hash=source.manifest.content_hash,
        source_universe_hash=source.manifest.universe_hash,
        source_histories_hash=source.manifest.histories_hash,
        candidate_id=spec.candidate.candidate_id,
        status="historical_rejected" if failures else "historical_passed",
        metrics=metrics or empty,
        training_evidence_hash=_rows_hash(source.training) if source.training else None,
        validation_evidence_hash=_rows_hash(source.validation) if source.validation else None,
        model_artifact_hash=fit.artifact.content_hash if fit is not None else None,
        failure_reasons=failures,
    )


def _evaluate(
    rows: tuple[TomorrowHistoricalRow, ...],
    predictions: tuple[float, ...],
    disagreements: tuple[float, ...],
    coverage: float,
    spec: TomorrowHistoricalSpec,
) -> TomorrowHistoricalGateMetrics:
    grouped: dict[date, list[tuple[TomorrowHistoricalRow, float, float]]] = defaultdict(list)
    for row, prediction, disagreement in zip(rows, predictions, disagreements, strict=True):
        grouped[row.trade_date].append((row, prediction, disagreement))
    selected_rows: list[_SelectionRow] = []
    previous_candidate: frozenset[str] = frozenset()
    previous_baseline: frozenset[str] = frozenset()
    maximum_board_fraction = 0.0
    rank_ics: list[float | None] = []
    top_values: list[float] = []
    bottom_values: list[float] = []
    for trade_date in sorted(grouped):
        population = tuple(sorted(grouped[trade_date], key=lambda item: item[0].code))
        amihud_ranks = percentile_ranks(tuple(item[0].amihud_20d for item in population))
        candidates = tuple(
            (
                prediction - spec.cost_rates[0] * (1.0 + amihud_ranks[index]),
                row.realized_volatility_20d + row.downside_semivariance_20d - row.drawdown_recovery_60d,
                disagreement,
                row,
            )
            for index, (row, prediction, disagreement) in enumerate(population)
        )
        candidate = _select_candidate(candidates, spec)
        baseline = _select_baseline(population, spec)
        candidate_codes = frozenset(item[3].code for item in candidate)
        baseline_codes = frozenset(item[0].code for item in baseline)
        candidate_weight = 1.0 / len(candidate_codes) if candidate_codes else 0.0
        baseline_weight = 1.0 / len(baseline_codes) if baseline_codes else 0.0
        for row, _prediction, _disagreement in population:
            selected_rows.append(
                _SelectionRow(
                    row=row,
                    candidate_weight=candidate_weight if row.code in candidate_codes else 0.0,
                    baseline_weight=baseline_weight if row.code in baseline_codes else 0.0,
                    candidate_turnover=1.0 if row.code in candidate_codes - previous_candidate else 0.0,
                    baseline_turnover=1.0 if row.code in baseline_codes - previous_baseline else 0.0,
                )
            )
        if candidate_codes:
            counts = Counter(item[3].board for item in candidate)
            maximum_board_fraction = max(maximum_board_fraction, max(counts.values()) / len(candidate_codes))
        previous_candidate = candidate_codes
        previous_baseline = baseline_codes
        score_pairs = tuple((prediction, row.gross_excess_return) for row, prediction, _value in population)
        rank_ics.append(population_spearman(score_pairs))
        buckets = quantile_bucket(tuple((row.code, prediction) for row, prediction, _value in population))
        for row, _prediction, _value in population:
            net = row.gross_excess_return - spec.cost_rates[0]
            if buckets[row.code] == 5:
                top_values.append(net)
            elif buckets[row.code] == 1:
                bottom_values.append(net)
    daily_increments = tuple(_daily_increment(selected_rows, value) for value in spec.cost_rates)
    bootstrap = paired_moving_block_statistics(
        daily_increments[0],
        plan=PreregisteredBootstrapPlan(
            identity=spec.research_identity,
            master_seed=spec.bootstrap_master_seed,
            challenger_id=spec.candidate.candidate_id,
            block_days=spec.bootstrap_block_days,
            repetitions=spec.bootstrap_repetitions,
        ),
    )
    candidate_severe = _weighted_rate(selected_rows, "candidate", spec)
    baseline_severe = _weighted_rate(selected_rows, "baseline", spec)
    candidate_turnover = _weighted_average(selected_rows, "candidate")
    baseline_turnover = _weighted_average(selected_rows, "baseline")
    concentration = _positive_concentration(selected_rows, spec.cost_rates[0])
    return TomorrowHistoricalGateMetrics(
        archive_coverage=coverage,
        training_trade_dates=0,
        validation_trade_dates=len(grouped),
        validation_pairs=len(rows),
        mean_net_increment_20bp=_mean(daily_increments[0]),
        mean_net_increment_50bp=_mean(daily_increments[1]),
        mean_net_increment_100bp=_mean(daily_increments[2]),
        bootstrap_lower_bound_20bp=bootstrap.confidence_lower,
        baseline_severe_loss_rate=baseline_severe,
        candidate_severe_loss_rate=candidate_severe,
        turnover_increase=candidate_turnover - baseline_turnover,
        mean_rank_ic=mean_rank_ic(tuple(rank_ics)),
        top_bottom_quintile_spread=_mean(tuple(top_values)) - _mean(tuple(bottom_values)),
        maximum_stock_positive_fraction=concentration[0],
        top_five_positive_fraction=concentration[1],
        maximum_board_fraction=maximum_board_fraction,
    )


def _select_candidate(
    candidates: tuple[tuple[float, float, float, TomorrowHistoricalRow], ...],
    spec: TomorrowHistoricalSpec,
) -> tuple[tuple[float, float, float, TomorrowHistoricalRow], ...]:
    selected: list[tuple[float, float, float, TomorrowHistoricalRow]] = []
    board_counts: Counter[str] = Counter()
    maximum_per_board = math.floor(spec.top_k * spec.maximum_board_fraction)
    for item in sorted(candidates, key=lambda value: (-value[0], value[1], value[2], value[3].code)):
        if item[0] <= 0.0 or board_counts[item[3].board] >= maximum_per_board:
            continue
        selected.append(item)
        board_counts[item[3].board] += 1
        if len(selected) == spec.top_k:
            break
    return tuple(selected)


def _select_baseline(
    population: tuple[tuple[TomorrowHistoricalRow, float, float], ...],
    spec: TomorrowHistoricalSpec,
) -> tuple[tuple[TomorrowHistoricalRow, float, float], ...]:
    selected: list[tuple[TomorrowHistoricalRow, float, float]] = []
    board_counts: Counter[str] = Counter()
    maximum_per_board = math.floor(spec.top_k * spec.maximum_board_fraction)
    for item in sorted(population, key=lambda value: (-value[0].baseline_score, value[0].code)):
        if board_counts[item[0].board] >= maximum_per_board:
            continue
        selected.append(item)
        board_counts[item[0].board] += 1
        if len(selected) == spec.top_k:
            break
    return tuple(selected)


def _daily_increment(rows: list[_SelectionRow], cost_rate: float) -> tuple[float, ...]:
    values: dict[date, float] = defaultdict(float)
    for item in rows:
        candidate = stock_net_contribution(
            item.candidate_weight, item.row.gross_excess_return, item.candidate_turnover, cost_rate
        )
        baseline = stock_net_contribution(
            item.baseline_weight, item.row.gross_excess_return, item.baseline_turnover, cost_rate
        )
        values[item.row.trade_date] += candidate - baseline
    return tuple(values[trade_date] for trade_date in sorted(values))


def _weighted_rate(
    rows: list[_SelectionRow], track: Literal["candidate", "baseline"], spec: TomorrowHistoricalSpec
) -> float:
    weights = tuple(item.candidate_weight if track == "candidate" else item.baseline_weight for item in rows)
    denominator = math.fsum(weights)
    if denominator == 0.0:
        return 0.0
    return (
        math.fsum(
            weight
            for item, weight in zip(rows, weights, strict=True)
            if item.row.mae_atr20 <= spec.severe_loss_mae_atr20
        )
        / denominator
    )


def _weighted_average(rows: list[_SelectionRow], track: Literal["candidate", "baseline"]) -> float:
    weights = tuple(item.candidate_weight if track == "candidate" else item.baseline_weight for item in rows)
    turnovers = tuple(item.candidate_turnover if track == "candidate" else item.baseline_turnover for item in rows)
    denominator = math.fsum(weights)
    if denominator == 0.0:
        return 0.0
    return math.fsum(weight * value for weight, value in zip(weights, turnovers, strict=True)) / denominator


def _positive_concentration(rows: list[_SelectionRow], cost_rate: float) -> tuple[float | None, float | None]:
    contributions: dict[str, float] = defaultdict(float)
    for item in rows:
        contributions[item.row.code] += stock_net_contribution(
            item.candidate_weight, item.row.gross_excess_return, item.candidate_turnover, cost_rate
        ) - stock_net_contribution(
            item.baseline_weight, item.row.gross_excess_return, item.baseline_turnover, cost_rate
        )
    positive = sorted((value for value in contributions.values() if value > 0.0), reverse=True)
    denominator = math.fsum(positive)
    if denominator <= 0.0:
        return None, None
    return positive[0] / denominator, math.fsum(positive[:5]) / denominator


def _gate_failures(metrics: TomorrowHistoricalGateMetrics, spec: TomorrowHistoricalSpec) -> tuple[str, ...]:
    checks = (
        (metrics.validation_pairs < spec.minimum_validation_pairs, "validation_pair_floor"),
        (
            metrics.mean_net_increment_20bp is None or metrics.mean_net_increment_20bp <= 0.0,
            "mean_increment_not_positive",
        ),
        (
            metrics.bootstrap_lower_bound_20bp is None or metrics.bootstrap_lower_bound_20bp <= 0.0,
            "bootstrap_lower_bound_not_positive",
        ),
        (
            metrics.baseline_severe_loss_rate is None
            or metrics.candidate_severe_loss_rate is None
            or metrics.candidate_severe_loss_rate > metrics.baseline_severe_loss_rate,
            "severe_loss_rate_worse",
        ),
        (
            metrics.turnover_increase is None or metrics.turnover_increase > spec.maximum_turnover_increase,
            "turnover_limit",
        ),
        (metrics.mean_rank_ic is None or metrics.mean_rank_ic <= 0.0, "rank_ic_not_positive"),
        (
            metrics.top_bottom_quintile_spread is None or metrics.top_bottom_quintile_spread <= 0.0,
            "quintile_spread_not_positive",
        ),
        (
            metrics.maximum_stock_positive_fraction is None
            or metrics.maximum_stock_positive_fraction > spec.maximum_stock_positive_fraction,
            "stock_concentration_limit",
        ),
        (
            metrics.top_five_positive_fraction is None
            or metrics.top_five_positive_fraction > spec.maximum_top_five_positive_fraction,
            "top_five_concentration_limit",
        ),
        (
            metrics.maximum_board_fraction is None or metrics.maximum_board_fraction > spec.maximum_board_fraction,
            "board_concentration_limit",
        ),
    )
    return tuple(reason for failed, reason in checks if failed)


def _rows_hash(rows: tuple[TomorrowHistoricalRow, ...]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(canonical_artifact_json(row).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def _mean(values: tuple[float, ...]) -> float:
    return math.fsum(values) / len(values)


__all__ = [
    "HistoricalBoard",
    "TOMORROW_HISTORICAL_ALPHA_FEATURE_IDS",
    "TomorrowHistoricalExecution",
    "TomorrowHistoricalModelFit",
    "TomorrowHistoricalModelTrainer",
    "TomorrowHistoricalRow",
    "TomorrowHistoricalScreeningService",
]
