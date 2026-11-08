"""Learned-rank training decisions over explicit sample, fitting and publication ports."""

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Literal, Protocol

from trader.recommendation.domain.publication.models import Strategy
from trader.training.application.tomorrow_training import (
    TomorrowTrainingProgress, TomorrowTrainingProgressPort, TomorrowTrainingProgressState, TomorrowTrainingStage,
)
from trader.training.application.training_diagnostics import (
    BuildTrainingOperationIssue, NotifyTrainingCommit, TrainingIssueStage, TrainingOperationIssue,
)
from trader.training.domain.final_fit import FinalFitDatePlan
from trader.training.domain.v1.learned_rank import (
    LearnedRankDateSplit, LearnedRankFinalHead, LearnedRankFinalPair, LearnedRankHead, V1TrainingSettings,
)
from trader.training.domain.v1.training_plan import (
    BuildLearnedRankTrainingPlan, CanReuseLearnedRankPair, EstimateLearnedRankFittingBytes,
    LearnedRankCoverageFailures, LearnedRankPartitionCoverage, LearnedRankSampleCounts,
    LearnedRankTrainingPlan, LearnedRankTrainingSource,
)


@dataclass(frozen=True)
class V1TrainingResult:
    status: Literal["engineering_ready", "failed"]
    run_id: str
    training_input_hash: str
    sample_database_peak_bytes: int
    heads: tuple[LearnedRankFinalHead, ...] = ()
    sample_counts: LearnedRankSampleCounts | None = None
    failure_reasons: tuple[str, ...] = ()
    coverage: tuple[LearnedRankPartitionCoverage, ...] = ()
    reused: bool = False
    post_commit_issues: tuple[TrainingOperationIssue, ...] = ()


@dataclass(frozen=True)
class PreparedLearnedRankSamples:
    counts: LearnedRankSampleCounts
    coverage: tuple[LearnedRankPartitionCoverage, ...]
    peak_bytes: int


class LearnedRankTrainingOperationError(RuntimeError):
    """A resource boundary failed with the original stable failure classification."""


class LearnedRankFittingPort(Protocol):
    def fit_diagnostic(self, strategy: Strategy, split: LearnedRankDateSplit) -> LearnedRankHead: ...

    def refit(self, diagnostic: LearnedRankHead, plan: FinalFitDatePlan) -> LearnedRankFinalHead: ...


class LearnedRankSamplesPort(Protocol):
    def build(self, plan: LearnedRankTrainingPlan) -> PreparedLearnedRankSamples: ...

    def fitting(self, required_bytes: int) -> AbstractContextManager[LearnedRankFittingPort]: ...


class LearnedRankInspectionPort(Protocol):
    @property
    def source(self) -> LearnedRankTrainingSource: ...

    @property
    def settings(self) -> V1TrainingSettings: ...

    def check_cancelled(self) -> None: ...

    def read_pair(self) -> LearnedRankFinalPair | None: ...

    def validate_pair(self, pair: LearnedRankFinalPair) -> bool: ...


class LearnedRankTrainingSessionPort(LearnedRankInspectionPort, Protocol):
    @property
    def progress(self) -> TomorrowTrainingProgressPort | None: ...

    def samples(self) -> AbstractContextManager[LearnedRankSamplesPort]: ...

    def publish_pair(self, pair: LearnedRankFinalPair) -> None: ...


class LearnedRankTrainingPort(Protocol):
    def open(self) -> AbstractContextManager[LearnedRankTrainingSessionPort]: ...


def InspectLearnedRankTraining(session: LearnedRankInspectionPort) -> V1TrainingResult | None:
    session.check_cancelled()
    pair = session.read_pair()
    if pair is None:
        return None
    try:
        plan = BuildLearnedRankTrainingPlan(session.source, session.settings)
    except ValueError:
        return None
    if not CanReuseLearnedRankPair(pair, plan) or not session.validate_pair(pair):
        return None
    session.check_cancelled()
    return V1TrainingResult(
        "engineering_ready", session.source.identity_hash, session.source.training_input_hash, 0, pair.heads, reused=True,
    )


def TrainLearnedRank(operations: LearnedRankTrainingPort) -> V1TrainingResult:
    run_id = input_hash = ""
    counts: LearnedRankSampleCounts | None = None
    peak_bytes = 0
    coverage: tuple[LearnedRankPartitionCoverage, ...] = ()
    published_heads: tuple[LearnedRankFinalHead, ...] | None = None
    post_commit_issues: list[TrainingOperationIssue] = []
    post_commit_stage: TrainingIssueStage = "scope_cleanup"
    try:
        with operations.open() as session:
            source = session.source
            run_id, input_hash = source.identity_hash, source.training_input_hash
            reused = InspectLearnedRankTraining(session)
            if reused is not None:
                return reused
            plan = BuildLearnedRankTrainingPlan(source, session.settings)
            with session.samples() as samples:
                prepared = samples.build(plan)
                counts, coverage, peak_bytes = prepared.counts, prepared.coverage, prepared.peak_bytes
                failures = LearnedRankCoverageFailures(coverage, plan)
                if failures:
                    return V1TrainingResult("failed", run_id, input_hash, peak_bytes, (), counts, failures, coverage)
                heads = []
                with samples.fitting(EstimateLearnedRankFittingBytes(coverage)) as fitting:
                    diagnostics = []
                    for strategy, split in zip((Strategy.TOMORROW, Strategy.D25), plan.splits, strict=True):
                        session.check_cancelled()
                        _progress(session.progress, "model_fit", "started", strategy)
                        diagnostics.append(fitting.fit_diagnostic(strategy, split))
                        _progress(session.progress, "model_fit", "completed", strategy)
                    for diagnostic, final_fit in zip(diagnostics, plan.final_fit_plans, strict=True):
                        _progress(session.progress, "final_model_refit", "started", diagnostic.strategy)
                        heads.append(fitting.refit(diagnostic, final_fit))
                        _progress(session.progress, "final_model_refit", "completed", diagnostic.strategy)
                _progress(session.progress, "artifact_publish", "started", None)
                pair = LearnedRankFinalPair(
                    source.training_input_hash, source.source_identity_hash, source.calendar_hash, source.source_cutoff,
                    plan.settings, (heads[0], heads[1]), run_id, source.calendar,
                    price_snapshot_hash=source.snapshot_hash, industry_snapshot=source.industry_snapshot,
                )
                session.check_cancelled()
                session.publish_pair(pair)
                published_heads = pair.heads
                issue = NotifyTrainingCommit(session.progress)
                if issue is not None:
                    post_commit_issues.append(issue)
                post_commit_stage = "sample_cleanup"
            post_commit_stage = "scope_cleanup"
        if published_heads is None:
            raise LearnedRankTrainingOperationError("learned_rank_publication_incomplete")
        return V1TrainingResult(
            "engineering_ready", run_id, input_hash, peak_bytes, published_heads, counts, coverage=coverage,
            post_commit_issues=tuple(post_commit_issues),
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        if published_heads is not None:
            return V1TrainingResult(
                "engineering_ready", run_id, input_hash, peak_bytes, published_heads, counts, coverage=coverage,
                post_commit_issues=(
                    *post_commit_issues, BuildTrainingOperationIssue(post_commit_stage, exc),
                ),
            )
        if not isinstance(exc, LearnedRankTrainingOperationError):
            raise
        return V1TrainingResult("failed", run_id, input_hash, peak_bytes, (), counts, (str(exc),), coverage)


def _progress(
    progress: TomorrowTrainingProgressPort | None,
    stage: TomorrowTrainingStage,
    state: TomorrowTrainingProgressState,
    strategy: Strategy | None,
) -> None:
    if progress is not None:
        completed = int(state == "completed")
        progress.publish(TomorrowTrainingProgress(
            stage, state, completed, 1, produced_units=completed if stage in {"model_fit", "final_model_refit"} else 0,
            strategy=strategy,
        ))


__all__ = [
    "V1TrainingResult", "PreparedLearnedRankSamples", "LearnedRankTrainingOperationError",
    "LearnedRankFittingPort", "LearnedRankSamplesPort", "LearnedRankInspectionPort",
    "LearnedRankTrainingSessionPort", "LearnedRankTrainingPort",
    "InspectLearnedRankTraining", "TrainLearnedRank",
]
