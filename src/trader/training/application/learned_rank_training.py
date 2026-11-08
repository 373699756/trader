"""Learned-rank training decisions over explicit sample, fitting and publication ports."""

from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Literal, Protocol

from trader.recommendation.domain.publication.models import Strategy
from trader.training.application.error_diagnostics import CaptureTrainingError, TrainingErrorDiagnostic
from trader.training.application.tomorrow_training import (
    TomorrowTrainingProgress, TomorrowTrainingProgressPort, TomorrowTrainingProgressState, TomorrowTrainingStage,
    TrainingFitCounts,
)
from trader.training.application.training_diagnostics import (
    BuildTrainingOperationIssue, NotifyTrainingCommit, TrainingIssueStage, TrainingOperationIssue,
)
from trader.training.domain.final_fit import FinalFitDatePlan
from trader.training.domain.training_date_coverage import TrainingDateBasis, TrainingDateCoverage, TrainingDateCoverageMatches
from trader.training.domain.v1.learned_rank import (
    LearnedRankDateSplit, LearnedRankFinalHead, LearnedRankFinalPair, LearnedRankHead, V1TrainingSettings,
)
from trader.training.domain.v1.training_plan import (
    BuildLearnedRankTrainingPlan, CanReuseLearnedRankPair, EstimateLearnedRankFittingBytes,
    LearnedRankCoverageFailures, LearnedRankPartitionCoverage, LearnedRankSampleCounts,
    LearnedRankTrainingPlan, LearnedRankTrainingSource,
    LearnedRankTrainingDateRequirements,
)


@dataclass(frozen=True)
class V1TrainingResult:
    status: Literal["engineering_ready", "blocked", "failed"]
    run_id: str
    training_input_hash: str
    sample_database_peak_bytes: int
    heads: tuple[LearnedRankFinalHead, ...] = ()
    sample_counts: LearnedRankSampleCounts | None = None
    failure_reasons: tuple[str, ...] = ()
    coverage: tuple[LearnedRankPartitionCoverage, ...] = ()
    reused: bool = False
    post_commit_issues: tuple[TrainingOperationIssue, ...] = ()
    date_coverage: tuple[TrainingDateCoverage, ...] = ()
    diagnostic: TrainingErrorDiagnostic | None = None


@dataclass(frozen=True)
class PreparedLearnedRankSamples:
    counts: LearnedRankSampleCounts
    coverage: tuple[LearnedRankPartitionCoverage, ...]
    peak_bytes: int
    date_coverage: tuple[TrainingDateCoverage, ...] = field(kw_only=True)


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

    def date_upper_bounds(self, plan: LearnedRankTrainingPlan) -> tuple[TrainingDateCoverage, ...]: ...

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
    date_coverage: tuple[TrainingDateCoverage, ...] = ()
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
            try:
                plan = BuildLearnedRankTrainingPlan(source, session.settings)
            except ValueError as exc:
                if str(exc) != "learned_rank_calendar_insufficient":
                    raise
                return V1TrainingResult("blocked", run_id, input_hash, peak_bytes, failure_reasons=(str(exc),))
            date_coverage = session.date_upper_bounds(plan)
            admission = InspectLearnedRankDateAdmission(plan, date_coverage, basis="metadata_upper_bound")
            if admission is not None:
                return admission
            with session.samples() as samples:
                prepared = samples.build(plan)
                counts, coverage, peak_bytes = prepared.counts, prepared.coverage, prepared.peak_bytes
                date_coverage = (*date_coverage, *prepared.date_coverage)
                failures = LearnedRankCoverageFailures(coverage, plan)
                if failures:
                    status: Literal["failed", "blocked"] = "failed" if failures == ("learned_rank_sample_coverage_contract_invalid",) else "blocked"
                    return V1TrainingResult(
                        status, run_id, input_hash, peak_bytes, (), counts, failures, coverage,
                        date_coverage=date_coverage,
                    )
                admission = InspectLearnedRankDateAdmission(plan, prepared.date_coverage, basis="sample_index")
                if admission is not None:
                    return V1TrainingResult(
                        admission.status, run_id, input_hash, peak_bytes, (), counts, admission.failure_reasons,
                        coverage, date_coverage=date_coverage,
                    )
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
            date_coverage=date_coverage,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        if published_heads is not None:
            return V1TrainingResult(
                "engineering_ready", run_id, input_hash, peak_bytes, published_heads, counts, coverage=coverage,
                post_commit_issues=(
                    *post_commit_issues, BuildTrainingOperationIssue(post_commit_stage, exc),
                ),
                date_coverage=date_coverage,
            )
        if not isinstance(exc, LearnedRankTrainingOperationError):
            raise
        return V1TrainingResult(
            "failed", run_id, input_hash, peak_bytes, (), counts, (str(exc),), coverage, date_coverage=date_coverage,
            diagnostic=CaptureTrainingError(exc),
        )


def InspectLearnedRankDateAdmission(
    plan: LearnedRankTrainingPlan, coverage: tuple[TrainingDateCoverage, ...], *, basis: TrainingDateBasis,
) -> V1TrainingResult | None:
    requirements = LearnedRankTrainingDateRequirements(plan)
    if not TrainingDateCoverageMatches(requirements, coverage, basis=basis):
        return V1TrainingResult(
            "failed", plan.source.identity_hash, plan.source.training_input_hash, 0,
            failure_reasons=("learned_rank_sample_coverage_contract_invalid",), date_coverage=coverage,
        )
    failures = tuple(
        f"training_signal_dates_upper_bound_insufficient:{item.strategy.value}:{item.partition}"
        if item.basis == "metadata_upper_bound" else (
            f"learned_rank_final_fit_dates_insufficient:{item.strategy.value}"
            if item.partition == "final_fitting"
            else f"learned_rank_sample_dates_insufficient:{item.strategy.value}:{item.partition}"
        )
        for item in coverage if item.missing_dates
    )
    return None if not failures else V1TrainingResult(
        "blocked", plan.source.identity_hash, plan.source.training_input_hash, 0,
        failure_reasons=failures, date_coverage=coverage,
    )


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
            fit_counts=TrainingFitCounts(completed, 1, completed, 0) if stage in {"model_fit", "final_model_refit"} else None,
        ))


__all__ = [
    "V1TrainingResult", "PreparedLearnedRankSamples", "LearnedRankTrainingOperationError",
    "LearnedRankFittingPort", "LearnedRankSamplesPort", "LearnedRankInspectionPort",
    "LearnedRankTrainingSessionPort", "LearnedRankTrainingPort",
    "InspectLearnedRankTraining", "TrainLearnedRank",
    "InspectLearnedRankDateAdmission",
]
