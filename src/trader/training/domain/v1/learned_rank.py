"""Immutable learned-rank contracts and date-isolated engineering evaluation."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from trader.recommendation.domain.market.history_alignment import HISTORY_ALIGNMENT_CONTRACT
from trader.recommendation.domain.market.v1.features import (
    LEARNED_RANK_FEATURE_IDS,
    LEARNED_RANK_HISTORY_SESSIONS,
)
from trader.recommendation.domain.publication.models import Strategy
from trader.training.domain.evaluation.research_identity import CanonicalResearchHash
from trader.training.domain.label_benchmark import TRAINING_LABEL_BENCHMARK_CONTRACT


@dataclass(frozen=True)
class V1TrainingSettings:
    fit_dates: int = 750
    early_stopping_dates: int = 20
    calibration_dates: int = 20
    validation_dates: int = 20
    embargo_dates: int = 5
    minimum_training_rows: int = 1000
    learning_rate: float = 0.05
    max_depth: int = 3
    num_leaves: int = 7
    minimum_leaf_rows: int = 20
    boosting_rounds: int = 200
    early_stopping_rounds: int = 20
    maximum_bins: int = 63
    histogram_pool_mib: int = 64
    seed: int = 0
    ridge_penalty: float = 10.0
    ranking_round_trip_cost: float = 0.002

    @property
    def history_sessions(self) -> int:
        return LEARNED_RANK_HISTORY_SESSIONS

    @property
    def momentum_horizons(self) -> tuple[int, ...]:
        return (20, 40, 60, 120, 250)

    def __post_init__(self) -> None:
        positive = (
            self.fit_dates,
            self.early_stopping_dates,
            self.calibration_dates,
            self.validation_dates,
            self.minimum_training_rows,
            self.max_depth,
            self.minimum_leaf_rows,
            self.boosting_rounds,
            self.early_stopping_rounds,
            self.histogram_pool_mib,
        )
        if (
            any(type(value) is not int or value < 1 for value in positive)
            or type(self.embargo_dates) is not int
            or self.embargo_dates < 5
            or type(self.num_leaves) is not int
            or self.num_leaves < 2
            or type(self.maximum_bins) is not int
            or self.maximum_bins < 2
            or type(self.seed) is not int
            or self.seed < 0
            or not math.isfinite(self.learning_rate)
            or not 0.0 < self.learning_rate <= 1.0
            or not math.isfinite(self.ridge_penalty)
            or self.ridge_penalty <= 0.0
            or not math.isfinite(self.ranking_round_trip_cost)
            or self.ranking_round_trip_cost < 0.0
        ):
            raise ValueError("learned-rank training settings are invalid")


@dataclass(frozen=True)
class LearnedRankDateSplit:
    fitting: tuple[date, ...]
    early_stopping: tuple[date, ...]
    calibration: tuple[date, ...]
    validation: tuple[date, ...]
    label_cutoff: date
    feature_cutoff: date
    maturity_sessions: int
    embargo_sessions: int

    def __post_init__(self) -> None:
        partitions = (self.fitting, self.early_stopping, self.calibration, self.validation)
        dates = tuple(day for partition in partitions for day in partition)
        if (
            any(not partition for partition in partitions)
            or dates != tuple(sorted(set(dates)))
            or self.maturity_sessions not in {1, 5}
            or self.embargo_sessions < max(5, self.maturity_sessions)
            or self.label_cutoff != self.validation[-1]
            or self.feature_cutoff >= self.label_cutoff
        ):
            raise ValueError("learned-rank split must contain four ordered isolated date partitions")

    @property
    def sample_dates(self) -> tuple[date, ...]:
        return (*self.fitting, *self.early_stopping, *self.calibration, *self.validation)


def BuildLearnedRankDateSplit(
    calendar: Sequence[date], settings: V1TrainingSettings, *, maturity_sessions: int
) -> LearnedRankDateSplit:
    """Fit recent mature labels without granting an untouched research identity."""

    dates = tuple(calendar)
    if maturity_sessions not in {1, 5} or dates != tuple(sorted(set(dates))):
        raise ValueError("learned-rank training calendar is invalid")
    embargo = max(settings.embargo_dates, maturity_sessions)
    end = len(dates) - maturity_sessions
    if end <= 0:
        raise ValueError("learned_rank_calendar_insufficient")
    partitions: list[tuple[date, ...]] = []
    for size in (
        settings.validation_dates,
        settings.calibration_dates,
        settings.early_stopping_dates,
        settings.fit_dates,
    ):
        start = end - size
        if start < settings.history_sessions:
            raise ValueError("learned_rank_calendar_insufficient")
        partitions.append(dates[start:end])
        end = start - embargo
    validation, calibration, early, fitting = partitions
    return LearnedRankDateSplit(
        fitting,
        early,
        calibration,
        validation,
        validation[-1],
        dates[dates.index(validation[-1]) - 1],
        maturity_sessions,
        embargo,
    )


def LearnedRankRelevance(values: Sequence[float]) -> tuple[int, ...]:
    """Five same-day grades with average ties and linear, explicit gains."""

    if not values or any(not math.isfinite(value) for value in values):
        raise ValueError("learned-rank relevance requires finite daily targets")
    if len(values) == 1:
        return (2,)
    ordered = sorted(enumerate(values), key=lambda item: item[1])
    result = [0] * len(values)
    start = 0
    while start < len(ordered):
        end = start + 1
        while end < len(ordered) and ordered[end][1] == ordered[start][1]:
            end += 1
        percentile = (start + end - 1) / (2.0 * (len(values) - 1))
        grade = min(4, int(percentile * 5.0))
        for index, _value in ordered[start:end]:
            result[index] = grade
        start = end
    return tuple(result)


@dataclass(frozen=True)
class LearnedRankPredictionMetrics:
    source_rows: int
    predicted_rows: int
    covered_codes: int
    covered_dates: int
    covered_industries: int
    mean_absolute_error: float
    root_mean_squared_error: float
    mean_daily_rank_ic: float
    mean_daily_ndcg_at_6: float

    def __post_init__(self) -> None:
        if (
            min(self.source_rows, self.predicted_rows, self.covered_codes, self.covered_dates, self.covered_industries)
            < 1
            or self.predicted_rows > self.source_rows
            or any(
                not math.isfinite(value)
                for value in (
                    self.mean_absolute_error,
                    self.root_mean_squared_error,
                    self.mean_daily_rank_ic,
                    self.mean_daily_ndcg_at_6,
                )
            )
            or self.mean_absolute_error < 0.0
            or self.root_mean_squared_error < 0.0
            or not -1.000001 <= self.mean_daily_rank_ic <= 1.000001
            or not 0.0 <= self.mean_daily_ndcg_at_6 <= 1.000001
        ):
            raise ValueError("learned-rank prediction metrics are invalid")


LEARNED_RANK_CONTRACT = (
    "learned_rank_completed_daily",
    LEARNED_RANK_FEATURE_IDS,
    HISTORY_ALIGNMENT_CONTRACT,
    "previous_251_market_sessions_qfq_close_nonnegative_amount",
    TRAINING_LABEL_BENCHMARK_CONTRACT,
    "tomorrow_t1_d25_mean_t2_t3_t4_t5",
    "daily_average_tie_relevance_0_4_linear_gain",
    "daily_equal_weight_inverse_query_size_no_lambda_normalization",
    "lambdarank_top10_ndcg6_shared_ridge_calibrated_gross",
    "shared_numeric_features_no_categorical_exposure_unknown_industry_allowed_at_inference",
    "engineering_shadow_no_return_validation_no_production_authority",
)


def LearnedRankContractHash(settings: V1TrainingSettings) -> str:
    return CanonicalResearchHash((LEARNED_RANK_CONTRACT, settings))


@dataclass(frozen=True)
class LearnedRankHead:
    """One complete ranking/return pair; never a production authorization."""

    strategy: Strategy
    training_contract_hash: str
    split: LearnedRankDateSplit
    transformer_means: tuple[float, ...]
    transformer_scales: tuple[float, ...]
    ridge_coefficients: tuple[float, ...]
    ridge_intercept: float
    calibration_intercept: float
    calibration_slope: float
    rank_model: str
    rank_iterations: int
    training_rows: int
    early_stopping_rows: int
    calibration_rows: int
    industries: tuple[str, ...]
    validation: LearnedRankPredictionMetrics

    def __post_init__(self) -> None:
        width = len(LEARNED_RANK_FEATURE_IDS)
        if (
            self.strategy not in {Strategy.TOMORROW, Strategy.D25}
            or len(self.training_contract_hash) != 64
            or any(char not in "0123456789abcdef" for char in self.training_contract_hash)
            or self.split.maturity_sessions != (1 if self.strategy is Strategy.TOMORROW else 5)
            or any(
                len(values) != width
                for values in (self.transformer_means, self.transformer_scales, self.ridge_coefficients)
            )
            or any(
                not math.isfinite(value)
                for value in (
                    *self.transformer_means,
                    *self.transformer_scales,
                    *self.ridge_coefficients,
                    self.ridge_intercept,
                    self.calibration_intercept,
                    self.calibration_slope,
                )
            )
            or any(value <= 0 for value in self.transformer_scales)
            or not self.rank_model
            or self.rank_iterations < 1
            or min(self.training_rows, self.early_stopping_rows, self.calibration_rows) < 1
            or not self.industries
            or self.industries != tuple(sorted(set(self.industries)))
        ):
            raise ValueError("learned-rank head is invalid")

    @property
    def content_hash(self) -> str:
        return CanonicalResearchHash(self)


@dataclass(frozen=True)
class LearnedRankPair:
    """Both strategy models and their shared training provenance."""

    training_input_hash: str
    source_identity_hash: str
    calendar_hash: str
    source_cutoff: date
    settings: V1TrainingSettings
    heads: tuple[LearnedRankHead, LearnedRankHead]
    training_identity_hash: str

    def __post_init__(self) -> None:
        if (
            any(
                len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
                for value in (
                    self.training_input_hash,
                    self.source_identity_hash,
                    self.calendar_hash,
                    self.training_identity_hash,
                )
            )
            or tuple(head.strategy for head in self.heads) != (Strategy.TOMORROW, Strategy.D25)
            or any(head.split.label_cutoff >= self.source_cutoff for head in self.heads)
            or any(head.training_contract_hash != self.contract_hash for head in self.heads)
        ):
            raise ValueError("learned-rank pair provenance is invalid")

    @property
    def contract_hash(self) -> str:
        return LearnedRankContractHash(self.settings)

    @property
    def content_hash(self) -> str:
        return CanonicalResearchHash((LEARNED_RANK_CONTRACT, self))


__all__ = [
    "LearnedRankContractHash",
    "BuildLearnedRankDateSplit",
    "LearnedRankDateSplit",
    "LearnedRankPredictionMetrics",
    "LearnedRankRelevance",
    "V1TrainingSettings",
    "LearnedRankHead",
    "LearnedRankPair",
    "LEARNED_RANK_CONTRACT",
]
