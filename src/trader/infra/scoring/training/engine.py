"""Deterministic one-scan training engine shared by trained scoring profiles."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Literal, Protocol, cast
from zoneinfo import ZoneInfo

from lightgbm.basic import LightGBMError

from trader.application.research.tomorrow_training import (
    TOMORROW_TRAINING_COMPUTE_THREADS,
    TOMORROW_TRAINING_PEAK_RSS_MIB,
    TomorrowTrainingPartitionValidationProgress,
    TomorrowTrainingProgress,
    TomorrowTrainingProgressPort,
    TomorrowTrainingWindow,
)
from trader.domain.recommendation.model_scoring import TRAINED_HEAD_EXPOSURE_CONTRACT
from trader.domain.recommendation.models import Strategy
from trader.domain.research.baostock_daily import BaoStockTrainingSplit, build_baostock_training_split
from trader.domain.research.history_control import (
    HistoryActiveSnapshot,
    HistoryTrainingDueReason,
    HistoryTrainingDueState,
)
from trader.domain.research.tomorrow_training_input import FrozenDailyInputDescriptor, evaluate_tomorrow_training_input
from trader.download.infra.history_archive_repack import (
    HistoryArchiveRepackFenceError,
    require_history_archive_repack_inactive,
)
from trader.download.infra.history_control_repository import (
    HistoryMaintenanceAlreadyRunningError,
    HistoryMaintenanceLock,
)
from trader.download.infra.history_month_partition import HistoryPartitionVerificationPhase
from trader.infra.artifacts.canonical import content_hash
from trader.infra.research.history_training_due import (
    HistoryTrainingDueEvaluation,
    HistoryTrainingDueQuery,
    evaluate_history_training_due,
)
from trader.infra.research.history_training_input import (
    HistoryTrainingInputError,
    HistoryTrainingInputSnapshot,
    SQLiteHistoryTrainingInputArchive,
)
from trader.infra.scoring.head_bundles.bundle_repository import (
    HeadBundlePublicationIdentity,
    make_bundle_staging_directory,
    publish_head_bundle,
    recover_head_bundle_publication,
)
from trader.infra.scoring.head_bundles.contracts import TrainedHeadContract, TrainedProfileContract
from trader.infra.scoring.training.model_fitting import MODEL_FITTING_PARAMETERS, fit_industry_models
from trader.infra.scoring.training.sample_builder import (
    TrainingSampleBuildRequest,
    TrainingWindowArchive,
    build_training_samples,
)
from trader.infra.scoring.training.sample_repository import (
    TRAINING_SAMPLE_CACHE_MIB,
    TRAINING_SAMPLE_MMAP_BYTES,
    SQLiteTrainingSampleRepository,
    TargetMetric,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")
TrainingStatus = Literal["blocked", "rejected", "engineering_ready", "already_current", "not_due"]


class _TrainingInputArchive(TrainingWindowArchive, Protocol):
    snapshot: HistoryTrainingInputSnapshot

    @property
    def archive_root(self) -> Path: ...

    @property
    def active_snapshot(self) -> HistoryActiveSnapshot: ...

    def describe_frozen_daily_input(self) -> FrozenDailyInputDescriptor: ...

    def verify_partitions(
        self,
        progress: Callable[[int, int, int, int, int, HistoryPartitionVerificationPhase], None] | None = None,
    ) -> None: ...


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
    matured_label_days_since_training: int = 0
    training_due: bool = False
    training_due_reason: HistoryTrainingDueReason = "data_incomplete"
    invalidated_cache_dates: tuple[date, ...] = ()
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
        if "engineering_ready" in statuses:
            return "engineering_ready"
        if statuses and all(item == "already_current" for item in statuses):
            return "already_current"
        return "not_due"


@dataclass(frozen=True)
class _HeadPlan:
    contract: TrainedHeadContract
    due: HistoryTrainingDueEvaluation
    label_cutoff: date
    training_contract_hash: str


@dataclass(frozen=True)
class _ArtifactContext:
    profile: TrainedProfileContract
    contract: TrainedHeadContract
    snapshot: HistoryTrainingInputSnapshot
    label_cutoff: date
    training_input_document_hash: str
    split: BaoStockTrainingSplit
    split_hash: str
    training_contract_hash: str
    training_rows: int
    validation_rows: int


@dataclass(frozen=True)
class ProfileTrainingRequest:
    history_root: Path
    train_root: Path
    profile: TrainedProfileContract
    contracts: tuple[TrainedHeadContract, ...]
    progress: TomorrowTrainingProgressPort | None = None
    observed_at: datetime | None = None
    expected_history_snapshot_hash: str | None = None

    def __post_init__(self) -> None:
        strategies = tuple(contract.strategy for contract in self.contracts)
        if (
            not self.contracts
            or len(set(strategies)) != len(strategies)
            or any(contract != self.profile.head_for_strategy(contract.strategy) for contract in self.contracts)
        ):
            raise ValueError("profile training request contains a foreign or duplicate head contract")


@dataclass(frozen=True)
class _HeadFitRequest:
    plan: _HeadPlan
    profile: TrainedProfileContract
    snapshot: HistoryTrainingInputSnapshot
    split: BaoStockTrainingSplit
    samples: SQLiteTrainingSampleRepository
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


def run_profile_training(request: ProfileTrainingRequest) -> TrainingRunResult:
    return _run_training(
        replace(
            request,
            train_root=request.train_root / request.profile.output_directory,
        )
    )


def run_repack_profile_training(request: ProfileTrainingRequest) -> TrainingRunResult:
    if request.expected_history_snapshot_hash is None:
        raise ValueError("repack training requires an expected history snapshot hash")
    return run_profile_training(request)


def _run_training(request: ProfileTrainingRequest) -> TrainingRunResult:
    _publish(request.progress, TomorrowTrainingProgress("resource_preflight", "completed", 1, 1))
    try:
        archive = SQLiteHistoryTrainingInputArchive.open(request.history_root)
    except (HistoryTrainingInputError, OSError, ValueError) as exc:
        heads = tuple(_unavailable_result(item, _reason(exc)) for item in request.contracts)
        return TrainingRunResult(None, "", 0, heads)
    try:
        # Training owns a profile-specific lock and may read an immutable
        # snapshot while history maintenance publishes a newer one.  The
        # archive itself remains protected by snapshot identity checks.
        training_lock = request.train_root / ".training.lock"
        with HistoryMaintenanceLock(training_lock):
            try:
                require_history_archive_repack_inactive(archive.archive_root)
            except HistoryArchiveRepackFenceError:
                if request.expected_history_snapshot_hash != archive.snapshot.active_snapshot_hash:
                    raise
            return _run_locked(archive, request)
    except HistoryMaintenanceAlreadyRunningError:
        heads = tuple(
            _blocked_from_snapshot(item, archive.snapshot, "history_maintenance_running") for item in request.contracts
        )
        return TrainingRunResult(None, archive.snapshot.active_snapshot_hash, 0, heads)
    except HistoryArchiveRepackFenceError:
        heads = tuple(
            _blocked_from_snapshot(item, archive.snapshot, "history_archive_repack_activation_pending")
            for item in request.contracts
        )
        return TrainingRunResult(None, archive.snapshot.active_snapshot_hash, 0, heads)


def _run_locked(archive: _TrainingInputArchive, request: ProfileTrainingRequest) -> TrainingRunResult:
    archive, target_matches = _reopen_expected_archive(archive, request.expected_history_snapshot_hash)
    if not target_matches:
        heads = tuple(
            _blocked_from_snapshot(item, archive.snapshot, "history_archive_repack_target_mismatch")
            for item in request.contracts
        )
        return TrainingRunResult(None, archive.snapshot.active_snapshot_hash, 0, heads)
    snapshot = archive.snapshot
    split, preparation_failure = _prepare_locked_split(snapshot, request.train_root, request.profile, request.contracts)
    if split is None:
        heads = tuple(_blocked_from_snapshot(item, snapshot, preparation_failure) for item in request.contracts)
        return TrainingRunResult(None, snapshot.active_snapshot_hash, 0, heads)
    now = request.observed_at.astimezone(_SHANGHAI) if request.observed_at is not None else datetime.now(_SHANGHAI)
    plans, completed = _build_head_plans(archive, snapshot, replace(request, observed_at=now))
    if not plans:
        ordered = _ordered_results(request.contracts, completed)
        return TrainingRunResult(None, snapshot.active_snapshot_hash, 0, ordered)
    preflight_result = _preflight_failure_result(archive, snapshot, plans, request.contracts, completed)
    if preflight_result is not None:
        return preflight_result
    run_id = hashlib.sha256(
        (
            f"{request.profile.profile_id}:{snapshot.active_snapshot_hash}:"
            f"{','.join(item.contract.strategy.value for item in plans)}"
        ).encode()
    ).hexdigest()
    try:
        _verify_partitions_once(archive, request.progress)
        request.train_root.mkdir(parents=True, exist_ok=True)
        _cleanup_abandoned_workspaces(request.train_root)
        with tempfile.TemporaryDirectory(prefix=".training-sample-workspace.", dir=request.train_root) as workspace:
            feature_count = max(max(contract.feature_positions) for contract in request.contracts) + 1
            with SQLiteTrainingSampleRepository(Path(workspace) / "samples.sqlite3", feature_count) as samples:
                build_training_samples(
                    TrainingSampleBuildRequest(
                        archive,
                        snapshot.training_codes,
                        TomorrowTrainingWindow(split),
                        samples,
                        request.profile,
                        request.progress,
                    )
                )
                peak_bytes = samples.database_size_bytes
                if samples.count() == 0:
                    raise ValueError("profile_training_rows_empty")
                metrics = samples.validation_target_metrics()
                for plan in plans:
                    completed.append(
                        _fit_and_publish_head(
                            _HeadFitRequest(
                                plan,
                                request.profile,
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
                    )
    except (HistoryTrainingInputError, OSError, ValueError, RuntimeError) as exc:
        peak_bytes = 0
        reason = _reason(exc)
        for plan in plans:
            if not any(item.strategy is plan.contract.strategy for item in completed):
                completed.append(
                    _with_due(
                        replace(_blocked_from_snapshot(plan.contract, snapshot, reason), run_id=run_id),
                        plan.due.state,
                        plan.due.invalidated_cache_dates,
                    )
                )
    ordered = _ordered_results(request.contracts, completed)
    return TrainingRunResult(run_id, snapshot.active_snapshot_hash, peak_bytes, ordered)


def _reopen_expected_archive(
    archive: _TrainingInputArchive,
    expected_snapshot_hash: str | None,
) -> tuple[_TrainingInputArchive, bool]:
    if expected_snapshot_hash is None:
        return archive, True
    reopened = SQLiteHistoryTrainingInputArchive.open(archive.archive_root.parent)
    return reopened, reopened.snapshot.active_snapshot_hash == expected_snapshot_hash


def _prepare_locked_split(
    snapshot: HistoryTrainingInputSnapshot,
    train_root: Path,
    profile: TrainedProfileContract,
    contracts: tuple[TrainedHeadContract, ...],
) -> tuple[BaoStockTrainingSplit | None, str]:
    try:
        _recover_incomplete_publications(train_root, profile, contracts)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return None, _reason(exc)
    try:
        split = build_baostock_training_split(
            snapshot.calendar.open_dates,
            parent_manifest_hash=snapshot.active_snapshot_hash,
        )
    except ValueError:
        return None, "training_split_unavailable"
    return split, ""


def _build_head_plans(
    archive: _TrainingInputArchive,
    snapshot: HistoryTrainingInputSnapshot,
    request: ProfileTrainingRequest,
) -> tuple[list[_HeadPlan], list[HeadTrainingResult]]:
    plans: list[_HeadPlan] = []
    completed: list[HeadTrainingResult] = []
    assert request.observed_at is not None
    for contract in request.contracts:
        label_cutoff = _mature_label_cutoff(snapshot, contract)
        contract_hash = _training_contract_hash(request.profile, contract)
        due = evaluate_history_training_due(
            HistoryTrainingDueQuery(
                archive.archive_root,
                request.train_root,
                request.observed_at,
                request.profile,
                contract.strategy,
                contract_hash,
            )
        )
        if due is None or label_cutoff is None:
            completed.append(_blocked_from_snapshot(contract, snapshot, "history_training_data_incomplete"))
        elif due.state.reason == "data_incomplete":
            completed.append(
                _with_due(
                    _blocked_from_snapshot(contract, snapshot, "history_training_data_incomplete"),
                    due.state,
                    due.invalidated_cache_dates,
                )
            )
        elif not due.state.training_due:
            current = (
                due.bundle is not None
                and due.bundle.training_input_hash == snapshot.active_snapshot_hash
                and due.bundle.label_cutoff == label_cutoff
            )
            result = _base_result(contract, "already_current" if current else "not_due", snapshot)
            if current and due.bundle is not None:
                result = replace(
                    result,
                    model_hash=due.bundle.model_hash,
                    report_hash=due.bundle.report_hash,
                )
            completed.append(
                _with_due(
                    result,
                    due.state,
                    due.invalidated_cache_dates,
                )
            )
        else:
            plans.append(_HeadPlan(contract, due, label_cutoff, contract_hash))
    return plans, completed


def _preflight_failure_result(
    archive: _TrainingInputArchive,
    snapshot: HistoryTrainingInputSnapshot,
    plans: list[_HeadPlan],
    contracts: tuple[TrainedHeadContract, ...],
    completed: list[HeadTrainingResult],
) -> TrainingRunResult | None:
    failures = evaluate_tomorrow_training_input(
        archive.describe_frozen_daily_input(),
        expected_manifest_hash=snapshot.active_snapshot_hash,
        expected_source_cutoff=snapshot.source_cutoff,
    ).failure_reasons
    if not failures:
        return None
    completed.extend(
        _with_due(
            _blocked_from_snapshot(plan.contract, snapshot, failures[0]),
            plan.due.state,
            plan.due.invalidated_cache_dates,
        )
        for plan in plans
    )
    return TrainingRunResult(
        None,
        snapshot.active_snapshot_hash,
        0,
        _ordered_results(contracts, completed),
    )


def _verify_partitions_once(
    archive: _TrainingInputArchive,
    progress: TomorrowTrainingProgressPort | None,
) -> None:
    total = len(archive.active_snapshot.partitions)
    _publish(progress, TomorrowTrainingProgress("partition_validation", "started", 0, total))
    archive.verify_partitions(
        lambda completed, partitions, current, completed_bytes, total_bytes, phase: _publish(
            progress,
            TomorrowTrainingProgress(
                "partition_validation",
                "completed" if completed == partitions else "running",
                completed,
                partitions,
                partition_validation=TomorrowTrainingPartitionValidationProgress(
                    current,
                    partitions,
                    completed_bytes,
                    total_bytes,
                    phase,
                ),
            ),
        )
    )


def _fit_and_publish_head(request: _HeadFitRequest) -> HeadTrainingResult:
    plan = request.plan
    snapshot = request.snapshot
    contract = plan.contract
    staging: Path | None = None
    try:
        models, training_rows, validation_rows = fit_industry_models(
            request.samples, request.split, contract, progress=request.progress
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
            )
        )
        input_document_hash = cast(str, training_input["content_hash"])
        context = _ArtifactContext(
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
        )
        provisional_model = _model_document(context, "0" * 64, models)
        model_payload_hash = cast(str, provisional_model["model_payload_hash"])
        report = _report_document(context, models, model_payload_hash, request.metrics)
        report_hash = content_hash(report)
        report["content_hash"] = report_hash
        if not report["validation_passed"]:
            return _with_due(
                HeadTrainingResult(
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
                    tuple(cast(list[str], report["failure_reasons"])),
                    sample_database_peak_bytes=request.peak_bytes,
                ),
                plan.due.state,
                plan.due.invalidated_cache_dates,
            )
        model = _model_document(context, report_hash, models)
        model_hash = content_hash(model)
        model["content_hash"] = model_hash
        output = request.train_root / contract.directory_name
        staging = make_bundle_staging_directory(output)
        _write_json(staging / "training-input.json", training_input)
        _write_json(staging / "report.json", report)
        _write_json(staging / "model.json", model)
        _publish(
            request.progress,
            TomorrowTrainingProgress("artifact_publish", "started", 0, 1, strategy=contract.strategy),
        )
        publish_head_bundle(
            staging,
            output,
            contract.strategy,
            request.profile,
            HeadBundlePublicationIdentity(
                snapshot.active_snapshot_hash,
                snapshot.source_identity_hash,
                plan.label_cutoff,
            ),
        )
        staging = None
        _publish(
            request.progress,
            TomorrowTrainingProgress("artifact_publish", "completed", 1, 1, strategy=contract.strategy),
        )
        return _after_success(
            HeadTrainingResult(
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
                sample_database_peak_bytes=request.peak_bytes,
            ),
            plan.label_cutoff,
            plan.due.invalidated_cache_dates,
        )
    except (LightGBMError, OSError, TypeError, ValueError, RuntimeError) as exc:
        return _with_due(
            replace(
                _blocked_from_snapshot(contract, snapshot, _reason(exc)),
                run_id=request.run_id,
                sample_database_peak_bytes=request.peak_bytes,
            ),
            plan.due.state,
            plan.due.invalidated_cache_dates,
        )
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


def _report_document(
    context: _ArtifactContext,
    models: dict[str, dict[str, object]],
    model_payload_hash: str,
    metrics: tuple[TargetMetric, ...],
) -> dict[str, object]:
    reasons = [] if models and context.validation_rows > 0 else ["industry_model_validation_insufficient"]
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
        "schema_version": f"{context.profile.profile_id}_head_training_report",
        "profile_id": context.profile.profile_id,
        "model_id": context.contract.model_id,
        "strategy_head": context.contract.strategy.value,
        "training_input_document_hash": context.training_input_document_hash,
        "model_payload_hash": model_payload_hash,
        "training_anchor": "15:00_close_proxy",
        "runtime_anchor": context.contract.runtime_anchor,
        "point_in_time_parity": False,
        "historical_status": "historical_data_insufficient",
        "historical_failure_reasons": ["daily_close_proxy_not_point_in_time"],
        "industry_count": len(models),
        "training_rows": context.training_rows,
        "validation_rows": context.validation_rows,
        "target_metrics": selected,
        "validation_passed": not reasons,
        "failure_reasons": reasons,
        "automatic_model_update": False,
    }
    return document


def _model_document(
    context: _ArtifactContext,
    report_hash: str,
    models: dict[str, dict[str, object]],
) -> dict[str, object]:
    document: dict[str, object] = {
        **_common_document(context),
        "schema_version": f"{context.profile.profile_id}_head_scoring_model",
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
        "training_input_document_hash": context.training_input_document_hash,
        "report_hash": report_hash,
        "historical_status": "historical_data_insufficient",
        "historical_failure_reasons": ["daily_close_proxy_not_point_in_time"],
        "training_anchor": "15:00_close_proxy",
        "runtime_anchor": context.contract.runtime_anchor,
        "point_in_time_parity": False,
        "training_rows": context.training_rows,
        "validation_rows": context.validation_rows,
        "industry_count": len(models),
        "ensemble_weights": {"ridge": 0.5, "lightgbm": 0.5},
        "industries": models,
        "dependencies": {"lightgbm": version("lightgbm"), "numpy": version("numpy")},
        "automatic_model_update": False,
    }
    document["model_payload_hash"] = _model_payload_hash(document)
    return document


def _training_input_document(context: _TrainingInputDocumentContext) -> dict[str, object]:
    profile = context.profile
    snapshot = context.snapshot
    contract = context.contract
    document: dict[str, object] = {
        "schema_version": f"{profile.profile_id}_head_training_input",
        "profile_id": profile.profile_id,
        "strategy_head": contract.strategy.value,
        "training_input_scope": snapshot.input_scope,
        "training_input_hash": snapshot.active_snapshot_hash,
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
    document["content_hash"] = content_hash(document)
    return document


def _common_document(context: _ArtifactContext) -> dict[str, object]:
    snapshot = context.snapshot
    return {
        "training_input_scope": snapshot.input_scope,
        "training_input_hash": snapshot.active_snapshot_hash,
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


def _training_contract_hash(profile: TrainedProfileContract, contract: TrainedHeadContract) -> str:
    fitting = MODEL_FITTING_PARAMETERS
    return content_hash(
        {
            "schema_version": f"{profile.profile_id}_head_training_contract",
            "profile_id": profile.profile_id,
            "history_sessions": profile.history_sessions,
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
            "ridge_penalty": fitting.ridge_penalty,
            "lightgbm": {
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
            "ensemble_weights": {"ridge": 0.5, "lightgbm": 0.5},
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


def _head_split_hash(split: BaoStockTrainingSplit, contract: TrainedHeadContract) -> str:
    return content_hash(
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


def _model_payload_hash(document: dict[str, object]) -> str:
    return content_hash(
        {
            key: value
            for key, value in document.items()
            if key not in {"content_hash", "report_hash", "model_payload_hash"}
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


def _with_due(
    result: HeadTrainingResult,
    state: HistoryTrainingDueState,
    invalidated_dates: tuple[date, ...],
) -> HeadTrainingResult:
    return replace(
        result,
        label_cutoff=state.current_label_cutoff,
        matured_label_days_since_training=state.matured_label_days_since_training,
        training_due=state.training_due,
        training_due_reason=state.reason,
        invalidated_cache_dates=invalidated_dates,
    )


def _after_success(
    result: HeadTrainingResult,
    label_cutoff: date,
    invalidated_dates: tuple[date, ...],
) -> HeadTrainingResult:
    return replace(
        result,
        label_cutoff=label_cutoff,
        matured_label_days_since_training=0,
        training_due=False,
        training_due_reason="not_due",
        invalidated_cache_dates=invalidated_dates,
    )


def _cleanup_abandoned_workspaces(train_root: Path) -> None:
    for candidate in train_root.glob(".training-sample-workspace.*"):
        if candidate.is_dir() and not candidate.is_symlink():
            shutil.rmtree(candidate)
    for output in (train_root / strategy.value for strategy in Strategy):
        if output.is_dir():
            for candidate in output.glob(".bundle-staging.*"):
                if candidate.is_dir() and not candidate.is_symlink():
                    shutil.rmtree(candidate)


def _recover_incomplete_publications(
    train_root: Path,
    profile: TrainedProfileContract,
    contracts: tuple[TrainedHeadContract, ...],
) -> None:
    for contract in contracts:
        output = train_root / contract.directory_name
        if output.is_dir():
            recover_head_bundle_publication(output, contract.strategy, profile)


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
    value = str(exc).lower()
    if value in {
        "history_manifest_unavailable",
        "history_manifest_parent_unavailable",
        "profile_training_rows_empty",
        "history_training_data_incomplete",
        "training_split_unavailable",
    }:
        return value
    return (
        "history_manifest_unavailable" if "manifest" in value or "no such file" in value else "profile_training_failed"
    )


__all__ = [
    "HeadTrainingResult",
    "ProfileTrainingRequest",
    "TrainingRunResult",
    "TrainingStatus",
    "run_profile_training",
    "run_repack_profile_training",
]
