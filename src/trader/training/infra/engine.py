"""Deterministic one-scan training engine shared by trained scoring profiles."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from datetime import date
from importlib.metadata import version
from pathlib import Path
from typing import Literal, Protocol, cast

from lightgbm.basic import LightGBMError

from trader.download.domain.history_control import HistoryActiveSnapshot
from trader.download.infra.history_control_index import (
    HistoryMaintenanceAlreadyRunningError,
    HistoryMaintenanceLock,
)
from trader.download.infra.history_partition_repack import (
    HistoryPartitionRepackFenceError,
    RequireHistoryPartitionRepackInactive,
)
from trader.download.infra.history_storage_profile import ClassifyHistoryStorage
from trader.infra.serialization.canonical import ContentHash
from trader.recommendation.domain.market.history_alignment import HISTORY_ALIGNMENT_CONTRACT
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.residualization import TRAINED_HEAD_EXPOSURE_CONTRACT
from trader.training.application.tomorrow_training import (
    TOMORROW_TRAINING_COMPUTE_THREADS,
    TOMORROW_TRAINING_PEAK_RSS_MIB,
    TomorrowTrainingProgress,
    TomorrowTrainingProgressPort,
    TomorrowTrainingWindow,
)
from trader.training.domain.tomorrow_training_input import EvaluateTomorrowTrainingInput, FrozenDailyInputDescriptor
from trader.training.domain.training_dates import BuildTrainingDateSplit, TrainingDateSplit
from trader.training.infra.history.history_training_input import (
    HistoryTrainingInputError,
    HistoryTrainingInputSnapshot,
    SQLiteHistoryTrainingInputSnapshot,
)
from trader.training.infra.implementation_identity import TrainingImplementationIdentity
from trader.training.infra.joint_fit_evidence import LoadJointFitEvidence
from trader.training.infra.model_bundles.bundle_snapshot import (
    MakeBundleStagingDirectory,
)
from trader.training.infra.model_bundles.contracts import TrainedHeadContract, TrainedProfileContract
from trader.training.infra.model_bundles.documents import BuildIndustryModelDocument, BuildPredictionMetricsDocument
from trader.training.infra.model_bundles.profile_publication import InspectActiveProfileBundles, PublishProfileBundles
from trader.training.infra.model_fitting import FitIndustryModels, FittingParameterDocument, LightGBMFittingParameters
from trader.training.infra.profile.v5.settings import TcnFittingParameters
from trader.training.infra.sample_builder import (
    BuildJointTrainingSamples,
    BuildTrainingSamples,
    TrainingHistorySource,
    TrainingSampleBuildRequest,
)
from trader.training.infra.sample_cache import PruneEmptySampleDirectories, SampleCacheBytes, TrainingSampleCache
from trader.training.infra.sample_index import (
    TRAINING_SAMPLE_CACHE_MIB,
    TRAINING_SAMPLE_MMAP_BYTES,
    SQLiteTrainingSampleIndex,
    TargetMetric,
    TrainingSplitName,
)
from trader.training.infra.training_settings import ProfileTrainingSettings

TrainingStatus = Literal["blocked", "rejected", "engineering_ready"]


class _TrainingInputSnapshot(TrainingHistorySource, Protocol):
    snapshot: HistoryTrainingInputSnapshot

    def industry_sample_upper_bounds(
        self, allowed_dates: frozenset[date], required_sessions: int, anchors: frozenset[date]
    ) -> tuple[tuple[str, int], ...]: ...

    @property
    def archive_root(self) -> Path: ...

    @property
    def active_snapshot(self) -> HistoryActiveSnapshot: ...

    def describe_frozen_daily_input(self) -> FrozenDailyInputDescriptor: ...


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


@dataclass(frozen=True)
class TrainingRunResult:
    run_id: str | None
    training_input_hash: str
    sample_database_peak_bytes: int
    heads: tuple[HeadTrainingResult, ...]

    @property
    def status(self) -> TrainingStatus:
        statuses = tuple(item.status for item in self.heads)
        if "blocked" in statuses:
            return "blocked"
        if "rejected" in statuses:
            return "rejected"
        return "engineering_ready" if statuses else "blocked"


@dataclass(frozen=True)
class JointTrainingRunResult:
    profiles: tuple[TrainingRunResult, TrainingRunResult]
    history_scan_passes: int
    history_rows_read: int
    fit_mode: Literal["serial", "parallel"]
    preparation_seconds: float
    fit_seconds: float
    reused: bool = False

    @property
    def status(self) -> TrainingStatus:
        statuses = tuple(profile.status for profile in self.profiles)
        if "blocked" in statuses:
            return "blocked"
        if "rejected" in statuses:
            return "rejected"
        return "engineering_ready"


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
    split: TrainingDateSplit
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
    progress: TomorrowTrainingProgressPort | None = None
    expected_history_snapshot_hash: str | None = None

    def __post_init__(self) -> None:
        strategies = tuple(contract.strategy for contract in self.contracts)
        self.settings.require_profile(self.profile)
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
    split: TrainingDateSplit
    samples: SQLiteTrainingSampleIndex
    metrics: tuple[TargetMetric, ...]
    train_root: Path
    run_id: str
    progress: TomorrowTrainingProgressPort | None
    peak_bytes: int


@dataclass(frozen=True)
class _TrainingInputDocumentContext:
    profile: TrainedProfileContract
    snapshot: HistoryTrainingInputSnapshot
    label_cutoff: date
    training_contract_hash: str
    split_hash: str
    contract: TrainedHeadContract
    training_run_hash: str


def RunProfileTraining(request: ProfileTrainingRequest) -> TrainingRunResult:
    return _run_training(
        replace(
            request,
            train_root=request.train_root / request.profile.output_directory,
            sample_root=request.sample_root / request.profile.output_directory,
        )
    )


def RunRepackProfileTraining(request: ProfileTrainingRequest) -> TrainingRunResult:
    if request.expected_history_snapshot_hash is None:
        raise ValueError("repack training requires an expected history snapshot hash")
    return RunProfileTraining(request)


def RunJointProfileTraining(
    requests: tuple[ProfileTrainingRequest, ProfileTrainingRequest],
    *,
    parallel_fit: bool,
    reuse_matching: bool = False,
) -> JointTrainingRunResult:
    """Build both profile caches from one published scan, then fit under the same history lock."""

    if {request.profile.profile_id for request in requests} != {"v2", "v3"}:
        raise ValueError("joint_training_requires_v2_v3")
    if requests[0].history_root != requests[1].history_root or requests[0].train_root != requests[1].train_root:
        raise ValueError("joint_training_roots_mismatch")
    normalized = tuple(
        replace(
            request,
            train_root=request.train_root / request.profile.output_directory,
            sample_root=request.sample_root / request.profile.output_directory,
        )
        for request in requests
    )
    started = time.monotonic()
    try:
        archive = SQLiteHistoryTrainingInputSnapshot.open(requests[0].history_root)
    except (HistoryTrainingInputError, OSError, ValueError) as exc:
        unavailable = tuple(
            TrainingRunResult(None, "", 0, tuple(_unavailable_result(head, _reason(exc)) for head in request.contracts))
            for request in normalized
        )
        return JointTrainingRunResult(
            cast(tuple[TrainingRunResult, TrainingRunResult], unavailable), 0, 0, "serial", 0, 0
        )
    try:
        with ExitStack() as locks:
            locks.enter_context(HistoryMaintenanceLock(archive.archive_root.with_name(".history.lock")))
            for request in normalized:
                request.train_root.mkdir(parents=True, exist_ok=True)
                locks.enter_context(
                    HistoryMaintenanceLock(request.train_root.parent / f".{request.profile.profile_id}.training.lock")
                )
            archive = SQLiteHistoryTrainingInputSnapshot.open(requests[0].history_root)
            RequireHistoryPartitionRepackInactive(archive.archive_root)
            snapshot = archive.snapshot
            if any(
                request.expected_history_snapshot_hash is not None
                and request.expected_history_snapshot_hash != snapshot.active_snapshot_hash
                for request in normalized
            ):
                raise ValueError("history_archive_repack_target_mismatch")
            if reuse_matching:
                reused = tuple(_reuse_profile(snapshot, request) for request in normalized)
                if all(result is not None for result in reused):
                    return JointTrainingRunResult(
                        cast(tuple[TrainingRunResult, TrainingRunResult], reused),
                        0,
                        0,
                        "serial",
                        time.monotonic() - started,
                        0,
                        True,
                    )
            pending: list[tuple[TrainingSampleBuildRequest, TrainingSampleCache]] = []
            with ExitStack() as samples_scope:
                for request in normalized:
                    split, _ = _prepare_locked_split(snapshot, request.profile, request.settings)
                    if split is None:
                        continue
                    try:
                        window = _require_industry_coverage(
                            archive,
                            split,
                            request.profile.algorithm,
                            request.settings.fitting.minimum_industry_training_rows,
                        )
                    except (HistoryTrainingInputError, OSError, ValueError):
                        continue
                    cache = _sample_cache_for(snapshot, split, request)
                    samples = samples_scope.enter_context(cache.open())
                    if not cache.reused:
                        pending.append(
                            (
                                TrainingSampleBuildRequest(
                                    archive, snapshot.training_codes, window, samples, request.profile, request.progress
                                ),
                                cache,
                            )
                        )
                rows_read = BuildJointTrainingSamples(tuple(item[0] for item in pending)) if pending else 0
                for sample_request, cache in pending:
                    if sample_request.repository.count() == 0:
                        raise ValueError("profile_training_rows_empty")
                    cache.seal(sample_request.repository)
            preparation_seconds = time.monotonic() - started
            fit_started = time.monotonic()
            fit_mode: Literal["serial", "parallel"] = "parallel" if parallel_fit else "serial"
            if parallel_fit:
                with ThreadPoolExecutor(max_workers=2, thread_name_prefix="profile-fit") as executor:
                    futures = tuple(
                        executor.submit(
                            _run_locked, SQLiteHistoryTrainingInputSnapshot.open(request.history_root), request
                        )
                        for request in normalized
                    )
                    profile_results = tuple(future.result() for future in futures)
            else:
                profile_results = tuple(
                    _run_locked(SQLiteHistoryTrainingInputSnapshot.open(request.history_root), request)
                    for request in normalized
                )
            return JointTrainingRunResult(
                cast(tuple[TrainingRunResult, TrainingRunResult], profile_results),
                int(bool(pending)),
                rows_read,
                fit_mode,
                preparation_seconds,
                time.monotonic() - fit_started,
            )
    except (HistoryMaintenanceAlreadyRunningError, HistoryPartitionRepackFenceError) as exc:
        blocked = tuple(
            TrainingRunResult(
                None,
                archive.snapshot.active_snapshot_hash,
                0,
                tuple(_blocked_from_snapshot(head, archive.snapshot, _reason(exc)) for head in request.contracts),
            )
            for request in normalized
        )
        return JointTrainingRunResult(cast(tuple[TrainingRunResult, TrainingRunResult], blocked), 0, 0, "serial", 0, 0)
    except (HistoryTrainingInputError, OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        blocked = tuple(
            TrainingRunResult(
                None,
                archive.snapshot.active_snapshot_hash,
                0,
                tuple(_blocked_from_snapshot(head, archive.snapshot, _reason(exc)) for head in request.contracts),
            )
            for request in normalized
        )
        return JointTrainingRunResult(cast(tuple[TrainingRunResult, TrainingRunResult], blocked), 0, 0, "serial", 0, 0)


def ChooseJointFitMode(history_root: Path, *, evidence_path: Path | None = None) -> Literal["serial", "parallel"]:
    """Require both safe resources and a same-input winning benchmark."""

    if not ParallelFitResourcesAvailable(history_root) or evidence_path is None:
        return "serial"
    evidence = LoadJointFitEvidence(evidence_path)
    if evidence is None or not evidence.supports_parallel(
        storage_kind=ClassifyHistoryStorage(history_root), cpu_count=os.cpu_count() or 1
    ):
        return "serial"
    return "parallel"


def ParallelFitResourcesAvailable(history_root: Path) -> bool:
    """Check bounded fit concurrency without reading or creating benchmark state."""

    if ClassifyHistoryStorage(history_root) != "ssd" or (os.cpu_count() or 1) < 4:
        return False
    try:
        available_line = next(
            line
            for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines()
            if line.startswith("MemAvailable:")
        )
        available_bytes = int(available_line.split()[1]) * 1024
        cgroup_limit = Path("/sys/fs/cgroup/memory.max").read_text(encoding="ascii").strip()
    except (OSError, StopIteration, ValueError, IndexError):
        return False
    try:
        cgroup_bytes = int(cgroup_limit) if cgroup_limit != "max" else None
    except ValueError:
        return False
    if available_bytes < 5 * 1024**3 or (cgroup_bytes is not None and cgroup_bytes < 5 * 1024**3):
        return False
    return True


def _run_training(request: ProfileTrainingRequest) -> TrainingRunResult:
    _publish(request.progress, TomorrowTrainingProgress("resource_preflight", "completed", 1, 1))
    _publish(request.progress, TomorrowTrainingProgress("input_read", "started", 0, 1))
    try:
        archive = SQLiteHistoryTrainingInputSnapshot.open(request.history_root)
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
            HistoryMaintenanceLock(archive.archive_root.with_name(".history.lock")),
            HistoryMaintenanceLock(training_lock),
        ):
            archive = SQLiteHistoryTrainingInputSnapshot.open(request.history_root)
            try:
                RequireHistoryPartitionRepackInactive(archive.archive_root)
            except HistoryPartitionRepackFenceError:
                if request.expected_history_snapshot_hash != archive.snapshot.active_snapshot_hash:
                    raise
            return _run_locked(archive, request)
    except HistoryMaintenanceAlreadyRunningError:
        heads = tuple(
            _blocked_from_snapshot(item, archive.snapshot, "history_maintenance_running") for item in request.contracts
        )
        return TrainingRunResult(None, archive.snapshot.active_snapshot_hash, 0, heads)
    except HistoryPartitionRepackFenceError:
        heads = tuple(
            _blocked_from_snapshot(item, archive.snapshot, "history_archive_repack_activation_pending")
            for item in request.contracts
        )
        return TrainingRunResult(None, archive.snapshot.active_snapshot_hash, 0, heads)


def _run_locked(archive: _TrainingInputSnapshot, request: ProfileTrainingRequest) -> TrainingRunResult:
    archive, target_matches = _reopen_expected_snapshot(archive, request.expected_history_snapshot_hash)
    if not target_matches:
        heads = tuple(
            _blocked_from_snapshot(item, archive.snapshot, "history_archive_repack_target_mismatch")
            for item in request.contracts
        )
        return TrainingRunResult(None, archive.snapshot.active_snapshot_hash, 0, heads)
    snapshot = archive.snapshot
    split, preparation_failure = _prepare_locked_split(snapshot, request.profile, request.settings)
    if split is None:
        heads = tuple(_blocked_from_snapshot(item, snapshot, preparation_failure) for item in request.contracts)
        return TrainingRunResult(None, snapshot.active_snapshot_hash, 0, heads)
    plans, completed = _build_head_plans(snapshot, request)
    if not plans:
        ordered = _ordered_results(request.contracts, completed)
        return TrainingRunResult(None, snapshot.active_snapshot_hash, 0, ordered)
    preflight_result = _preflight_failure_result(archive, snapshot, plans, request.contracts, completed)
    if preflight_result is not None:
        return preflight_result
    _publish(request.progress, TomorrowTrainingProgress("training_preparation", "completed", 1, 1))
    run_id = ContentHash(
        (
            request.profile.profile_id,
            snapshot.active_snapshot_hash,
            split.content_hash,
            tuple((item.contract.strategy.value, item.training_contract_hash) for item in plans),
        )
    )
    peak_bytes = 0
    cache: TrainingSampleCache | None = None
    try:
        training_window = _require_industry_coverage(
            archive,
            split,
            request.profile.algorithm,
            request.settings.fitting.minimum_industry_training_rows,
        )
        request.train_root.mkdir(parents=True, exist_ok=True)
        _cleanup_abandoned_workspaces(request.train_root)
        cache = _sample_cache_for(snapshot, split, request)
        _publish(request.progress, TomorrowTrainingProgress("sample_cache", "started", 0, 1))
        with cache.open() as samples:
            _publish(request.progress, TomorrowTrainingProgress("sample_cache", "completed", 1, 1, int(cache.reused)))
            metrics = _prepare_samples(
                TrainingSampleBuildRequest(
                    archive, snapshot.training_codes, training_window, samples, request.profile, request.progress
                ),
                cache,
            )
            for contract in request.contracts:
                for split_name, expected in cast(
                    tuple[tuple[TrainingSplitName, int], ...],
                    (
                        ("training", len(split.model_fit_dates)),
                        ("early_stopping", len(split.early_stopping_dates)),
                        ("calibration", len(split.calibration_dates)),
                        ("validation", len(split.confirmation_dates) + len(split.daily_proxy_holdout_dates)),
                    ),
                ):
                    if expected and samples.split_distinct_date_count(split_name, contract) != expected:
                        raise ValueError("profile_training_effective_dates_insufficient")
            peak_bytes = samples.database_size_bytes
            prepared: list[tuple[Strategy, Path]] = []
            for plan in plans:
                result, staging = _fit_and_prepare_head(
                    _HeadFitRequest(
                        plan,
                        request.profile,
                        request.settings,
                        snapshot,
                        split,
                        samples,
                        metrics,
                        request.train_root,
                        run_id,
                        request.progress,
                        peak_bytes,
                    )
                )
                completed.append(result)
                if staging is not None:
                    prepared.append((plan.contract.strategy, staging))
            if len(prepared) == len(request.profile.heads) and all(
                item.status == "engineering_ready" for item in completed
            ):
                try:
                    _publish(request.progress, TomorrowTrainingProgress("artifact_publish", "started", 0, 1))
                    PublishProfileBundles(request.train_root, request.profile, tuple(prepared))
                    _publish(request.progress, TomorrowTrainingProgress("artifact_publish", "completed", 1, 1))
                except InterruptedError:
                    raise
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    completed = [
                        replace(item, status="blocked", model_hash="", failure_reasons=(_reason(exc),))
                        for item in completed
                    ]
            else:
                completed = [
                    replace(item, status="blocked", model_hash="", failure_reasons=("profile_pair_incomplete",))
                    if item.status == "engineering_ready"
                    else item
                    for item in completed
                ]
            for _, staging in prepared:
                if staging.exists():
                    shutil.rmtree(staging)
        if all(item.status != "blocked" for item in completed):
            cache.clear()
            PruneEmptySampleDirectories(request.sample_root, request.sample_root.parent)
    except InterruptedError:
        raise
    except (HistoryTrainingInputError, OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        peak_bytes = max(peak_bytes, SampleCacheBytes(cache))
        reason = _reason(exc)
        for plan in plans:
            if not any(item.strategy is plan.contract.strategy for item in completed):
                completed.append(
                    replace(
                        _blocked_from_snapshot(plan.contract, snapshot, reason),
                        run_id=run_id,
                        label_cutoff=plan.label_cutoff,
                    )
                )
    ordered = _ordered_results(request.contracts, completed)
    return TrainingRunResult(run_id, snapshot.active_snapshot_hash, peak_bytes, ordered)


def _sample_cache_for(
    snapshot: HistoryTrainingInputSnapshot,
    split: TrainingDateSplit,
    request: ProfileTrainingRequest,
) -> TrainingSampleCache:
    feature_count = max(max(contract.feature_positions) for contract in request.contracts) + 1
    cache_identity = ContentHash(
        (
            TrainingImplementationIdentity(),
            snapshot.active_snapshot_hash,
            snapshot.input_descriptor_hash,
            request.profile.profile_id,
            split.content_hash,
            feature_count,
            "monthly_samples",
            HISTORY_ALIGNMENT_CONTRACT,
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
    return TrainingSampleCache(request.sample_root, cache_identity, feature_count, split.content_hash)


def _prepare_samples(request: TrainingSampleBuildRequest, cache: TrainingSampleCache) -> tuple[TargetMetric, ...]:
    if not cache.reused:
        BuildTrainingSamples(request)
    _publish(request.progress, TomorrowTrainingProgress("target_statistics", "started", 0, 1))
    if request.repository.count() == 0:
        raise ValueError("profile_training_rows_empty")
    if not cache.reused:
        cache.seal(request.repository)
    metrics = request.repository.validation_target_metrics()
    _publish(request.progress, TomorrowTrainingProgress("target_statistics", "completed", 1, 1))
    return metrics


def _require_industry_coverage(
    archive: _TrainingInputSnapshot,
    split: TrainingDateSplit,
    algorithm: Literal["lightgbm", "ridge", "tcn"],
    minimum_training_rows: int,
) -> TomorrowTrainingWindow:
    window = TomorrowTrainingWindow(split)
    # Calendar continuity is checked by the aligner; these are signal-fact bounds.
    if not archive.has_industry_window(window.readable_dates, 1):
        raise ValueError("history_industry_coverage_insufficient")
    for anchors in (
        split.model_fit_dates,
        *((split.early_stopping_dates,) if algorithm in {"lightgbm", "tcn"} else ()),
        split.calibration_dates,
        split.confirmation_dates,
        split.daily_proxy_holdout_dates,
    ):
        if not archive.has_industry_window(window.readable_dates, 1, anchor_dates=frozenset(anchors)):
            raise ValueError("history_industry_split_coverage_insufficient")
    bounds = archive.industry_sample_upper_bounds(window.readable_dates, 1, frozenset(split.model_fit_dates))
    if not any(count >= minimum_training_rows for _, count in bounds):
        raise ValueError("history_industry_training_rows_insufficient")
    return window


def _reopen_expected_snapshot(
    archive: _TrainingInputSnapshot,
    expected_snapshot_hash: str | None,
) -> tuple[_TrainingInputSnapshot, bool]:
    if expected_snapshot_hash is None:
        return archive, True
    reopened = SQLiteHistoryTrainingInputSnapshot.open(archive.archive_root)
    return reopened, reopened.snapshot.active_snapshot_hash == expected_snapshot_hash


def _prepare_locked_split(
    snapshot: HistoryTrainingInputSnapshot,
    profile: TrainedProfileContract,
    settings: ProfileTrainingSettings,
) -> tuple[TrainingDateSplit | None, str]:
    try:
        split = BuildTrainingDateSplit(
            snapshot.calendar.open_dates,
            parent_manifest_hash=snapshot.active_snapshot_hash,
            fit_dates=settings.fit_dates,
            early_stopping_sessions=20 if profile.algorithm in {"lightgbm", "tcn"} else 0,
            history_sessions=profile.history_sessions,
        )
    except ValueError:
        return None, "training_split_unavailable"
    return split, ""


def _build_head_plans(
    snapshot: HistoryTrainingInputSnapshot,
    request: ProfileTrainingRequest,
) -> tuple[list[_HeadPlan], list[HeadTrainingResult]]:
    plans: list[_HeadPlan] = []
    completed: list[HeadTrainingResult] = []
    for contract in request.contracts:
        label_cutoff = _mature_label_cutoff(snapshot, contract)
        contract_hash = _training_contract_hash(request.profile, contract, request.settings)
        if label_cutoff is None:
            completed.append(_blocked_from_snapshot(contract, snapshot, "history_training_data_incomplete"))
        else:
            plans.append(_HeadPlan(contract, label_cutoff, contract_hash))
    return plans, completed


def _preflight_failure_result(
    archive: _TrainingInputSnapshot,
    snapshot: HistoryTrainingInputSnapshot,
    plans: list[_HeadPlan],
    contracts: tuple[TrainedHeadContract, ...],
    completed: list[HeadTrainingResult],
) -> TrainingRunResult | None:
    failures = EvaluateTomorrowTrainingInput(
        archive.describe_frozen_daily_input(),
        expected_manifest_hash=snapshot.active_snapshot_hash,
        expected_source_cutoff=snapshot.source_cutoff,
    ).failure_reasons
    if not failures:
        return None
    completed.extend(
        replace(
            _blocked_from_snapshot(plan.contract, snapshot, failures[0]),
            label_cutoff=plan.label_cutoff,
        )
        for plan in plans
    )
    return TrainingRunResult(
        None,
        snapshot.active_snapshot_hash,
        0,
        _ordered_results(contracts, completed),
    )


def _fit_and_prepare_head(request: _HeadFitRequest) -> tuple[HeadTrainingResult, Path | None]:
    plan = request.plan
    snapshot = request.snapshot
    contract = plan.contract
    staging: Path | None = None
    try:
        models, training_rows, validation_rows = FitIndustryModels(
            request.samples,
            request.split,
            contract,
            request.settings.fitting,
            progress=request.progress,
        )
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
            training_rows,
            validation_rows,
            request.run_id,
        )
        model = BuildIndustryModelDocument(models)
        model_hash = ContentHash(model)
        model["content_hash"] = model_hash
        report = _report_document(context, models, model_hash, request.metrics)
        report_hash = ContentHash(report)
        report["content_hash"] = report_hash
        if not report["artifact_structure_valid"]:
            return HeadTrainingResult(
                contract.strategy,
                "rejected",
                snapshot.input_scope,
                request.run_id,
                snapshot.active_snapshot_hash,
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
            ), None
        metadata = _metadata_document(context, model_hash, models, training_input, report)
        metadata["content_hash"] = ContentHash(metadata)
        staging = MakeBundleStagingDirectory(request.train_root)
        _write_json(staging / "model.json", model)
        _write_json(staging / "metadata.json", metadata)
        result = HeadTrainingResult(
            contract.strategy,
            "engineering_ready",
            snapshot.input_scope,
            request.run_id,
            snapshot.active_snapshot_hash,
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
        )
        return result, staging
    except InterruptedError:
        raise
    except (LightGBMError, OSError, TypeError, ValueError, RuntimeError, sqlite3.Error) as exc:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        return replace(
            _blocked_from_snapshot(contract, snapshot, _reason(exc)),
            run_id=request.run_id,
            label_cutoff=plan.label_cutoff,
            sample_database_peak_bytes=request.peak_bytes,
        ), None


def _report_document(
    context: _TrainingRunContext,
    models: dict[str, dict[str, object]],
    model_hash: str,
    metrics: tuple[TargetMetric, ...],
) -> dict[str, object]:
    structure_reasons = [] if models and context.validation_rows > 0 else ["industry_model_validation_insufficient"]
    selected = (
        {
            item.target: _metric_payload(item)
            for item in metrics
            if item.target in {"target_t2", "target_t3", "target_t4", "target_t5", "target_d25_aggregate"}
        }
        if context.contract.strategy is Strategy.D25
        else {item.target: _metric_payload(item) for item in metrics if item.target == "target_t1"}
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
        "industry_count": len(models),
        "training_rows": context.training_rows,
        "validation_rows": context.validation_rows,
        "target_metrics": selected,
        "artifact_structure_valid": not structure_reasons,
        "artifact_structure_failure_reasons": structure_reasons,
        "prediction_return_validation_passed": False,
        "prediction_return_failure_reasons": ["daily_close_proxy_not_point_in_time"],
        "automatic_model_update": False,
    }
    if context.profile.algorithm == "tcn":
        document["prediction_metrics"] = BuildPredictionMetricsDocument(models)
    return document


def _metadata_document(
    context: _TrainingRunContext,
    model_hash: str,
    models: dict[str, dict[str, object]],
    training_input: dict[str, object],
    report: dict[str, object],
) -> dict[str, object]:
    return {
        "schema_version": f"{context.profile.profile_id}_head_metadata",
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
        "industry_sample_counts": {
            industry: {key: parameters[key] for key in ("training_rows", "validation_rows")}
            for industry, parameters in models.items()
        },
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
        "training_input_hash": snapshot.active_snapshot_hash,
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
        "training_input_hash": snapshot.active_snapshot_hash,
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


def _reuse_profile(snapshot: HistoryTrainingInputSnapshot, request: ProfileTrainingRequest) -> TrainingRunResult | None:
    try:
        bundles = InspectActiveProfileBundles(request.train_root, request.profile)
    except (OSError, ValueError, RuntimeError):
        return None
    heads: list[HeadTrainingResult] = []
    for bundle, contract in zip(bundles, request.contracts, strict=True):
        if (
            bundle.training_input_hash != snapshot.active_snapshot_hash
            or bundle.label_cutoff != _mature_label_cutoff(snapshot, contract)
            or bundle.training_contract_hash != _training_contract_hash(request.profile, contract, request.settings)
        ):
            return None
        model = bundle.model
        heads.append(
            HeadTrainingResult(
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
            )
        )
    return TrainingRunResult(bundles[0].training_run_hash, snapshot.active_snapshot_hash, 0, tuple(heads))


def _training_contract_hash(
    profile: TrainedProfileContract, contract: TrainedHeadContract, settings: ProfileTrainingSettings
) -> str:
    fitting = settings.fitting
    return ContentHash(
        {
            "implementation_identity": TrainingImplementationIdentity(),
            "dependencies": {"numpy": version("numpy"), "lightgbm": version("lightgbm")}
            if profile.algorithm == "lightgbm"
            else {"numpy": version("numpy"), "torch": version("torch")}
            if profile.algorithm == "tcn"
            else {"numpy": version("numpy")},
            "schema_version": f"{profile.profile_id}_head_training_contract",
            "profile_id": profile.profile_id,
            "history_sessions": profile.history_sessions,
            "feature_day_axis": HISTORY_ALIGNMENT_CONTRACT,
            "fit_dates": settings.fit_dates,
            "algorithm": profile.algorithm,
            "strategy_head": contract.strategy.value,
            "compute_threads": TOMORROW_TRAINING_COMPUTE_THREADS,
            "peak_rss_mib": TOMORROW_TRAINING_PEAK_RSS_MIB,
            "training_input_scope": "complete_manifest",
            "feature_manifest_hash": contract.feature_manifest.content_hash,
            "feature_ids": list(contract.feature_manifest.names),
            "feature_units": list(contract.feature_manifest.units),
            "label_target": contract.label_target,
            "maturity_sessions": contract.maturity_sessions,
            "benchmark": "daily_close_equal_weight_engineering_proxy",
            "training_cost_bps": 0,
            "evaluation_cost_bps": [20, 50, 100],
            "minimum_industry_training_rows": fitting.minimum_industry_training_rows,
            "model_parameters": FittingParameterDocument(fitting)
            if isinstance(fitting, TcnFittingParameters)
            else {"ridge_penalty": fitting.penalty}
            if not isinstance(fitting, LightGBMFittingParameters)
            else {
                "objective": "regression_l2",
                "learning_rate": fitting.learning_rate,
                "max_depth": fitting.max_depth,
                "num_leaves": fitting.num_leaves,
                "min_data_in_leaf": fitting.minimum_leaf_rows,
                "num_boost_round": fitting.boosting_rounds,
                "early_stopping_rounds": fitting.early_stopping_rounds,
                "max_bin": fitting.maximum_bins,
                "deterministic": True,
                "seed": fitting.seed,
                "force_col_wise": True,
                "histogram_pool_size_mib": fitting.histogram_pool_mib,
            },
            "sqlite_cache_mib": TRAINING_SAMPLE_CACHE_MIB,
            "sqlite_mmap_bytes": TRAINING_SAMPLE_MMAP_BYTES,
            "parallel_industries": 1,
            "calibration": "independent_linear",
            "training_anchor": "15:00_close_proxy",
            "runtime_anchor": contract.runtime_anchor,
            "point_in_time_parity": False,
            "validation_scope": "daily_close_engineering_proxy",
            "terminal_holdout_opened": False,
            "automatic_model_update": False,
            "production_authority": False,
        }
    )


def _head_split_hash(split: TrainingDateSplit, contract: TrainedHeadContract) -> str:
    return ContentHash(
        {
            "calendar_split_hash": split.content_hash,
            "strategy_head": contract.strategy.value,
            "maturity_sessions": contract.maturity_sessions,
            "label_target": contract.label_target,
        }
    )


def _mature_label_cutoff(
    snapshot: HistoryTrainingInputSnapshot,
    contract: TrainedHeadContract,
) -> date | None:
    dates = snapshot.calendar.open_dates
    positions = {day: position for position, day in enumerate(dates)}
    position = positions.get(snapshot.source_cutoff)
    if position is None or position < contract.maturity_sessions:
        return None
    return dates[position - contract.maturity_sessions]


def _metric_payload(metric: TargetMetric) -> dict[str, object]:
    return {
        "count": metric.count,
        "mean": metric.mean,
        "standard_deviation": metric.standard_deviation,
    }


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
        snapshot.active_snapshot_hash,
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


def _ordered_results(
    contracts: tuple[TrainedHeadContract, ...],
    results: list[HeadTrainingResult],
) -> tuple[HeadTrainingResult, ...]:
    by_strategy = {item.strategy: item for item in results}
    return tuple(by_strategy[item.strategy] for item in contracts)


def _cleanup_abandoned_workspaces(train_root: Path) -> None:
    for candidate in train_root.glob(".training-sample-workspace.*"):
        if candidate.is_dir() and not candidate.is_symlink():
            shutil.rmtree(candidate)
    for candidate in train_root.glob(".bundle-staging.*"):
        if candidate.is_dir() and not candidate.is_symlink():
            shutil.rmtree(candidate)


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
    }:
        return value
    return (
        "history_manifest_unavailable" if "manifest" in value or "no such file" in value else "profile_training_failed"
    )


__all__ = [
    "ChooseJointFitMode",
    "HeadTrainingResult",
    "JointTrainingRunResult",
    "ParallelFitResourcesAvailable",
    "ProfileTrainingRequest",
    "TrainingRunResult",
    "TrainingStatus",
    "RunJointProfileTraining",
    "RunProfileTraining",
    "RunRepackProfileTraining",
]
