"""V1 admission and pair publication are independent of SQL and native fitters."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, timedelta
from typing import Literal

import pytest

from trader.recommendation.domain.market.v1.features import LEARNED_RANK_FEATURE_IDS
from trader.recommendation.domain.publication.models import Strategy
from tests.helpers.training.current_industry import CurrentIndustrySnapshotFixture
from trader.training.application.learned_rank_training import (
    LearnedRankFittingPort,
    LearnedRankSamplesPort,
    LearnedRankTrainingOperationError,
    LearnedRankTrainingSessionPort,
    PreparedLearnedRankSamples,
    TrainLearnedRank,
)
from trader.training.domain.final_fit import FinalFitDatePlan
from trader.training.domain.v1.learned_rank import (
    LearnedRankContractHash,
    LearnedRankDateSplit,
    LearnedRankFinalContractHash,
    LearnedRankFinalHead,
    LearnedRankFinalPair,
    LearnedRankHead,
    LearnedRankPredictionMetrics,
    V1TrainingSettings,
)
from trader.training.domain.v1.training_plan import (
    BuildLearnedRankReadDates,
    BuildLearnedRankTrainingPlan,
    LearnedRankPartitionCoverage,
    LearnedRankSampleCounts,
    LearnedRankTrainingPlan,
    LearnedRankTrainingSource,
)
from trader.training.entrypoints.profile_training import ProjectLearnedRankTrainingResult


def _settings() -> V1TrainingSettings:
    return V1TrainingSettings(
        fit_dates=30,
        early_stopping_dates=5,
        calibration_dates=5,
        validation_dates=5,
        embargo_dates=5,
        minimum_training_rows=10,
        learning_rate=0.1,
        max_depth=3,
        num_leaves=4,
        minimum_leaf_rows=2,
        boosting_rounds=3,
        early_stopping_rounds=1,
        maximum_bins=31,
        histogram_pool_mib=8,
        seed=7,
        ridge_penalty=1.0,
        ranking_round_trip_cost=0.01,
    )


class _Training:
    def __init__(
        self, *, missing_coverage: bool = False, error: type[Exception] | None = None, coverage_case: str | None = None,
        cleanup_error: type[Exception] | None = None, scope_error: type[Exception] | None = None,
    ) -> None:
        calendar = tuple(date(2024, 1, 1) + timedelta(days=index) for index in range(340))
        self.source = LearnedRankTrainingSource(
            "a" * 64, "b" * 64, "c" * 64, calendar[-3], calendar, "d" * 64,
            industry_snapshot=CurrentIndustrySnapshotFixture(("600001",)),
        )
        self.settings = _settings()
        self.progress = None
        self.events: list[str] = []
        self.scopes: list[str] = []
        self.pair: LearnedRankFinalPair | None = None
        self.missing_coverage = missing_coverage
        self.error = error
        self.required_bytes = 0
        self.coverage_case = coverage_case
        self.cleanup_error = cleanup_error
        self.scope_error = scope_error
        self.published_calls = 0

    @contextmanager
    def open(self) -> Iterator[LearnedRankTrainingSessionPort]:
        self.scopes.append("source")
        try:
            yield self
        finally:
            self.scopes.remove("source")
            if self.scope_error is not None:
                raise self.scope_error("private source scope detail")

    def check_cancelled(self) -> None:
        pass

    def read_pair(self) -> LearnedRankFinalPair | None:
        return self.pair

    def validate_pair(self, pair: LearnedRankFinalPair) -> bool:
        self.events.append("validated")
        return pair is self.pair

    @contextmanager
    def samples(self) -> Iterator[LearnedRankSamplesPort]:
        self.events.append("samples_open")
        self.scopes.append("samples")
        try:
            yield self
        finally:
            self.scopes.remove("samples")
            self.events.append("samples_closed")
            if self.cleanup_error is not None:
                raise self.cleanup_error("private sample cleanup detail")

    def build(self, plan: LearnedRankTrainingPlan) -> PreparedLearnedRankSamples:
        coverage = []
        for strategy, split, final_fit in zip(
            (Strategy.TOMORROW, Strategy.D25), plan.splits, plan.final_fit_plans, strict=True,
        ):
            partitions: tuple[
                tuple[Literal["fitting", "early_stopping", "calibration", "validation", "final_fitting"], tuple[date, ...]], ...
            ] = (
                ("fitting", split.fitting), ("early_stopping", split.early_stopping),
                ("calibration", split.calibration), ("validation", split.validation),
                ("final_fitting", final_fit.fit_dates),
            )
            for partition, days in partitions:
                coverage.append(LearnedRankPartitionCoverage(strategy, partition, len(days), len(days), len(days) * 10))
        if self.missing_coverage:
            coverage[-1] = replace(coverage[-1], effective_dates=coverage[-1].effective_dates - 1)
        if self.coverage_case == "empty":
            coverage = []
        elif self.coverage_case == "missing_head":
            coverage = [item for item in coverage if item.strategy is Strategy.TOMORROW]
        elif self.coverage_case == "missing_partition":
            coverage = coverage[1:]
        elif self.coverage_case == "duplicate":
            coverage.append(coverage[0])
        elif self.coverage_case == "duplicate_missing":
            coverage[-1] = coverage[0]
        elif self.coverage_case == "unknown_head":
            coverage[0] = replace(coverage[0], strategy=Strategy.LONG)
        elif self.coverage_case == "unknown_partition":
            coverage[0] = replace(coverage[0], partition="unknown")
        elif self.coverage_case == "forged_fitting_dates":
            coverage[0] = replace(coverage[0], expected_dates=1, effective_dates=1)
        elif self.coverage_case == "forged_final_dates":
            coverage[-1] = replace(coverage[-1], expected_dates=1, effective_dates=1)
        elif self.coverage_case == "wrong_expected_dates":
            coverage[-1] = replace(coverage[-1], expected_dates=1)
        return PreparedLearnedRankSamples(LearnedRankSampleCounts(3400, 900, 0, 0, 0, 0, 0), tuple(coverage), 123)

    @contextmanager
    def fitting(self, required_bytes: int) -> Iterator[LearnedRankFittingPort]:
        self.events.append("fitting_open")
        self.scopes.append("fitting")
        self.required_bytes = required_bytes
        try:
            yield self
        finally:
            self.scopes.remove("fitting")
            self.events.append("fitting_closed")

    def fit_diagnostic(self, strategy: Strategy, split: LearnedRankDateSplit) -> LearnedRankHead:
        self.events.append(f"diagnostic:{strategy.value}")
        width = len(LEARNED_RANK_FEATURE_IDS)
        return LearnedRankHead(
            strategy, LearnedRankContractHash(self.settings), split, (0.0,) * width, (1.0,) * width,
            (0.0,) * width, 0.0, 0.0, 1.0, "controlled-native-model", 1, 300, 50, 50,
            ("capco:industry",), LearnedRankPredictionMetrics(50, 50, 10, 5, 1, 0.1, 0.1, 0.0, 0.5),
        )

    def refit(self, diagnostic: LearnedRankHead, plan: FinalFitDatePlan) -> LearnedRankFinalHead:
        self.events.append(f"final:{diagnostic.strategy.value}")
        if diagnostic.strategy is Strategy.D25 and self.error is not None:
            raise self.error("controlled_refit_failure")
        return LearnedRankFinalHead(
            diagnostic.strategy,
            LearnedRankFinalContractHash(self.settings),
            diagnostic.split,
            diagnostic.transformer_means,
            diagnostic.transformer_scales,
            diagnostic.ridge_coefficients,
            diagnostic.ridge_intercept,
            diagnostic.calibration_intercept,
            diagnostic.calibration_slope,
            diagnostic.rank_model,
            diagnostic.rank_iterations,
            diagnostic.training_rows,
            diagnostic.early_stopping_rows,
            diagnostic.calibration_rows,
            diagnostic.industries,
            diagnostic.validation,
            plan,
            diagnostic,
        )

    def publish_pair(self, pair: LearnedRankFinalPair) -> None:
        assert self.scopes == ["source", "samples"]
        self.events.append("published")
        self.published_calls += 1
        self.pair = pair


def test_complete_pair_is_published_after_fitting_resources_are_released() -> None:
    training = _Training()

    result = TrainLearnedRank(training)

    assert result.status == "engineering_ready" and training.pair is not None
    assert result.heads == training.pair.heads
    assert training.events == [
        "samples_open", "fitting_open", "diagnostic:tomorrow", "diagnostic:d25", "final:tomorrow", "final:d25",
        "fitting_closed", "published", "samples_closed",
    ]
    assert training.scopes == []
    assert training.required_bytes == 450 * 512 + 256 * 1024**2


class _CompletionFailure:
    def publish(self, event):
        if event.stage == "artifact_publish" and event.state == "completed":
            raise BrokenPipeError("private completion detail")


@pytest.mark.parametrize(("failure", "stage"), (
    ("notification", "completion_notification"), ("samples", "sample_cleanup"), ("source", "scope_cleanup"),
))
def test_post_commit_operation_failures_preserve_v1_heads_and_are_public_diagnostics(failure: str, stage: str) -> None:
    training = _Training(
        cleanup_error=OSError if failure == "samples" else None,
        scope_error=OSError if failure == "source" else None,
    )
    if failure == "notification":
        training.progress = _CompletionFailure()

    result = TrainLearnedRank(training)

    assert result.status == "engineering_ready" and training.pair is not None
    assert result.heads == training.pair.heads and not result.failure_reasons
    assert training.published_calls == 1 and training.scopes == []
    assert len(result.post_commit_issues) == 1
    assert result.post_commit_issues[0].stage == stage
    payload = ProjectLearnedRankTrainingResult(result)
    assert payload["post_commit_issues"][0]["stage"] == stage
    assert len(payload["heads"]) == 2
    assert "private" not in str(payload)

    training.cleanup_error = training.scope_error = None
    reused = TrainLearnedRank(training)
    assert reused.reused and reused.heads == result.heads
    assert training.published_calls == 1


def test_incomplete_final_dates_block_both_heads_and_publication() -> None:
    training = _Training(missing_coverage=True)

    result = TrainLearnedRank(training)

    assert result.status == "failed" and result.heads == ()
    assert result.failure_reasons == ("learned_rank_final_fit_dates_insufficient:d25",)
    assert training.events == ["samples_open", "samples_closed"]
    assert training.scopes == [] and training.pair is None


@pytest.mark.parametrize("case", (
    "empty", "missing_head", "missing_partition", "duplicate", "duplicate_missing", "unknown_head", "unknown_partition",
))
def test_incomplete_or_ambiguous_v1_coverage_never_enters_fitting(case: str) -> None:
    training = _Training(coverage_case=case)

    result = TrainLearnedRank(training)

    assert result.status == "failed" and result.heads == ()
    assert result.failure_reasons == ("learned_rank_sample_coverage_contract_invalid",)
    assert training.events == ["samples_open", "samples_closed"]
    assert training.scopes == [] and training.pair is None


@pytest.mark.parametrize(("case", "reason"), (
    ("forged_fitting_dates", "learned_rank_sample_dates_insufficient:tomorrow:fitting"),
    ("forged_final_dates", "learned_rank_final_fit_dates_insufficient:d25"),
    ("wrong_expected_dates", "learned_rank_final_fit_dates_insufficient:d25"),
))
def test_v1_coverage_counts_are_admitted_against_the_actual_plan(case: str, reason: str) -> None:
    training = _Training(coverage_case=case)

    result = TrainLearnedRank(training)

    assert result.status == "failed" and result.heads == ()
    assert result.failure_reasons == (reason,)
    assert training.events == ["samples_open", "samples_closed"]
    assert training.scopes == [] and training.pair is None


@pytest.mark.parametrize("error", (LearnedRankTrainingOperationError, InterruptedError))
def test_second_final_head_failure_releases_all_resources_without_publishing(error: type[Exception]) -> None:
    training = _Training(error=error)

    if error is InterruptedError:
        with pytest.raises(InterruptedError, match="controlled_refit_failure"):
            TrainLearnedRank(training)
    else:
        result = TrainLearnedRank(training)
        assert result.status == "failed" and result.heads == ()
        assert result.failure_reasons == ("controlled_refit_failure",)
    assert training.events[-2:] == ["fitting_closed", "samples_closed"]
    assert training.scopes == [] and training.pair is None


def test_exact_reuse_validates_pair_without_sample_or_fitting_scopes() -> None:
    training = _Training()
    original = TrainLearnedRank(training)
    training.events.clear()

    result = TrainLearnedRank(training)

    assert result.reused and result.heads == original.heads and result.run_id == original.run_id
    assert training.events == ["validated"] and training.scopes == []
    assert result.sample_database_peak_bytes == 0


def test_read_window_keeps_warmup_and_excludes_future_calendar_dates() -> None:
    training = _Training()
    plan = BuildLearnedRankTrainingPlan(training.source, training.settings)

    dates = BuildLearnedRankReadDates(training.source.calendar, training.source.source_cutoff, training.settings)

    earliest = min(day for split in plan.splits for day in split.sample_dates)
    first_position = training.source.calendar.index(earliest) - training.settings.history_sessions
    assert min(dates) == training.source.calendar[first_position]
    assert max(dates) == training.source.source_cutoff
    assert all(day in dates for split in plan.splits for day in split.sample_dates)
    assert all(day in dates for final_fit in plan.final_fit_plans for day in final_fit.fit_dates)
