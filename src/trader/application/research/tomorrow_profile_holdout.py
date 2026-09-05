"""Read-only H0 holdout comparison for the sealed scoring-profile artifacts."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from trader.application.ports.model_scoring import ModelInput, ModelPredictorPort
from trader.application.research.historical_screening import HistoricalArchiveManifest, HistoricalArchiveStatus
from trader.application.research.replay_models import canonical_hash
from trader.application.research.tomorrow_historical_p2_models import TomorrowHistoricalP2GateMetrics
from trader.application.research.tomorrow_historical_p2_screening import TomorrowHistoricalP2Row
from trader.domain.research.baseline import mean_rank_ic, population_spearman, quantile_bucket, stock_net_contribution
from trader.domain.research.historical_screening import SCORE_H0_V1_SPEC, HistoricalScreeningSpec
from trader.domain.research.paired_statistics import (
    PreregisteredBootstrapPlan,
    newey_west_long_run_std,
    paired_moving_block_statistics,
)

_FEATURE_IDS = (
    "qfq_return_1d",
    "qfq_return_3d",
    "qfq_return_5d",
    "qfq_residual_momentum_20d_skip5",
    "qfq_residual_momentum_40d_skip5",
    "qfq_residual_momentum_60d_skip5",
)
_COST_RATES = (0.002, 0.005, 0.01)
TOMORROW_PROFILE_HOLDOUT_REPORT_HASH = "47e2b9bfd4d404521f8251e2e51c491aa96c1bc0d8423dea95e63320daa6e3bf"


class TomorrowProfileHoldoutEvidence(Protocol):
    def inspect(self, research_identity: str) -> HistoricalArchiveStatus: ...

    def manifest(self, spec: HistoricalScreeningSpec) -> HistoricalArchiveManifest: ...

    def tomorrow_historical_p2_rows(self, spec: HistoricalScreeningSpec) -> Sequence[TomorrowHistoricalP2Row]: ...


@dataclass(frozen=True)
class TomorrowProfileHoldoutMetrics:
    profile_id: str
    model_id: str
    model_hash: str
    gates: TomorrowHistoricalP2GateMetrics
    failure_reasons: tuple[str, ...]


@dataclass(frozen=True)
class TomorrowProfileHoldoutReport:
    source_spec_hash: str
    source_manifest_hash: str
    validation_evidence_hash: str
    validation_trade_dates: int
    validation_pairs: int
    v1: TomorrowProfileHoldoutMetrics
    v2: TomorrowProfileHoldoutMetrics
    daily_v2_minus_v1_20bp: tuple[float, ...]
    historical_daily_difference_std_pct: float
    historical_long_run_difference_std_pct: float
    status: str = "completed"
    production_authority: bool = False
    schema_version: str = "tomorrow_v1_v2_h0_holdout_report_v2"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            self.source_spec_hash != SCORE_H0_V1_SPEC.content_hash
            or len(self.source_manifest_hash) != 64
            or len(self.validation_evidence_hash) != 64
            or self.validation_trade_dates < 1
            or self.validation_pairs < 1
            or len(self.daily_v2_minus_v1_20bp) != self.validation_trade_dates
            or not math.isfinite(self.historical_daily_difference_std_pct)
            or self.historical_daily_difference_std_pct <= 0.0
            or not math.isfinite(self.historical_long_run_difference_std_pct)
            or self.historical_long_run_difference_std_pct <= 0.0
            or self.status != "completed"
            or self.production_authority
        ):
            raise ValueError("Tomorrow profile holdout report is invalid")
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class _ProfileDay:
    trade_date: date
    selected: frozenset[str]
    daily_net: tuple[float, float, float]
    severe_rate: float | None
    turnover: float
    rank_ic: float | None
    quintile_spread: float | None
    board_fraction: float
    positive_by_code: tuple[tuple[str, float], ...]


class TomorrowProfileHoldoutService:
    def __init__(
        self,
        evidence: TomorrowProfileHoldoutEvidence,
        v1: ModelPredictorPort,
        v2: ModelPredictorPort,
    ) -> None:
        if v1.profile_id != "v1" or v2.profile_id != "v2":
            raise ValueError("Tomorrow holdout requires sealed V1 and V2 predictors")
        self._evidence = evidence
        self._v1 = v1
        self._v2 = v2

    def execute(self) -> TomorrowProfileHoldoutReport:
        archive = self._evidence.inspect(SCORE_H0_V1_SPEC.research_identity)
        manifest = self._evidence.manifest(SCORE_H0_V1_SPEC)
        if archive.spec_hash != SCORE_H0_V1_SPEC.content_hash or manifest.spec_hash != SCORE_H0_V1_SPEC.content_hash:
            raise ValueError("Tomorrow holdout requires the exact H0 evidence identity")
        rows = tuple(
            row
            for row in self._evidence.tomorrow_historical_p2_rows(SCORE_H0_V1_SPEC)
            if SCORE_H0_V1_SPEC.validation_start <= row.trade_date <= SCORE_H0_V1_SPEC.validation_end
        )
        if not rows:
            raise ValueError("Tomorrow holdout validation rows are unavailable")
        ordered = tuple(sorted(rows))
        baseline = _profile_days(ordered, None, feature_ids=(), profile_id="baseline")
        v1_predictions = _predict(self._v1, ordered)
        v2_predictions = _predict(self._v2, ordered)
        v1_days = _profile_days(ordered, v1_predictions, feature_ids=self._v1.feature_ids, profile_id="v1")
        v2_days = _profile_days(ordered, v2_predictions, feature_ids=self._v2.feature_ids, profile_id="v2")
        if tuple(item.trade_date for item in v1_days) != tuple(item.trade_date for item in v2_days):
            raise ValueError("Tomorrow holdout profile dates are not paired")
        daily_difference = tuple(
            (right.daily_net[0] - left.daily_net[0]) * 100.0 for left, right in zip(v1_days, v2_days, strict=True)
        )
        return TomorrowProfileHoldoutReport(
            source_spec_hash=SCORE_H0_V1_SPEC.content_hash,
            source_manifest_hash=manifest.content_hash,
            validation_evidence_hash=canonical_hash(ordered),
            validation_trade_dates=len(v1_days),
            validation_pairs=len(ordered),
            v1=_profile_metrics(self._v1, v1_days, baseline, archive, len(ordered)),
            v2=_profile_metrics(self._v2, v2_days, baseline, archive, len(ordered)),
            daily_v2_minus_v1_20bp=daily_difference,
            historical_daily_difference_std_pct=_sample_std(daily_difference),
            historical_long_run_difference_std_pct=newey_west_long_run_std(daily_difference, lag_days=4),
        )


def _predict(
    predictor: ModelPredictorPort,
    rows: tuple[TomorrowHistoricalP2Row, ...],
) -> tuple[float, ...]:
    positions = tuple(_FEATURE_IDS.index(item) for item in predictor.feature_ids)
    inputs = tuple(ModelInput(row.code, tuple(row.alpha_features[position] for position in positions)) for row in rows)
    predictions = predictor.predict(inputs)
    if tuple(item.code for item in predictions) != tuple(item.code for item in inputs):
        raise ValueError("Tomorrow holdout predictor returned mismatched rows")
    return tuple(item.predicted_excess_return for item in predictions)


def _profile_days(
    rows: tuple[TomorrowHistoricalP2Row, ...],
    predictions: tuple[float, ...] | None,
    *,
    feature_ids: tuple[str, ...],
    profile_id: str,
) -> tuple[_ProfileDay, ...]:
    del feature_ids
    if predictions is not None and len(predictions) != len(rows):
        raise ValueError("Tomorrow holdout predictions are incomplete")
    grouped: dict[date, list[tuple[TomorrowHistoricalP2Row, float | None]]] = defaultdict(list)
    for index, row in enumerate(rows):
        grouped[row.trade_date].append((row, predictions[index] if predictions is not None else None))
    previous: frozenset[str] = frozenset()
    days: list[_ProfileDay] = []
    for trade_date in sorted(grouped):
        population = tuple(sorted(grouped[trade_date], key=lambda item: item[0].code))
        costs = _costs(population)
        candidates = tuple(
            (
                row.baseline_score if prediction is None else prediction - costs[index],
                row,
                prediction,
            )
            for index, (row, prediction) in enumerate(population)
        )
        selected_rows = _select(candidates, require_positive=profile_id != "baseline")
        selected = frozenset(item[1].code for item in selected_rows)
        weight = 1.0 / len(selected) if selected else 0.0
        turnover = len(selected - previous) * weight
        daily_net = tuple(
            math.fsum(
                stock_net_contribution(
                    weight if row.code in selected else 0.0,
                    row.gross_excess_return,
                    1.0 if row.code in selected - previous else 0.0,
                    cost,
                )
                for row, _prediction in population
            )
            for cost in _COST_RATES
        )
        severe = (
            math.fsum(1.0 for _utility, row, _prediction in selected_rows if row.mae_atr20 <= -1.5) / len(selected_rows)
            if selected_rows
            else None
        )
        score_pairs = tuple((utility, row.gross_excess_return) for utility, row, _prediction in candidates)
        rank_ic = population_spearman(score_pairs)
        buckets = quantile_bucket(tuple((row.code, utility) for utility, row, _prediction in candidates))
        top = tuple(
            row.gross_excess_return - _COST_RATES[0] for utility, row, _value in candidates if buckets[row.code] == 5
        )
        bottom = tuple(
            row.gross_excess_return - _COST_RATES[0] for utility, row, _value in candidates if buckets[row.code] == 1
        )
        counts = Counter(row.board for _utility, row, _prediction in selected_rows)
        board_fraction = max(counts.values()) / len(selected_rows) if selected_rows else 0.0
        positive = tuple(
            (row.code, max(0.0, weight * (row.gross_excess_return - _COST_RATES[0])))
            for _utility, row, _prediction in selected_rows
        )
        fixed_daily_net = (daily_net[0], daily_net[1], daily_net[2])
        days.append(
            _ProfileDay(
                trade_date,
                selected,
                fixed_daily_net,
                severe,
                turnover,
                rank_ic,
                (_mean(top) - _mean(bottom)) if top and bottom else None,
                board_fraction,
                positive,
            )
        )
        previous = selected
    return tuple(days)


def _costs(population: tuple[tuple[TomorrowHistoricalP2Row, float | None], ...]) -> tuple[float, ...]:
    values = tuple(item[0].amihud_20d for item in population)
    order = sorted(range(len(values)), key=lambda index: (values[index], index))
    ranks = [0.0] * len(values)
    for position, index in enumerate(order):
        ranks[index] = position / (len(values) - 1) if len(values) > 1 else 0.0
    return tuple(_COST_RATES[0] * (1.0 + rank) for rank in ranks)


def _select(
    values: tuple[tuple[float, TomorrowHistoricalP2Row, float | None], ...],
    *,
    require_positive: bool,
) -> tuple[tuple[float, TomorrowHistoricalP2Row, float | None], ...]:
    selected: list[tuple[float, TomorrowHistoricalP2Row, float | None]] = []
    boards: Counter[str] = Counter()
    for item in sorted(values, key=lambda value: (-value[0], value[1].code)):
        if (require_positive and item[0] <= 0.0) or boards[item[1].board] >= 3:
            continue
        selected.append(item)
        boards[item[1].board] += 1
        if len(selected) == 6:
            break
    return tuple(selected)


def _profile_metrics(
    predictor: ModelPredictorPort,
    profile: tuple[_ProfileDay, ...],
    baseline: tuple[_ProfileDay, ...],
    archive: HistoricalArchiveStatus,
    validation_pairs: int,
) -> TomorrowProfileHoldoutMetrics:
    differences = tuple(
        tuple(
            candidate.daily_net[index] - control.daily_net[index]
            for candidate, control in zip(profile, baseline, strict=True)
        )
        for index in range(3)
    )
    bootstrap = paired_moving_block_statistics(
        differences[0],
        plan=PreregisteredBootstrapPlan(
            identity="tomorrow_v1_v2_h0_holdout_v1",
            master_seed=20260831,
            challenger_id=predictor.profile_id,
            block_days=5,
            repetitions=10_000,
        ),
    )
    positive: dict[str, float] = defaultdict(float)
    for day in profile:
        for code, value in day.positive_by_code:
            positive[code] += value
    total_positive = math.fsum(positive.values())
    shares = sorted((value / total_positive for value in positive.values()), reverse=True) if total_positive else []
    severe_candidate = _mean(tuple(item.severe_rate for item in profile if item.severe_rate is not None))
    severe_baseline = _mean(tuple(item.severe_rate for item in baseline if item.severe_rate is not None))
    coverage = archive.completed_codes / archive.universe_count if archive.universe_count else 0.0
    gates = TomorrowHistoricalP2GateMetrics(
        archive_coverage=coverage,
        training_trade_dates=0,
        validation_trade_dates=len(profile),
        validation_pairs=validation_pairs,
        mean_net_increment_20bp=_mean(differences[0]),
        mean_net_increment_50bp=_mean(differences[1]),
        mean_net_increment_100bp=_mean(differences[2]),
        bootstrap_lower_bound_20bp=bootstrap.confidence_lower,
        baseline_severe_loss_rate=severe_baseline,
        candidate_severe_loss_rate=severe_candidate,
        turnover_increase=_mean(tuple(item.turnover for item in profile))
        - _mean(tuple(item.turnover for item in baseline)),
        mean_rank_ic=mean_rank_ic(tuple(item.rank_ic for item in profile)),
        top_bottom_quintile_spread=_mean(
            tuple(item.quintile_spread for item in profile if item.quintile_spread is not None)
        ),
        maximum_stock_positive_fraction=shares[0] if shares else 0.0,
        top_five_positive_fraction=math.fsum(shares[:5]),
        maximum_board_fraction=max((item.board_fraction for item in profile), default=0.0),
    )
    failures = _failures(gates)
    return TomorrowProfileHoldoutMetrics(
        predictor.profile_id,
        predictor.model_id,
        predictor.model_hash,
        gates,
        failures,
    )


def _failures(metrics: TomorrowHistoricalP2GateMetrics) -> tuple[str, ...]:
    checks = (
        (metrics.archive_coverage < 0.95, "archive_coverage"),
        (
            metrics.bootstrap_lower_bound_20bp is None or metrics.bootstrap_lower_bound_20bp <= 0.0,
            "bootstrap_lower_bound",
        ),
        (
            metrics.candidate_severe_loss_rate is None
            or metrics.baseline_severe_loss_rate is None
            or metrics.candidate_severe_loss_rate > metrics.baseline_severe_loss_rate,
            "severe_loss_rate_worse",
        ),
        (metrics.turnover_increase is None or metrics.turnover_increase > 0.05, "turnover_limit"),
        (metrics.mean_rank_ic is None or metrics.mean_rank_ic <= 0.0, "rank_ic_not_positive"),
        (
            metrics.top_bottom_quintile_spread is None or metrics.top_bottom_quintile_spread <= 0.0,
            "quintile_spread_not_positive",
        ),
        (
            metrics.maximum_stock_positive_fraction is None or metrics.maximum_stock_positive_fraction > 0.10,
            "stock_concentration",
        ),
        (
            metrics.top_five_positive_fraction is None or metrics.top_five_positive_fraction > 0.30,
            "top_five_concentration",
        ),
        (metrics.maximum_board_fraction is None or metrics.maximum_board_fraction > 0.60, "board_concentration"),
    )
    return tuple(reason for failed, reason in checks if failed)


def _mean(values: tuple[float, ...]) -> float:
    return math.fsum(values) / len(values) if values else 0.0


def _sample_std(values: tuple[float, ...]) -> float:
    if len(values) < 2:
        raise ValueError("Tomorrow holdout power estimate requires at least two days")
    mean = _mean(values)
    return math.sqrt(math.fsum((value - mean) ** 2 for value in values) / (len(values) - 1))


__all__ = [
    "TOMORROW_PROFILE_HOLDOUT_REPORT_HASH",
    "TomorrowProfileHoldoutEvidence",
    "TomorrowProfileHoldoutMetrics",
    "TomorrowProfileHoldoutReport",
    "TomorrowProfileHoldoutService",
]
