"""Production profile training policy over explicitly injected I/O and fitting ports."""

from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Literal, Protocol

from trader.recommendation.domain.publication.models import Strategy
from trader.training.application.tomorrow_training import ProductionTrainingWindow, TomorrowTrainingProgress, TomorrowTrainingProgressPort
from trader.training.application.training_diagnostics import (
    BuildTrainingOperationIssue, NotifyTrainingCommit, TrainingIssueStage, TrainingOperationIssue,
)
from trader.training.domain.final_fit import FinalFitDatePlan
from trader.training.domain.profile_training import (
    BuildProfileTrainingPlan,
    PlannedTrainingHead,
    ProfileTrainingPlan,
    ProfileTrainingSource,
    ProfileTrainingDateRequirements,
)
from trader.training.domain.production_diagnostic_dates import ProductionDiagnosticDatePlan
from trader.training.domain.sample_coverage import SampleCoverage
from trader.training.domain.tomorrow_training_input import EvaluateTomorrowTrainingInput
from trader.training.domain.training_date_coverage import TrainingDateBasis, TrainingDateCoverage, TrainingDateCoverageMatches

from trader.training.application.error_diagnostics import CaptureTrainingError, TrainingErrorDiagnostic

TrainingStatus = Literal["blocked", "rejected", "engineering_ready"]


@dataclass(frozen=True)
class HeadTrainingResult:
    strategy: Strategy
    status: TrainingStatus
    training_input_scope: Literal["unavailable", "complete_manifest"]
    run_id: str | None
    training_input_hash: str
    training_input_codes: int
    training_universe_codes: int
    report_hash: str
    model_hash: str
    industry_count: int
    training_rows: int
    validation_rows: int
    failure_reasons: tuple[str, ...]
    label_cutoff: date | None = None
    sample_database_peak_bytes: int = 0
    coverage: tuple[SampleCoverage, ...] = ()
    final_fit: FinalFitDatePlan | None = None
    diagnostic_plan: ProductionDiagnosticDatePlan | None = None
    date_coverage: tuple[TrainingDateCoverage, ...] = ()
    diagnostic: TrainingErrorDiagnostic | None = None


@dataclass(frozen=True)
class TrainingRunResult:
    run_id: str | None
    training_input_hash: str
    sample_database_peak_bytes: int
    heads: tuple[HeadTrainingResult, ...]
    reused: bool = False
    post_commit_issues: tuple[TrainingOperationIssue, ...] = ()

    @property
    def status(self) -> TrainingStatus:
        statuses = tuple(item.status for item in self.heads)
        if "blocked" in statuses:
            return "blocked"
        if "rejected" in statuses:
            return "rejected"
        return "engineering_ready" if statuses else "blocked"


@dataclass(frozen=True)
class PreparedTrainingSamples:
    coverage: tuple[SampleCoverage, ...]
    peak_bytes: int
    date_coverage: tuple[TrainingDateCoverage, ...] = field(kw_only=True)


@dataclass(frozen=True)
class PreparedTrainingHead:
    result: HeadTrainingResult
    staged: bool


@dataclass(frozen=True)
class PublishedTrainingHead:
    result: HeadTrainingResult
    training_contract_hash: str


class ProfileTrainingOperationError(RuntimeError):
    """A boundary operation failed with a stable training failure reason."""


class ProfileFittingPort(Protocol):
    def fit_diagnostics(self) -> None: ...

    def prepare_head(self, head: PlannedTrainingHead) -> PreparedTrainingHead: ...

    def publish_pair(self) -> None: ...


class ProfileTrainingPort(Protocol):
    def read_source(self) -> ProfileTrainingSource: ...

    def date_upper_bounds(self, plan: ProfileTrainingPlan) -> tuple[TrainingDateCoverage, ...]: ...

    def has_industry_window(
        self, readable_dates: frozenset[date], anchors: frozenset[date] | None = None,
    ) -> bool: ...

    def industry_sample_upper_bounds(
        self, readable_dates: frozenset[date], anchors: frozenset[date],
    ) -> tuple[tuple[str, int], ...]: ...

    def prepare_samples(self, plan: ProfileTrainingPlan, window: ProductionTrainingWindow) -> PreparedTrainingSamples: ...

    def fitting(self, plan: ProfileTrainingPlan) -> AbstractContextManager[ProfileFittingPort]: ...

    def clear_samples(self) -> None: ...

    def sample_peak_bytes(self) -> int: ...


def InspectProfileReuse(
    source: ProfileTrainingSource, published: tuple[PublishedTrainingHead, ...],
) -> TrainingRunResult | None:
    if not source.target_matches:
        return None
    try:
        plan = BuildProfileTrainingPlan(source)
    except ValueError:
        return None
    if (
        tuple(head.result.strategy for head in published) != tuple(head.strategy for head in plan.heads)
        or len({head.result.run_id for head in published}) != 1
    ):
        return None
    for current, head in zip(published, plan.heads, strict=True):
        result = current.result
        if (
            result.status != "engineering_ready"
            or result.training_input_hash != source.training_input_hash
            or result.label_cutoff != head.label_cutoff
            or current.training_contract_hash != head.training_contract_hash
            or result.final_fit != head.final_fit
            or result.diagnostic_plan != plan.split
        ):
            return None
    return TrainingRunResult(published[0].result.run_id, source.training_input_hash, 0, tuple(
        head.result for head in published
    ), reused=True)


def TrainProfile(
    operations: ProfileTrainingPort, *, progress: TomorrowTrainingProgressPort | None = None,
) -> TrainingRunResult:
    source = operations.read_source()
    if not source.target_matches:
        return _blocked_run(source, "history_archive_repack_target_mismatch")
    try:
        plan = BuildProfileTrainingPlan(source)
    except ValueError as exc:
        return _blocked_run(source, str(exc))
    failures = EvaluateTomorrowTrainingInput(
        source.descriptor,
        expected_manifest_hash=source.snapshot_hash,
        expected_source_cutoff=source.source_cutoff,
    ).failure_reasons
    if failures:
        return _blocked_run(source, failures[0], plan=plan)
    date_coverage = operations.date_upper_bounds(plan)
    admission = InspectProfileDateAdmission(plan, date_coverage, basis="metadata_upper_bound")
    if admission is not None:
        return admission
    if progress is not None:
        progress.publish(TomorrowTrainingProgress("training_preparation", "completed", 1, 1))
    completed: list[HeadTrainingResult] = []
    coverage: tuple[SampleCoverage, ...] = ()
    peak_bytes = 0
    published = False
    post_commit_issues: list[TrainingOperationIssue] = []
    post_commit_stage: TrainingIssueStage = "scope_cleanup"
    try:
        window = _require_industry_coverage(operations, plan)
        samples = operations.prepare_samples(plan, window)
        peak_bytes = samples.peak_bytes
        coverage = _expected_coverage(plan.split, samples.coverage)
        date_coverage = (*date_coverage, *samples.date_coverage)
        admission = InspectProfileDateAdmission(plan, samples.date_coverage, basis="sample_index")
        if admission is not None:
            return replace(admission, sample_database_peak_bytes=peak_bytes, heads=tuple(
                replace(head, date_coverage=tuple(item for item in date_coverage if item.strategy is head.strategy),
                        coverage=tuple(item for item in coverage if item.target == (
                            "target_t1" if head.strategy is Strategy.TOMORROW else "target_d25_aggregate"
                        )))
                for head in admission.heads
            ))
        if any(item.effective_dates != item.expected_dates for item in coverage):
            raise ProfileTrainingOperationError("profile_training_effective_dates_insufficient")
        with operations.fitting(plan) as fitting:
            fitting.fit_diagnostics()
            prepared = []
            for head in plan.heads:
                item = fitting.prepare_head(head)
                prepared.append(item)
                completed.append(item.result)
            if len(prepared) == len(source.heads) and all(
                item.staged and item.result.status == "engineering_ready" for item in prepared
            ):
                try:
                    if progress is not None:
                        progress.publish(TomorrowTrainingProgress("artifact_publish", "started", 0, 1))
                    fitting.publish_pair()
                    published = True
                except ProfileTrainingOperationError as exc:
                    completed = [replace(item, status="blocked", model_hash="", failure_reasons=(str(exc),),
                                         diagnostic=CaptureTrainingError(exc)) for item in completed]
                if published:
                    issue = NotifyTrainingCommit(progress)
                    if issue is not None:
                        post_commit_issues.append(issue)
            else:
                completed = [
                    replace(item, status="blocked", model_hash="", failure_reasons=("profile_pair_incomplete",))
                    if item.status == "engineering_ready" else item
                    for item in completed
                ]
        if all(item.status != "blocked" for item in completed):
            post_commit_stage = "sample_cleanup"
            operations.clear_samples()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        if published:
            post_commit_issues.append(BuildTrainingOperationIssue(post_commit_stage, exc))
        elif not isinstance(exc, ProfileTrainingOperationError):
            raise
        else:
            peak_bytes = max(peak_bytes, operations.sample_peak_bytes())
            reason = str(exc)
            completed = [
                replace(item, status="blocked", model_hash="", failure_reasons=(reason,), diagnostic=CaptureTrainingError(exc))
                if item.status == "engineering_ready" else item
                for item in completed
            ]
            completed.extend(
                replace(
                    _blocked_head(source, head.strategy, reason),
                    run_id=plan.run_id,
                    label_cutoff=head.label_cutoff,
                    diagnostic=CaptureTrainingError(exc),
                    coverage=tuple(item for item in coverage if item.target == (
                        "target_t1" if head.strategy is Strategy.TOMORROW else "target_d25_aggregate"
                    )),
                )
                for head in plan.heads
                if not any(item.strategy is head.strategy for item in completed)
            )
    ordered = tuple(
        replace(
            next(item for item in completed if item.strategy is head.strategy), diagnostic_plan=plan.split,
            date_coverage=tuple(item for item in date_coverage if item.strategy is head.strategy),
        )
        for head in source.heads
    )
    return TrainingRunResult(
        plan.run_id, source.training_input_hash, peak_bytes, ordered, post_commit_issues=tuple(post_commit_issues),
    )


def InspectProfileDateAdmission(
    plan: ProfileTrainingPlan, coverage: tuple[TrainingDateCoverage, ...], *, basis: TrainingDateBasis,
) -> TrainingRunResult | None:
    requirements = ProfileTrainingDateRequirements(plan)
    if not TrainingDateCoverageMatches(requirements, coverage, basis=basis):
        return _blocked_run(plan.source, "profile_training_coverage_contract_invalid", plan=plan)
    if not any(item.missing_dates for item in coverage):
        return None
    heads = []
    for head in plan.heads:
        observed = tuple(item for item in coverage if item.strategy is head.strategy)
        reasons = tuple(dict.fromkeys(
            f"training_signal_dates_upper_bound_insufficient:{item.partition}"
            if item.basis == "metadata_upper_bound" else (
                "final_fit_effective_dates_insufficient" if item.partition == "final_fitting"
                else "profile_training_effective_dates_insufficient"
            )
            for item in observed if item.missing_dates
        )) or ("profile_pair_admission_blocked",)
        heads.append(replace(
            _blocked_head(plan.source, head.strategy, reasons[0]), run_id=plan.run_id,
            label_cutoff=head.label_cutoff, final_fit=head.final_fit, diagnostic_plan=plan.split,
            failure_reasons=reasons, date_coverage=observed,
        ))
    return TrainingRunResult(plan.run_id, plan.source.training_input_hash, 0, tuple(heads))


def _require_industry_coverage(
    operations: ProfileTrainingPort, plan: ProfileTrainingPlan,
) -> ProductionTrainingWindow:
    source, split = plan.source, plan.split
    window = ProductionTrainingWindow(split, tuple(day for day in source.calendar if day <= source.source_cutoff))
    if not operations.has_industry_window(window.readable_dates):
        raise ProfileTrainingOperationError("history_industry_coverage_insufficient")
    for anchors in (
        split.fitting_dates,
        *((split.early_stopping_dates,) if source.algorithm in {"lightgbm", "tcn"} else ()),
        split.calibration_dates,
        split.validation_dates,
    ):
        if not operations.has_industry_window(window.readable_dates, frozenset(anchors)):
            raise ProfileTrainingOperationError("history_industry_split_coverage_insufficient")
    bounds = operations.industry_sample_upper_bounds(window.readable_dates, frozenset(split.fitting_dates))
    if not any(count >= source.minimum_industry_training_rows for _, count in bounds):
        raise ProfileTrainingOperationError("history_industry_training_rows_insufficient")
    return window


def _expected_coverage(
    split: ProductionDiagnosticDatePlan, coverage: tuple[SampleCoverage, ...],
) -> tuple[SampleCoverage, ...]:
    dates = (
        ("training", split.fitting_dates),
        ("early_stopping", split.early_stopping_dates),
        ("calibration", split.calibration_dates),
        ("validation", split.validation_dates),
        ("holdout", ()),
    )
    expected = tuple(
        (target, partition)
        for target in ("target_t1", "target_d25_aggregate")
        for partition, _ in dates
    )
    observed = tuple((item.target, item.partition) for item in coverage)
    if len(observed) != len(expected) or frozenset(observed) != frozenset(expected):
        raise ProfileTrainingOperationError("profile_training_coverage_contract_invalid")
    return tuple(replace(item, expected_dates=next(len(days) for name, days in dates if name == item.partition)) for item in coverage)


def _blocked_head(source: ProfileTrainingSource, strategy: Strategy, reason: str) -> HeadTrainingResult:
    return HeadTrainingResult(
        strategy, "blocked", source.input_scope, None, source.training_input_hash, source.training_codes,
        source.universe_codes, "", "", 0, 0, 0, (reason,),
    )


def _blocked_run(
    source: ProfileTrainingSource, reason: str, *, plan: ProfileTrainingPlan | None = None,
) -> TrainingRunResult:
    return TrainingRunResult(None, source.training_input_hash, 0, tuple(
        replace(
            _blocked_head(source, head.strategy, reason),
            label_cutoff=None if plan is None else next(item.label_cutoff for item in plan.heads if item.strategy is head.strategy),
            diagnostic_plan=None if plan is None else plan.split,
        )
        for head in source.heads
    ))


__all__ = [
    "HeadTrainingResult",
    "InspectProfileReuse",
    "InspectProfileDateAdmission",
    "PreparedTrainingHead",
    "PreparedTrainingSamples",
    "ProfileFittingPort",
    "ProfileTrainingOperationError",
    "ProfileTrainingPort",
    "PublishedTrainingHead",
    "TrainProfile",
    "TrainingRunResult",
    "TrainingStatus",
]
