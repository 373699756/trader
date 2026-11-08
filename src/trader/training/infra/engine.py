"""Deterministic one-scan training engine shared by trained scoring profiles."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy
from trader.download.domain.current_industry import CurrentIndustrySnapshot
from trader.training.domain.training_input_identity import TRAINING_INDUSTRY_POLICY
from trader.training.application.resources import TrainingSampleSettings
from trader.training.application.error_diagnostics import CaptureTrainingError
from trader.training.application.profile_training import (
    HeadTrainingResult, InspectProfileDateAdmission, InspectProfileReuse, PreparedTrainingHead, PreparedTrainingSamples, ProfileFittingPort,
    ProfileTrainingOperationError, PublishedTrainingHead, TrainProfile, TrainingRunResult, TrainingStatus,
)
from trader.training.domain.profile_training import (
    BuildProfileDiagnosticPlan, BuildProfileTrainingPlan, PlannedTrainingHead, ProfileTrainingHead, ProfileTrainingPlan, ProfileTrainingSource,
    ProfileTrainingDateRequirements,
)
from trader.training.domain.training_date_coverage import TrainingDateCoverage

import json
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import date
from importlib.metadata import version
from pathlib import Path
from typing import Protocol, cast

from lightgbm.basic import LightGBMError

from trader.download.domain.history_control import HistorySnapshot
from trader.download.domain.history_revision import HistoryTrainingRow
from trader.download.infra.history_control_index import (
    HistoryMaintenanceAlreadyRunningError,
    HistoryMaintenanceLock,
)
from trader.download.infra.history_partition_repack import (
    HistoryPartitionRepackFenceError,
    RequireHistoryRepackIdle,
)
from trader.infra.serialization.canonical import ContentHash
from trader.recommendation.domain.market.history_alignment import HISTORY_ALIGNMENT_CONTRACT
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.residualization import TRAINED_HEAD_EXPOSURE_CONTRACT
from trader.training.application.resources import TrainingCpuAdmission, TrainingExecutionObserver, TrainingMemoryAdmission
from trader.training.application.tomorrow_training import (
    TOMORROW_TRAINING_COMPUTE_THREADS,
    TomorrowTrainingProgress,
    TomorrowTrainingProgressPort,
    ProductionTrainingWindow,
)
from trader.training.domain.final_fit import BuildFinalFitDatePlan, FINAL_FIT_CONTRACT, FinalFitDatePlan
from trader.training.domain.tomorrow_training_input import FrozenDailyInputDescriptor
from trader.training.domain.production_diagnostic_dates import (
    PRODUCTION_DIAGNOSTIC_CONTRACT,
    ProductionDiagnosticDatePlan,
)
from trader.training.infra.history.history_training_input import (
    HistoryTrainingInputError,
    HistoryTrainingInputSnapshot,
    SQLiteHistoryTrainingInputSnapshot,
)
from trader.training.infra.implementation_identity import TrainingImplementationIdentity
from trader.training.infra.execution_options import TrainingExecutionOptions
from trader.training.infra.fitting_resources import ReserveTrainingFittingResources, TrainingFittingReservation
from trader.training.infra.model_bundles.bundle_snapshot import (
    MakeBundleStagingDirectory,
)
from trader.training.infra.model_bundles.contracts import TrainedHeadContract, TrainedProfileContract
from trader.training.infra.model_bundles.documents import (
    BuildIndustryModelDocument,
    BuildIndustrySampleCountsDocument,
    BuildPredictionMetricsDocument,
    BuildTargetMetricDocument,
)
from trader.training.infra.model_bundles.profile_publication import (
    ClearProfileStaging,
    InspectPublishedProfileBundles,
    PublishProfileBundles,
    RecoverProfileBundles,
)
from trader.training.infra.model_evaluation import RECENT_PREDICTION_EVALUATION_CONTRACT
from trader.training.infra.model_fitting import FitIndustryModels, LightGBMFittingParameters, RefitIndustryModels
from trader.training.infra.model_fitting_results import FittedIndustryModel, IndustryFitResult
from trader.training.infra.model_bundles.final_fit_evidence import BuildFinalFitEvidence
from trader.training.infra.profile.v5.configuration import TcnConfigurationDocument
from trader.training.infra.profile.v5.settings import TcnFittingParameters
from trader.training.infra.sample_builder import (
    BuildTrainingSamples,
    TrainingHistorySource,
    TrainingSampleBuildRequest,
)
from trader.training.infra.sample_cache import PruneEmptySampleDirectories, SampleCacheBytes, TrainingSampleCache
from trader.training.infra.sample_index import (
    FinalFitIndustryCounts,
    SQLiteTrainingSampleIndex,
    TargetMetric,
)
from trader.training.infra.sample_schema import TrainingFeatureNames
from trader.training.infra.sample_sequences import SEQUENCE_FORMAT, SampleSequenceLayout
from trader.training.infra.training_settings import ProfileTrainingSettings
from trader.training.infra.training_industry_metadata import ProjectTrainingIndustrySnapshot
from trader.training.infra.training_contract import BuildTrainingContractHash

class _TrainingInputSnapshot(TrainingHistorySource, Protocol):
    snapshot: HistoryTrainingInputSnapshot

    def industry_sample_upper_bounds(
        self, allowed_dates: frozenset[date], required_sessions: int, anchors: frozenset[date]
    ) -> tuple[tuple[str, int], ...]: ...

    @property
    def history_root(self) -> Path: ...

    @property
    def published_snapshot(self) -> HistorySnapshot: ...

    def describe_frozen_daily_input(self) -> FrozenDailyInputDescriptor: ...


@dataclass(frozen=True)
class _HeadPlan:
    contract: TrainedHeadContract
    label_cutoff: date
    training_contract_hash: str


@dataclass(frozen=True)
class _TrainingRunContext:
    profile: TrainedProfileContract
    contract: TrainedHeadContract
    snapshot: HistoryTrainingInputSnapshot
    label_cutoff: date
    training_input_document_hash: str
    split: ProductionDiagnosticDatePlan
    split_hash: str
    training_contract_hash: str
    training_rows: int
    validation_rows: int
    training_run_hash: str


@dataclass(frozen=True)
class ProfileTrainingRequest:
    history_root: Path
    train_root: Path
    profile: TrainedProfileContract
    contracts: tuple[TrainedHeadContract, ...]
    settings: ProfileTrainingSettings
    sample_root: Path = field(kw_only=True)
    sample_settings: TrainingSampleSettings = field(kw_only=True)
    industry_snapshot: CurrentIndustrySnapshot = field(kw_only=True)
    progress: TomorrowTrainingProgressPort | None = None
    expected_history_snapshot_hash: str | None = None
    memory_admission: TrainingMemoryAdmission | None = None
    cpu_admission: TrainingCpuAdmission | None = None
    compute_threads: int = field(default=TOMORROW_TRAINING_COMPUTE_THREADS, kw_only=True)
    execution_options: TrainingExecutionOptions = field(default_factory=TrainingExecutionOptions, kw_only=True)
    memory_capacity_bytes: int | None = field(default=None, kw_only=True)
    execution_observer: TrainingExecutionObserver | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        strategies = tuple(contract.strategy for contract in self.contracts)
        self.settings.require_profile(self.profile)
        if type(self.compute_threads) is not int or self.compute_threads < 1:
            raise ValueError("training_cpu_request_invalid")
        if self.memory_capacity_bytes is not None and (
            type(self.memory_capacity_bytes) is not int or self.memory_capacity_bytes < 1
        ):
            raise ValueError("training_memory_capacity_invalid")
        if (
            self.contracts != self.profile.heads
            or len(set(strategies)) != len(strategies)
            or any(contract != self.profile.head_for_strategy(contract.strategy) for contract in self.contracts)
            or (self.profile.algorithm == "lightgbm") != isinstance(self.settings.fitting, LightGBMFittingParameters)
            or (self.profile.algorithm == "tcn") != isinstance(self.settings.fitting, TcnFittingParameters)
        ):
            raise ValueError("profile training request contains a foreign or duplicate head contract")


@dataclass(frozen=True)
class _HeadFitRequest:
    plan: _HeadPlan
    profile: TrainedProfileContract
    settings: ProfileTrainingSettings
    snapshot: HistoryTrainingInputSnapshot
    split: ProductionDiagnosticDatePlan
    metrics: tuple[TargetMetric, ...]
    train_root: Path
    run_id: str
    progress: TomorrowTrainingProgressPort | None
    peak_bytes: int
    final_fit_plan: FinalFitDatePlan


@dataclass(frozen=True)
class _TrainingInputDocumentContext:
    profile: TrainedProfileContract
    snapshot: HistoryTrainingInputSnapshot
    label_cutoff: date
    training_contract_hash: str
    split_hash: str
    contract: TrainedHeadContract
    training_run_hash: str


def RunProfileTraining(request: ProfileTrainingRequest, *, sqlite_policy: SqliteIoPolicy) -> TrainingRunResult:
    return _run_training(
        replace(
            request,
            train_root=request.train_root / request.profile.output_directory,
            sample_root=request.sample_root / request.profile.output_directory,
        ), sqlite_policy=sqlite_policy
    )


def RunRepackProfileTraining(request: ProfileTrainingRequest, *, sqlite_policy: SqliteIoPolicy) -> TrainingRunResult:
    if request.expected_history_snapshot_hash is None:
        raise ValueError("repack training requires an expected history snapshot hash")
    return RunProfileTraining(request, sqlite_policy=sqlite_policy)


def _run_training(request: ProfileTrainingRequest, *, sqlite_policy: SqliteIoPolicy) -> TrainingRunResult:
    _publish(request.progress, TomorrowTrainingProgress("resource_preflight", "completed", 1, 1))
    _publish(request.progress, TomorrowTrainingProgress("input_read", "started", 0, 1))
    try:
        history = SQLiteHistoryTrainingInputSnapshot.open(
            request.history_root, industry_snapshot=request.industry_snapshot, sqlite_policy=sqlite_policy,
        )
    except (HistoryTrainingInputError, OSError, ValueError) as exc:
        heads = tuple(_unavailable_result(item, _reason(exc)) for item in request.contracts)
        return TrainingRunResult(None, "", 0, heads)
    _publish(request.progress, TomorrowTrainingProgress("input_read", "completed", 1, 1))
    _publish(request.progress, TomorrowTrainingProgress("training_preparation", "started", 0, 1))
    try:
        # Full-history maintenance and either training profile are deliberately
        # serialized: concurrent multi-gigabyte scans make a rotating disk seek
        # bound even though the published snapshot itself is immutable.
        training_lock = request.train_root.parent / f".{request.profile.profile_id}.training.lock"
        with (
            HistoryMaintenanceLock(history.history_root.with_name(".history.lock")),
            HistoryMaintenanceLock(training_lock),
        ):
            history = SQLiteHistoryTrainingInputSnapshot.open(
                request.history_root, industry_snapshot=request.industry_snapshot, sqlite_policy=sqlite_policy,
            )
            try:
                RequireHistoryRepackIdle(history.history_root)
            except HistoryPartitionRepackFenceError:
                if request.expected_history_snapshot_hash != history.snapshot.snapshot_hash:
                    raise
            return _run_profile_training(history, request, sqlite_policy=sqlite_policy)
    except HistoryMaintenanceAlreadyRunningError:
        heads = tuple(
            _blocked_from_snapshot(item, history.snapshot, "history_maintenance_running") for item in request.contracts
        )
        return TrainingRunResult(None, history.snapshot.training_input_hash, 0, heads)
    except HistoryPartitionRepackFenceError:
        heads = tuple(
            _blocked_from_snapshot(item, history.snapshot, "history_archive_repack_activation_pending")
            for item in request.contracts
        )
        return TrainingRunResult(None, history.snapshot.training_input_hash, 0, heads)


def FitSelectedProfile(
    history: _TrainingInputSnapshot,
    request: ProfileTrainingRequest,
    cache: TrainingSampleCache,
    aligned_rows: Iterator[HistoryTrainingRow], *, sqlite_policy: SqliteIoPolicy,
) -> TrainingRunResult:
    """Caller owns history/profile locks; connections belong exclusively to this worker."""
    return _run_profile_training(history, request, prepared_cache=cache, aligned_rows=aligned_rows, sqlite_policy=sqlite_policy)


def InspectSelectedProfile(
    history: _TrainingInputSnapshot,
    request: ProfileTrainingRequest, *, sqlite_policy: SqliteIoPolicy,
) -> tuple[ProfileTrainingRequest, TrainingSampleCache, frozenset[date], TrainingRunResult | None]:
    request = replace(
        request,
        train_root=request.train_root / request.profile.output_directory,
        sample_root=request.sample_root / request.profile.output_directory,
    )
    split, reason = _prepare_locked_split(history.snapshot, request.profile, request.settings)
    if split is None:
        raise ValueError(reason)
    cache = _sample_cache_for(history.snapshot, split, request, sqlite_policy=sqlite_policy)
    reused = _reuse_profile(history, request)
    if reused is None:
        plan = BuildProfileTrainingPlan(_read_profile_source(history, request))
        bounds = history.snapshot.date_upper_bounds(
            ProfileTrainingDateRequirements(plan), history_sessions=request.profile.history_sessions,
            history_excludes_signal=request.profile.algorithm == "tcn",
        )
        blocked = InspectProfileDateAdmission(plan, bounds, basis="metadata_upper_bound")
        if blocked is not None:
            return request, cache, frozenset(), blocked
    dates = frozenset() if reused is not None or cache.inspect() else _production_window(history.snapshot, split).readable_dates
    return request, cache, dates, reused


def _run_profile_training(
    history: _TrainingInputSnapshot,
    request: ProfileTrainingRequest,
    *,
    prepared_cache: TrainingSampleCache | None = None,
    aligned_rows: Iterator[HistoryTrainingRow] | None = None,
    sqlite_policy: SqliteIoPolicy,
) -> TrainingRunResult:
    return TrainProfile(
        _ProfileTrainingAdapter(history, request, sqlite_policy, prepared_cache, aligned_rows),
        progress=request.progress,
    )


@contextmanager
def _training_errors() -> Iterator[None]:
    try:
        yield
    except (InterruptedError, ProfileTrainingOperationError):
        raise
    except (HistoryTrainingInputError, LightGBMError, OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        raise ProfileTrainingOperationError(_reason(exc)) from exc


@dataclass
class _ProfileTrainingAdapter:
    history: _TrainingInputSnapshot
    request: ProfileTrainingRequest
    sqlite_policy: SqliteIoPolicy
    cache: TrainingSampleCache | None
    aligned_rows: Iterator[HistoryTrainingRow] | None
    metrics: tuple[TargetMetric, ...] = ()
    final_counts: tuple[tuple[TrainedHeadContract, tuple[FinalFitIndustryCounts, ...]], ...] = ()

    def read_source(self) -> ProfileTrainingSource:
        with _training_errors():
            self.history, target_matches = _reopen_expected_snapshot(
                self.history, self.request.expected_history_snapshot_hash, sqlite_policy=self.sqlite_policy,
            )
            return _read_profile_source(self.history, self.request, target_matches=target_matches)

    def date_upper_bounds(self, plan: ProfileTrainingPlan) -> tuple[TrainingDateCoverage, ...]:
        return self.history.snapshot.date_upper_bounds(
            ProfileTrainingDateRequirements(plan), history_sessions=self.request.profile.history_sessions,
            history_excludes_signal=self.request.profile.algorithm == "tcn",
        )

    def has_industry_window(
        self, readable_dates: frozenset[date], anchors: frozenset[date] | None = None,
    ) -> bool:
        with _training_errors():
            if anchors is None:
                return self.history.has_industry_window(readable_dates, 1)
            return self.history.has_industry_window(readable_dates, 1, anchor_dates=anchors)

    def industry_sample_upper_bounds(
        self, readable_dates: frozenset[date], anchors: frozenset[date],
    ) -> tuple[tuple[str, int], ...]:
        with _training_errors():
            return self.history.industry_sample_upper_bounds(readable_dates, 1, anchors)

    def prepare_samples(
        self, plan: ProfileTrainingPlan, window: ProductionTrainingWindow,
    ) -> PreparedTrainingSamples:
        with _training_errors():
            request, snapshot = self.request, self.history.snapshot
            request.train_root.mkdir(parents=True, exist_ok=True)
            ClearProfileStaging(request.train_root)
            inspected = self.cache is not None
            if self.cache is None:
                self.cache = _sample_cache_for(snapshot, plan.split, request, sqlite_policy=self.sqlite_policy)
            cache = self.cache
            _publish(request.progress, TomorrowTrainingProgress("sample_cache", "started", 0, 1))
            with cache.open(inspected=inspected) as samples:
                _publish(request.progress, TomorrowTrainingProgress("sample_cache", "completed", 1, 1, int(cache.reused)))
                self.metrics = _prepare_samples(
                    TrainingSampleBuildRequest(
                        self.history, snapshot.training_codes, window, samples, request.profile, request.progress,
                    ),
                    cache, aligned_rows=self.aligned_rows, observer=request.execution_observer,
                )
                final_inspected = tuple(
                    (contract, samples.inspect_final_fit(contract, head.final_fit))
                    for contract, head in zip(request.contracts, plan.heads, strict=True)
                )
                self.final_counts = tuple((contract, item.industries) for contract, item in final_inspected)
                coverage = tuple(item for contract in request.contracts for item in samples.coverage(contract))
                dates = []
                for requirement in ProfileTrainingDateRequirements(plan):
                    contract = next(item for item in request.contracts if item.strategy is requirement.strategy)
                    if requirement.partition == "final_fitting":
                        available = next(item.available_dates for selected, item in final_inspected if selected is contract)
                    else:
                        partition = "training" if requirement.partition == "fitting" else requirement.partition
                        gaps = next(item.missing for item in coverage if (
                            item.target == contract.target_column and item.partition == partition
                        ))
                        missing = frozenset(item.trade_date for item in gaps)
                        available = tuple(day for day in requirement.required_dates if day not in missing)
                    dates.append(requirement.observe(available, "sample_index"))
                return PreparedTrainingSamples(coverage, samples.database_size_bytes, date_coverage=tuple(dates))

    def _require_cache(self) -> TrainingSampleCache:
        if self.cache is None:
            raise RuntimeError("profile_training_samples_not_prepared")
        return self.cache

    def _memory_requirement(self, samples: SQLiteTrainingSampleIndex) -> int:
        request, cache = self.request, self._require_cache()
        counts = tuple(item for contract in request.contracts for item in samples.industry_counts(contract))
        matrix_bytes = max((
            (item.training + item.early_stopping + item.calibration) * (cache.feature_count + 1) * 8
            for item in counts
        ), default=0)
        final_matrix_bytes = max((
            item.rows * (len(contract.feature_positions) + 1) * 8
            for contract, counts in self.final_counts for item in counts
        ), default=0)
        matrix_bytes = max(matrix_bytes, final_matrix_bytes)
        estimate = (
            512 * 1024**2 + final_matrix_bytes * 2 if request.profile.algorithm == "tcn"
            else 256 * 1024**2 + matrix_bytes * 4
        )
        if request.execution_options.shared_validation:
            estimate += (512 if request.profile.algorithm == "tcn" else 256) * 1024**2
        if request.execution_options.shared_matrix_memory:
            capacity = request.memory_capacity_bytes or 0
            extra = min(samples.shared_matrix_memory_bytes(), max(0, capacity - estimate))
            samples.configure_shared_matrix_budget(extra)
            estimate += extra
        return estimate

    @contextmanager
    def fitting(self, plan: ProfileTrainingPlan) -> Iterator[ProfileFittingPort]:
        with _training_errors():
            request, cache = self.request, self._require_cache()
            with (
                SQLiteTrainingSampleIndex(
                    cache.path, cache.feature_names, prepared_split_hash=plan.split.content_hash,
                    sequence_layout=cache.sequence_layout, sqlite_policy=self.sqlite_policy,
                    sample_settings=request.sample_settings, execution_options=request.execution_options,
                ) as samples,
                ExitStack() as resources,
            ):
                requested_threads = (
                    request.settings.fitting.compute_threads
                    if isinstance(request.settings.fitting, TcnFittingParameters) else request.compute_threads
                )
                reservation = resources.enter_context(ReserveTrainingFittingResources(
                    request.cpu_admission, request.memory_admission, requested_threads,
                    lambda: self._memory_requirement(samples),
                    lambda: _publish(request.progress, TomorrowTrainingProgress("model_fit", "running", 0, 1)),
                    memory_first=request.execution_options.memory_first,
                ))
                yield _ProfileFittingAdapter(self, plan, samples, resources, reservation)

    def clear_samples(self) -> None:
        with _training_errors():
            self._require_cache().clear()
            PruneEmptySampleDirectories(self.request.sample_root, self.request.sample_root.parent)

    def sample_peak_bytes(self) -> int:
        return SampleCacheBytes(self.cache)


@dataclass
class _ProfileFittingAdapter:
    owner: _ProfileTrainingAdapter
    plan: ProfileTrainingPlan
    samples: SQLiteTrainingSampleIndex
    resources: ExitStack
    reservation: TrainingFittingReservation
    fitted: tuple[IndustryFitResult, ...] = ()
    staged: list[tuple[Strategy, Path]] = field(default_factory=list)

    def fit_diagnostics(self) -> None:
        with _training_errors():
            request = self.owner.request
            self.fitted = FitIndustryModels(
                self.samples, self.plan.split, request.contracts, request.settings.fitting,
                progress=request.progress, compute_threads=self.reservation.compute_threads,
                industry_snapshot=self.owner.history.snapshot.industry_snapshot,
            )

    def prepare_head(self, head: PlannedTrainingHead) -> PreparedTrainingHead:
        with _training_errors():
            request, snapshot = self.owner.request, self.owner.history.snapshot
            position = next(index for index, contract in enumerate(request.contracts) if contract.strategy is head.strategy)
            contract, fitted = request.contracts[position], self.fitted[position]
            refitted = RefitIndustryModels(
                self.samples, contract, head.final_fit, fitted, request.settings.fitting,
                compute_threads=self.reservation.compute_threads, progress=request.progress,
                industry_snapshot=snapshot.industry_snapshot,
            )
            result, staging = _fit_and_prepare_head(
                _HeadFitRequest(
                    _HeadPlan(contract, head.label_cutoff, head.training_contract_hash),
                    request.profile, request.settings, snapshot, self.plan.split, self.owner.metrics,
                    request.train_root, self.plan.run_id, request.progress, self.owner.sample_peak_bytes(), head.final_fit,
                ),
                fitted, refitted,
            )
            if staging is not None:
                if not self.staged:
                    self.resources.callback(ClearProfileStaging, request.train_root)
                self.staged.append((head.strategy, staging))
            return PreparedTrainingHead(result, staging is not None)

    def publish_pair(self) -> None:
        with _training_errors():
            request = self.owner.request
            if request.execution_options.release_cpu_before_publish:
                self.reservation.release_cpu()
            PublishProfileBundles(request.train_root, request.profile, tuple(self.staged))


def _sample_cache_for(
    snapshot: HistoryTrainingInputSnapshot,
    split: ProductionDiagnosticDatePlan,
    request: ProfileTrainingRequest, *, sqlite_policy: SqliteIoPolicy,
) -> TrainingSampleCache:
    feature_names = TrainingFeatureNames(request.profile.heads)
    feature_count = len(feature_names)
    sequence_layout = SampleSequenceLayout(6, 60, 6) if request.profile.algorithm == "tcn" else None
    cache_identity = ContentHash(
        (
            TrainingImplementationIdentity(),
            snapshot.training_input_hash,
            snapshot.input_descriptor_hash,
            request.profile.profile_id,
            split.content_hash,
            feature_count,
            SEQUENCE_FORMAT if sequence_layout else "monthly_samples",
            HISTORY_ALIGNMENT_CONTRACT,
            RECENT_PREDICTION_EVALUATION_CONTRACT,
            PRODUCTION_DIAGNOSTIC_CONTRACT,
            FINAL_FIT_CONTRACT,
            tuple(_final_fit_plan(snapshot, request.settings, contract).content_hash for contract in request.contracts),
            request.profile.history_sessions,
            request.profile.momentum_horizons,
            request.profile.market_state_momentum_horizons,
            request.profile.raw_feature_manifest.content_hash,
            TRAINED_HEAD_EXPOSURE_CONTRACT,
            tuple(
                (contract.feature_manifest.content_hash, contract.label_target, contract.maturity_sessions)
                for contract in request.contracts
            ),
        )
    )
    return TrainingSampleCache(
        request.sample_root,
        cache_identity,
        feature_names,
        split.content_hash,
        sequence_layout=sequence_layout,
        sample_settings=request.sample_settings,
        progress=lambda phase, state, done, total: _publish(
            request.progress,
            TomorrowTrainingProgress(
                "sample_cache_check" if phase == "checking" else "sample_cache_seal", state, done, total
            ),
        ), sqlite_policy=sqlite_policy,
    )


def _prepare_samples(
    request: TrainingSampleBuildRequest,
    cache: TrainingSampleCache,
    *,
    aligned_rows: Iterator[HistoryTrainingRow] | None = None,
    observer: TrainingExecutionObserver | None = None,
) -> tuple[TargetMetric, ...]:
    if not cache.reused:
        with observer.sample_build(request.profile.profile_id) if observer is not None else nullcontext():
            BuildTrainingSamples(request, aligned_rows=aligned_rows)
    if request.index.count() == 0:
        raise ValueError("profile_training_rows_empty")
    if not cache.reused:
        cache.seal(request.index)
    _publish(request.progress, TomorrowTrainingProgress("target_statistics", "started", 0, 1))
    metrics = request.index.validation_target_metrics()
    _publish(request.progress, TomorrowTrainingProgress("target_statistics", "completed", 1, 1))
    return metrics


def _production_window(snapshot: HistoryTrainingInputSnapshot, split: ProductionDiagnosticDatePlan) -> ProductionTrainingWindow:
    return ProductionTrainingWindow(
        split, tuple(day for day in snapshot.calendar.open_dates if day <= snapshot.source_cutoff),
    )


def _final_fit_plan(
    snapshot: HistoryTrainingInputSnapshot, settings: ProfileTrainingSettings, contract: TrainedHeadContract,
) -> FinalFitDatePlan:
    return BuildFinalFitDatePlan(
        snapshot.calendar.open_dates, source_cutoff=snapshot.source_cutoff,
        fit_sessions=settings.fit_dates, maturity_sessions=contract.maturity_sessions,
    )


def _reopen_expected_snapshot(
    history: _TrainingInputSnapshot,
    expected_snapshot_hash: str | None, *, sqlite_policy: SqliteIoPolicy,
) -> tuple[_TrainingInputSnapshot, bool]:
    if expected_snapshot_hash is None:
        return history, True
    reopened = SQLiteHistoryTrainingInputSnapshot.open(
        history.history_root, industry_snapshot=history.snapshot.industry_snapshot, sqlite_policy=sqlite_policy,
    )
    return reopened, reopened.snapshot.snapshot_hash == expected_snapshot_hash


def _prepare_locked_split(
    snapshot: HistoryTrainingInputSnapshot,
    profile: TrainedProfileContract,
    settings: ProfileTrainingSettings,
) -> tuple[ProductionDiagnosticDatePlan | None, str]:
    try:
        split = BuildProfileDiagnosticPlan(
            snapshot.calendar.open_dates,
            source_cutoff=snapshot.source_cutoff,
            fit_sessions=settings.fit_dates,
            history_sessions=profile.history_sessions,
            algorithm=profile.algorithm,
        )
    except ValueError as exc:
        return None, _reason(exc)
    return split, ""


def _fit_and_prepare_head(
    request: _HeadFitRequest, fitted: IndustryFitResult, refitted: tuple[FittedIndustryModel, ...],
) -> tuple[HeadTrainingResult, Path | None]:
    plan = request.plan
    snapshot = request.snapshot
    contract = plan.contract
    staging: Path | None = None
    try:
        models, training_rows, validation_rows = refitted, sum(item.training_rows for item in refitted), fitted.validation_rows
        split_hash = _head_split_hash(request.split, contract)
        training_input = _training_input_document(
            _TrainingInputDocumentContext(
                request.profile,
                snapshot,
                plan.label_cutoff,
                plan.training_contract_hash,
                split_hash,
                contract,
                request.run_id,
            )
        )
        input_document_hash = cast(str, training_input["content_hash"])
        context = _TrainingRunContext(
            request.profile,
            contract,
            snapshot,
            plan.label_cutoff,
            input_document_hash,
            request.split,
            split_hash,
            plan.training_contract_hash,
            fitted.training_rows,
            validation_rows,
            request.run_id,
        )
        diagnostic_model = BuildIndustryModelDocument(fitted.models)
        diagnostic_hash = ContentHash(diagnostic_model)
        diagnostic_model["content_hash"] = diagnostic_hash
        model = BuildIndustryModelDocument(refitted)
        model_hash = ContentHash(model)
        model["content_hash"] = model_hash
        report = _report_document(context, fitted, diagnostic_hash, request.metrics)
        report_hash = ContentHash(report)
        report["content_hash"] = report_hash
        if not report["artifact_structure_valid"]:
            return HeadTrainingResult(
                contract.strategy,
                "rejected",
                snapshot.input_scope,
                request.run_id,
                snapshot.training_input_hash,
                len(snapshot.training_codes),
                snapshot.universe_count,
                report_hash,
                "",
                len(models),
                training_rows,
                validation_rows,
                tuple(cast(list[str], report["artifact_structure_failure_reasons"])),
                label_cutoff=plan.label_cutoff,
                sample_database_peak_bytes=request.peak_bytes,
                final_fit=request.final_fit_plan,
                diagnostic_plan=request.split,
            ), None
        metadata = _metadata_document(context, model_hash, fitted, training_input, report)
        metadata["final_fit"] = BuildFinalFitEvidence(
            request.final_fit_plan, request.split, diagnostic_model, model_hash, refitted,
            snapshot.calendar.open_dates,
        )
        if isinstance(request.settings.fitting, TcnFittingParameters):
            metadata["training_configuration"] = TcnConfigurationDocument(
                request.settings.fit_dates, request.settings.fitting
            )
        metadata["content_hash"] = ContentHash(metadata)
        staging = MakeBundleStagingDirectory(request.train_root)
        _write_json(staging / "model.json", model)
        _write_json(staging / "metadata.json", metadata)
        result = HeadTrainingResult(
            contract.strategy,
            "engineering_ready",
            snapshot.input_scope,
            request.run_id,
            snapshot.training_input_hash,
            len(snapshot.training_codes),
            snapshot.universe_count,
            report_hash,
            model_hash,
            len(models),
            training_rows,
            validation_rows,
            (),
            label_cutoff=plan.label_cutoff,
            sample_database_peak_bytes=request.peak_bytes,
            final_fit=request.final_fit_plan,
            diagnostic_plan=request.split,
        )
        return result, staging
    except InterruptedError:
        if staging is not None:
            ClearProfileStaging(request.train_root)
        raise
    except (LightGBMError, OSError, TypeError, ValueError, RuntimeError, sqlite3.Error) as exc:
        if staging is not None:
            ClearProfileStaging(request.train_root)
        return replace(
            _blocked_from_snapshot(contract, snapshot, _reason(exc)),
            diagnostic=CaptureTrainingError(exc),
            run_id=request.run_id,
            label_cutoff=plan.label_cutoff,
            sample_database_peak_bytes=request.peak_bytes,
            diagnostic_plan=request.split,
        ), None


def _report_document(
    context: _TrainingRunContext,
    fitted: IndustryFitResult,
    model_hash: str,
    metrics: tuple[TargetMetric, ...],
) -> dict[str, object]:
    structure_reasons = (
        [] if fitted.models and context.validation_rows > 0 else ["industry_model_validation_insufficient"]
    )
    selected = (
        {
            item.target: BuildTargetMetricDocument(item)
            for item in metrics
            if item.target in {"target_t2", "target_t3", "target_t4", "target_t5", "target_d25_aggregate"}
        }
        if context.contract.strategy is Strategy.D25
        else {item.target: BuildTargetMetricDocument(item) for item in metrics if item.target == "target_t1"}
    )
    document: dict[str, object] = {
        **_common_document(context),
        "schema_version": f"{context.profile.profile_id}_head_validation_report",
        "profile_id": context.profile.profile_id,
        "model_id": context.contract.model_id,
        "strategy_head": context.contract.strategy.value,
        "training_input_document_hash": context.training_input_document_hash,
        "model_hash": model_hash,
        "training_anchor": "15:00_close_proxy",
        "runtime_anchor": context.contract.runtime_anchor,
        "point_in_time_parity": False,
        "historical_status": "historical_data_insufficient",
        "historical_failure_reasons": ["daily_close_proxy_not_point_in_time"],
        "industry_count": len(fitted.models),
        "training_rows": context.training_rows,
        "validation_rows": context.validation_rows,
        "target_metrics": selected,
        "artifact_structure_valid": not structure_reasons,
        "artifact_structure_failure_reasons": structure_reasons,
        "prediction_return_validation_passed": False,
        "prediction_return_failure_reasons": ["daily_close_proxy_not_point_in_time"],
        "automatic_model_update": False,
    }
    document.update(BuildPredictionMetricsDocument(fitted, evaluation_contract=RECENT_PREDICTION_EVALUATION_CONTRACT))
    return document


def _metadata_document(
    context: _TrainingRunContext,
    model_hash: str,
    fitted: IndustryFitResult,
    training_input: dict[str, object],
    report: dict[str, object],
) -> dict[str, object]:
    return {
        "schema_version": f"{context.profile.profile_id}_head_recent_refit_metadata",
        "profile_id": context.profile.profile_id,
        "model_id": context.contract.model_id,
        "strategy_head": context.contract.strategy.value,
        "feature_ids": list(context.contract.feature_manifest.names),
        "feature_units": list(context.contract.feature_manifest.units),
        "exposure_contract": {
            "market": True,
            "board": True,
            "industry": True,
            "log_average_amount_20d": True,
            "order": list(TRAINED_HEAD_EXPOSURE_CONTRACT.order),
        },
        "algorithm": context.profile.algorithm,
        "dependencies": (
            {"lightgbm": version("lightgbm"), "numpy": version("numpy")}
            if context.profile.algorithm == "lightgbm"
            else {"torch": version("torch"), "numpy": version("numpy")}
            if context.profile.algorithm == "tcn"
            else {"numpy": version("numpy")}
        ),
        "training_input": training_input,
        "validation_report": report,
        "industry_sample_counts": BuildIndustrySampleCountsDocument(fitted),
        "model_hash": model_hash,
    }


def _training_input_document(context: _TrainingInputDocumentContext) -> dict[str, object]:
    profile = context.profile
    snapshot = context.snapshot
    contract = context.contract
    document: dict[str, object] = {
        "schema_version": f"{profile.profile_id}_head_training_input_manifest",
        "profile_id": profile.profile_id,
        "strategy_head": contract.strategy.value,
        "training_input_scope": snapshot.input_scope,
        "training_input_hash": snapshot.training_input_hash,
        "price_snapshot_hash": snapshot.snapshot_hash,
        "industry_policy": TRAINING_INDUSTRY_POLICY,
        "industry_content_hash": snapshot.industry_snapshot.content_hash,
        "industry_snapshot": ProjectTrainingIndustrySnapshot(snapshot.industry_snapshot),
        "training_run_hash": context.training_run_hash,
        "label_cutoff": context.label_cutoff.isoformat(),
        "source_identity_hash": snapshot.source_identity_hash,
        "calendar_hash": snapshot.calendar_hash,
        "source_cutoff": snapshot.source_cutoff.isoformat(),
        "requested_sessions": len(snapshot.calendar.open_dates),
        "input_descriptor_hash": snapshot.input_descriptor_hash,
        "training_input_codes": len(snapshot.training_codes),
        "training_universe_codes": snapshot.universe_count,
        "codes": list(snapshot.training_codes),
        "feature_manifest_hash": contract.feature_manifest.content_hash,
        "split_hash": context.split_hash,
        "training_contract_hash": context.training_contract_hash,
        "label_target": contract.label_target,
        "training_cost_bps": 0,
        "validation_scope": "daily_close_engineering_proxy",
        "production_authority": False,
    }
    document["content_hash"] = ContentHash(document)
    return document


def _common_document(context: _TrainingRunContext) -> dict[str, object]:
    snapshot = context.snapshot
    return {
        "training_input_scope": snapshot.input_scope,
        "training_input_hash": snapshot.training_input_hash,
        "price_snapshot_hash": snapshot.snapshot_hash,
        "industry_policy": TRAINING_INDUSTRY_POLICY,
        "industry_content_hash": snapshot.industry_snapshot.content_hash,
        "training_run_hash": context.training_run_hash,
        "label_cutoff": context.label_cutoff.isoformat(),
        "source_identity_hash": snapshot.source_identity_hash,
        "training_input_codes": len(snapshot.training_codes),
        "training_universe_codes": snapshot.universe_count,
        "feature_manifest_hash": context.contract.feature_manifest.content_hash,
        "split_hash": context.split_hash,
        "training_contract_hash": context.training_contract_hash,
        "label_target": context.contract.label_target,
        "training_cost_bps": 0,
        "validation_scope": "daily_close_engineering_proxy",
        "production_authority": False,
    }


def _read_profile_source(
    history: _TrainingInputSnapshot, request: ProfileTrainingRequest, *, target_matches: bool = True,
) -> ProfileTrainingSource:
    snapshot = history.snapshot
    return ProfileTrainingSource(
        request.profile.profile_id, request.profile.algorithm, snapshot.calendar.open_dates,
        snapshot.source_cutoff, snapshot.snapshot_hash, snapshot.input_scope,
        len(snapshot.training_codes), snapshot.universe_count, request.settings.fit_dates,
        request.profile.history_sessions, request.settings.fitting.minimum_industry_training_rows,
        tuple(ProfileTrainingHead(
            contract.strategy, contract.maturity_sessions,
            BuildTrainingContractHash(request.profile, contract, request.settings, compute_threads=request.compute_threads),
        ) for contract in request.contracts),
        history.describe_frozen_daily_input(), target_matches,
        industry_content_hash=snapshot.industry_snapshot.content_hash,
    )


def _reuse_profile(history: _TrainingInputSnapshot, request: ProfileTrainingRequest) -> TrainingRunResult | None:
    RecoverProfileBundles(request.train_root, request.profile)
    try:
        bundles = InspectPublishedProfileBundles(request.train_root, request.profile)
    except (OSError, ValueError, RuntimeError):
        return None
    heads: list[PublishedTrainingHead] = []
    for bundle, contract in zip(bundles, request.contracts, strict=True):
        model = bundle.model
        heads.append(
            PublishedTrainingHead(HeadTrainingResult(
                contract.strategy,
                "engineering_ready",
                "complete_manifest",
                bundle.training_run_hash,
                bundle.training_input_hash,
                model.training_input_codes,
                model.training_universe_codes,
                bundle.report_hash,
                bundle.model_hash,
                len(model.industries),
                model.training_rows,
                model.validation_rows,
                (),
                bundle.label_cutoff,
                final_fit=model.final_fit,
                diagnostic_plan=model.diagnostic_plan,
            ), bundle.training_contract_hash)
        )
    return InspectProfileReuse(_read_profile_source(history, request), tuple(heads))


def _head_split_hash(split: ProductionDiagnosticDatePlan, contract: TrainedHeadContract) -> str:
    return ContentHash(
        {
            "calendar_split_hash": split.content_hash,
            "strategy_head": contract.strategy.value,
            "maturity_sessions": contract.maturity_sessions,
            "label_target": contract.label_target,
        }
    )


def _base_result(
    contract: TrainedHeadContract,
    status: TrainingStatus,
    snapshot: HistoryTrainingInputSnapshot,
) -> HeadTrainingResult:
    return HeadTrainingResult(
        contract.strategy,
        status,
        snapshot.input_scope,
        None,
        snapshot.training_input_hash,
        len(snapshot.training_codes),
        snapshot.universe_count,
        "",
        "",
        0,
        0,
        0,
        (),
    )


def _blocked_from_snapshot(
    contract: TrainedHeadContract,
    snapshot: HistoryTrainingInputSnapshot,
    reason: str,
) -> HeadTrainingResult:
    return replace(_base_result(contract, "blocked", snapshot), failure_reasons=(reason,))


def _unavailable_result(contract: TrainedHeadContract, reason: str) -> HeadTrainingResult:
    return HeadTrainingResult(contract.strategy, "blocked", "unavailable", None, "", 0, 0, "", "", 0, 0, 0, (reason,))


def _publish(progress: TomorrowTrainingProgressPort | None, update: TomorrowTrainingProgress) -> None:
    if progress is not None:
        progress.publish(update)


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


def _reason(exc: BaseException) -> str:
    if str(exc).startswith("training_memory_request_exceeds_budget:"):
        return str(exc)
    if isinstance(exc, sqlite3.Error):
        return "training_database_failed"
    value = str(exc).lower()
    if value in {
        "history_manifest_unavailable",
        "history_manifest_parent_unavailable",
        "profile_training_rows_empty",
        "profile_training_effective_dates_insufficient",
        "history_alignment_calendar_mismatch",
        "history_alignment_source_day_missing",
        "history_alignment_duplicate_row",
        "history_alignment_row_outside_calendar",
        "history_industry_coverage_insufficient",
        "history_industry_split_coverage_insufficient",
        "history_industry_training_rows_insufficient",
        "history_training_data_incomplete",
        "training_split_unavailable",
        "final_fit_effective_dates_insufficient",
        "final_fit_industry_training_rows_insufficient",
        "final_fit_industry_counts_changed",
        "production_diagnostic_calendar_insufficient",
        "production_diagnostic_calendar_or_settings_invalid",
        "production_diagnostic_date_plan_invalid",
    }:
        return value
    return (
        "history_manifest_unavailable" if "manifest" in value or "no such file" in value else "profile_training_failed"
    )


__all__ = [
    "FitSelectedProfile",
    "InspectSelectedProfile",
    "ProfileTrainingRequest",
    "RunProfileTraining",
    "RunRepackProfileTraining",
]
