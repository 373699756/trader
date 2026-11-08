"""Pure learned-rank date plans, reuse qualification and sample admission."""

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from trader.recommendation.domain.publication.models import Strategy
from trader.download.domain.current_industry import CurrentIndustrySnapshot
from trader.training.domain.training_input_identity import BuildTrainingInputHash
from trader.training.domain.final_fit import BuildFinalFitDatePlan, FinalFitDatePlan
from trader.training.domain.v1.learned_rank import (
    BuildLearnedRankDateSplit, LearnedRankDateSplit, LearnedRankFinalPair, V1TrainingSettings,
)


@dataclass(frozen=True)
class LearnedRankSampleCounts:
    source_rows: int
    samples: int
    missing_prices: int
    missing_industry: int
    short_window: int
    missing_t1: int
    missing_d25: int
    suspended_sessions: int = 0
    estimated_sessions: int = 0


@dataclass(frozen=True)
class LearnedRankPartitionCoverage:
    strategy: Strategy
    partition: Literal["fitting", "early_stopping", "calibration", "validation", "final_fitting"]
    expected_dates: int
    effective_dates: int
    rows: int


@dataclass(frozen=True)
class LearnedRankTrainingSource:
    snapshot_hash: str
    source_identity_hash: str
    calendar_hash: str
    source_cutoff: date
    calendar: tuple[date, ...]
    identity_hash: str
    industry_snapshot: CurrentIndustrySnapshot = field(kw_only=True)

    @property
    def training_input_hash(self) -> str:
        return BuildTrainingInputHash(self.snapshot_hash, self.industry_snapshot.content_hash)


@dataclass(frozen=True)
class LearnedRankTrainingPlan:
    source: LearnedRankTrainingSource
    settings: V1TrainingSettings
    splits: tuple[LearnedRankDateSplit, LearnedRankDateSplit]
    final_fit_plans: tuple[FinalFitDatePlan, FinalFitDatePlan]


def BuildLearnedRankTrainingPlan(
    source: LearnedRankTrainingSource, settings: V1TrainingSettings,
) -> LearnedRankTrainingPlan:
    return LearnedRankTrainingPlan(
        source, settings,
        _diagnostic_splits(source.calendar, source.source_cutoff, settings),
        (
            BuildFinalFitDatePlan(
                source.calendar, source_cutoff=source.source_cutoff, fit_sessions=settings.fit_dates, maturity_sessions=1,
            ),
            BuildFinalFitDatePlan(
                source.calendar, source_cutoff=source.source_cutoff, fit_sessions=settings.fit_dates, maturity_sessions=5,
            ),
        ),
    )


def BuildLearnedRankReadDates(
    calendar: tuple[date, ...], source_cutoff: date, settings: V1TrainingSettings,
) -> frozenset[date]:
    splits = _diagnostic_splits(calendar, source_cutoff, settings)
    first = min(calendar.index(day) for split in splits for day in split.sample_dates) - settings.history_sessions
    return frozenset(day for day in calendar[first:] if day <= source_cutoff)


def _diagnostic_splits(
    calendar: tuple[date, ...], source_cutoff: date, settings: V1TrainingSettings,
) -> tuple[LearnedRankDateSplit, LearnedRankDateSplit]:
    diagnostic_calendar = tuple(day for day in calendar if day <= source_cutoff)
    return (
        BuildLearnedRankDateSplit(diagnostic_calendar, settings, maturity_sessions=1),
        BuildLearnedRankDateSplit(diagnostic_calendar, settings, maturity_sessions=5),
    )


def CanReuseLearnedRankPair(pair: LearnedRankFinalPair, plan: LearnedRankTrainingPlan) -> bool:
    source = plan.source
    return (
        pair.training_identity_hash == source.identity_hash
        and pair.training_input_hash == source.training_input_hash
        and pair.source_identity_hash == source.source_identity_hash
        and pair.calendar_hash == source.calendar_hash
        and pair.source_cutoff == source.source_cutoff
        and pair.settings == plan.settings
        and pair.calendar_dates == source.calendar
        and tuple(head.split for head in pair.heads) == plan.splits
        and tuple(head.final_fit for head in pair.heads) == plan.final_fit_plans
    )


def LearnedRankCoverageFailures(
    coverage: tuple[LearnedRankPartitionCoverage, ...], plan: LearnedRankTrainingPlan,
) -> tuple[str, ...]:
    expected = tuple(
        (strategy, partition, len(dates))
        for strategy, split, final_fit in zip(
            (Strategy.TOMORROW, Strategy.D25), plan.splits, plan.final_fit_plans, strict=True,
        )
        for partition, dates in (
            ("fitting", split.fitting), ("early_stopping", split.early_stopping),
            ("calibration", split.calibration), ("validation", split.validation),
            ("final_fitting", final_fit.fit_dates),
        )
    )
    observed = tuple((item.strategy, item.partition) for item in coverage)
    expected_pairs = tuple((strategy, partition) for strategy, partition, _ in expected)
    if (
        any(not isinstance(item.strategy, Strategy) for item in coverage)
        or len(observed) != len(expected_pairs) or frozenset(observed) != frozenset(expected_pairs)
    ):
        return ("learned_rank_sample_coverage_contract_invalid",)
    failures = []
    for item in coverage:
        dates = next(
            count for strategy, partition, count in expected
            if strategy is item.strategy and partition == item.partition
        )
        if item.rows <= 0:
            failures.append(f"learned_rank_sample_partition_empty:{item.strategy.value}:{item.partition}")
        elif item.effective_dates != dates or item.expected_dates != dates:
            failures.append(
                f"learned_rank_final_fit_dates_insufficient:{item.strategy.value}"
                if item.partition == "final_fitting"
                else f"learned_rank_sample_dates_insufficient:{item.strategy.value}:{item.partition}"
            )
        elif item.partition in {"fitting", "final_fitting"} and item.rows < plan.settings.minimum_training_rows:
            failures.append(f"learned_rank_training_rows_insufficient:{item.strategy.value}:{item.partition}")
    return tuple(failures)


def EstimateLearnedRankFittingBytes(coverage: tuple[LearnedRankPartitionCoverage, ...]) -> int:
    return max(
        sum(item.rows for item in coverage if item.strategy is strategy and (item.partition == "final_fitting") == final_phase)
        for strategy in (Strategy.TOMORROW, Strategy.D25) for final_phase in (False, True)
    ) * 512 + 256 * 1024**2


__all__ = [
    "LearnedRankSampleCounts",
    "LearnedRankPartitionCoverage",
    "LearnedRankTrainingSource",
    "LearnedRankTrainingPlan",
    "BuildLearnedRankTrainingPlan",
    "BuildLearnedRankReadDates",
    "CanReuseLearnedRankPair",
    "LearnedRankCoverageFailures",
    "EstimateLearnedRankFittingBytes",
]
