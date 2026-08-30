"""Single-shot historical screening for the frozen Tomorrow P2 candidate."""

from __future__ import annotations

import dataclasses
import hashlib
import math
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal, Protocol

from trader.application.research.historical_screening import HistoricalArchiveManifest, HistoricalArchiveStatus
from trader.application.research.replay_models import canonical_hash, canonical_json
from trader.application.research.tomorrow_historical_p2_models import (
    TomorrowHistoricalP2GateMetrics,
    TomorrowHistoricalP2Report,
)
from trader.domain.research.baseline import mean_rank_ic, population_spearman, quantile_bucket, stock_net_contribution
from trader.domain.research.historical_screening import SCORE_H0_V1_SPEC, HistoricalScreeningSpec
from trader.domain.research.paired_statistics import (
    PreregisteredBootstrapPlan,
    paired_moving_block_statistics,
)
from trader.domain.research.tomorrow_historical_p2 import (
    TOMORROW_HISTORICAL_P2_CANDIDATE_ID,
    TOMORROW_HISTORICAL_P2_SPEC,
    TomorrowHistoricalP2Candidate,
    TomorrowHistoricalP2Spec,
)

HistoricalP2Board = Literal["main", "chinext", "star"]
TOMORROW_HISTORICAL_P2_ALPHA_FEATURE_IDS = (
    "qfq_return_1d",
    "qfq_return_3d",
    "qfq_return_5d",
    "qfq_residual_momentum_20d_skip5",
    "qfq_residual_momentum_40d_skip5",
    "qfq_residual_momentum_60d_skip5",
)


@dataclass(frozen=True, order=True)
class TomorrowHistoricalP2Row:
    trade_date: date
    code: str
    board: HistoricalP2Board
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
            raise ValueError("Tomorrow P2 row identity is invalid")
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
            raise ValueError("Tomorrow P2 row values must be finite")
        if (
            min(
                self.realized_volatility_20d,
                self.downside_semivariance_20d,
                self.amihud_20d,
                self.average_amount_20d,
            )
            < 0.0
        ):
            raise ValueError("Tomorrow P2 risk, cost, and capacity values cannot be negative")


@dataclass(frozen=True)
class TomorrowHistoricalP2ModelArtifact:
    candidate_id: str
    feature_ids: tuple[str, ...]
    transformer_means: tuple[float, ...]
    transformer_scales: tuple[float, ...]
    linear_intercept: float
    linear_coefficients: tuple[float, ...]
    lightgbm_model: str
    lightgbm_best_iteration: int
    training_rows: int
    internal_validation_rows: int
    schema_version: str = "score_tomorrow_historical_p2_model_v1"
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        width = len(self.feature_ids)
        if (
            self.candidate_id != TOMORROW_HISTORICAL_P2_CANDIDATE_ID
            or width < 1
            or len(set(self.feature_ids)) != width
            or len(self.transformer_means) != width
            or len(self.transformer_scales) != width
            or len(self.linear_coefficients) != width
            or not self.lightgbm_model
            or self.lightgbm_best_iteration < 1
            or self.training_rows < 1
            or not 1 <= self.internal_validation_rows < self.training_rows
            or self.schema_version != "score_tomorrow_historical_p2_model_v1"
        ):
            raise ValueError("Tomorrow P2 model artifact identity is invalid")
        numeric = (
            *self.transformer_means,
            *self.transformer_scales,
            self.linear_intercept,
            *self.linear_coefficients,
        )
        if any(not math.isfinite(value) for value in numeric) or any(value <= 0.0 for value in self.transformer_scales):
            raise ValueError("Tomorrow P2 model artifact parameters are invalid")
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class TomorrowHistoricalP2ModelFit:
    artifact: TomorrowHistoricalP2ModelArtifact
    training_predictions: tuple[float, ...]
    validation_predictions: tuple[float, ...]
    validation_model_disagreement: tuple[float, ...]

    def __post_init__(self) -> None:
        if (
            not self.training_predictions
            or not self.validation_predictions
            or len(self.validation_predictions) != len(self.validation_model_disagreement)
        ):
            raise ValueError("Tomorrow P2 model prediction coverage is incomplete")
        values = (*self.training_predictions, *self.validation_predictions, *self.validation_model_disagreement)
        if any(not math.isfinite(value) for value in values) or any(
            value < 0.0 for value in self.validation_model_disagreement
        ):
            raise ValueError("Tomorrow P2 model predictions must be finite")


@dataclass(frozen=True)
class TomorrowHistoricalP2Execution:
    report: TomorrowHistoricalP2Report
    model_artifact: TomorrowHistoricalP2ModelArtifact | None

    def __post_init__(self) -> None:
        expected = self.model_artifact.content_hash if self.model_artifact is not None else None
        if self.report.model_artifact_hash != expected:
            raise ValueError("Tomorrow P2 execution model binding is invalid")


class TomorrowHistoricalP2Evidence(Protocol):
    def inspect(self, research_identity: str) -> HistoricalArchiveStatus: ...

    def manifest(self, spec: HistoricalScreeningSpec) -> HistoricalArchiveManifest: ...

    def tomorrow_historical_p2_rows(self, spec: HistoricalScreeningSpec) -> Sequence[TomorrowHistoricalP2Row]: ...


class TomorrowHistoricalP2ModelTrainer(Protocol):
    def fit(
        self,
        training: tuple[TomorrowHistoricalP2Row, ...],
        validation: tuple[TomorrowHistoricalP2Row, ...],
        candidate: TomorrowHistoricalP2Candidate,
    ) -> TomorrowHistoricalP2ModelFit: ...


@dataclass(frozen=True)
class _SelectionRow:
    row: TomorrowHistoricalP2Row
    candidate_weight: float
    baseline_weight: float
    candidate_turnover: float
    baseline_turnover: float


@dataclass(frozen=True)
class _ReportSource:
    spec: TomorrowHistoricalP2Spec
    manifest: HistoricalArchiveManifest
    coverage: float
    training: tuple[TomorrowHistoricalP2Row, ...]
    validation: tuple[TomorrowHistoricalP2Row, ...]


class TomorrowHistoricalP2ScreeningService:
    def __init__(self, evidence: TomorrowHistoricalP2Evidence, trainer: TomorrowHistoricalP2ModelTrainer) -> None:
        self._evidence = evidence
        self._trainer = trainer

    def execute(self, spec: TomorrowHistoricalP2Spec) -> TomorrowHistoricalP2Execution:
        if spec != TOMORROW_HISTORICAL_P2_SPEC:
            raise ValueError("Tomorrow P2 screening requires the frozen historical spec")
        archive = self._evidence.inspect(spec.source_research_identity)
        manifest = self._evidence.manifest(SCORE_H0_V1_SPEC)
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
            return TomorrowHistoricalP2Execution(
                _report(_ReportSource(spec, manifest, coverage, (), ()), None, (reason,)),
                None,
            )
        rows = tuple(sorted(self._evidence.tomorrow_historical_p2_rows(SCORE_H0_V1_SPEC)))
        training = tuple(row for row in rows if spec.training_window[0] <= row.trade_date <= spec.training_window[1])
        validation = tuple(
            row for row in rows if spec.validation_window[0] <= row.trade_date <= spec.validation_window[1]
        )
        if not training or not validation:
            return TomorrowHistoricalP2Execution(
                _report(
                    _ReportSource(spec, manifest, coverage, training, validation),
                    None,
                    ("historical_split_incomplete",),
                ),
                None,
            )
        fit = self._trainer.fit(training, validation, spec.candidate)
        if len(fit.training_predictions) != len(training) or len(fit.validation_predictions) != len(validation):
            raise ValueError("Tomorrow P2 trainer returned incomplete split predictions")
        metrics = dataclasses.replace(
            _evaluate(validation, fit.validation_predictions, fit.validation_model_disagreement, coverage, spec),
            training_trade_dates=len({row.trade_date for row in training}),
        )
        failures = _gate_failures(metrics, spec)
        return TomorrowHistoricalP2Execution(
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
    fit: TomorrowHistoricalP2ModelFit | None,
    failures: tuple[str, ...],
    *,
    metrics: TomorrowHistoricalP2GateMetrics | None = None,
) -> TomorrowHistoricalP2Report:
    spec = source.spec
    empty = TomorrowHistoricalP2GateMetrics(
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
    return TomorrowHistoricalP2Report(
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
    rows: tuple[TomorrowHistoricalP2Row, ...],
    predictions: tuple[float, ...],
    disagreements: tuple[float, ...],
    coverage: float,
    spec: TomorrowHistoricalP2Spec,
) -> TomorrowHistoricalP2GateMetrics:
    grouped: dict[date, list[tuple[TomorrowHistoricalP2Row, float, float]]] = defaultdict(list)
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
        amihud_ranks = _percentile_ranks(tuple(item[0].amihud_20d for item in population))
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
    return TomorrowHistoricalP2GateMetrics(
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
    candidates: tuple[tuple[float, float, float, TomorrowHistoricalP2Row], ...],
    spec: TomorrowHistoricalP2Spec,
) -> tuple[tuple[float, float, float, TomorrowHistoricalP2Row], ...]:
    selected: list[tuple[float, float, float, TomorrowHistoricalP2Row]] = []
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
    population: tuple[tuple[TomorrowHistoricalP2Row, float, float], ...],
    spec: TomorrowHistoricalP2Spec,
) -> tuple[tuple[TomorrowHistoricalP2Row, float, float], ...]:
    selected: list[tuple[TomorrowHistoricalP2Row, float, float]] = []
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
    rows: list[_SelectionRow], track: Literal["candidate", "baseline"], spec: TomorrowHistoricalP2Spec
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


def _gate_failures(metrics: TomorrowHistoricalP2GateMetrics, spec: TomorrowHistoricalP2Spec) -> tuple[str, ...]:
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


def _percentile_ranks(values: tuple[float, ...]) -> tuple[float, ...]:
    if len(values) <= 1:
        return (0.0,) * len(values)
    order = sorted(range(len(values)), key=lambda index: (values[index], index))
    ranks = [0.0] * len(values)
    for position, index in enumerate(order):
        ranks[index] = position / (len(values) - 1)
    return tuple(ranks)


def _rows_hash(rows: tuple[TomorrowHistoricalP2Row, ...]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(canonical_json(row).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def _mean(values: tuple[float, ...]) -> float:
    return math.fsum(values) / len(values)


__all__ = [
    "HistoricalP2Board",
    "TOMORROW_HISTORICAL_P2_ALPHA_FEATURE_IDS",
    "TomorrowHistoricalP2Execution",
    "TomorrowHistoricalP2ModelArtifact",
    "TomorrowHistoricalP2ModelFit",
    "TomorrowHistoricalP2ModelTrainer",
    "TomorrowHistoricalP2Row",
    "TomorrowHistoricalP2ScreeningService",
]
