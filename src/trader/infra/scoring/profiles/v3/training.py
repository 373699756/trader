"""Deterministic Tomorrow V3 training from the partitioned BaoStock archive."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Literal, Protocol, cast
from zoneinfo import ZoneInfo

from trader.application.research.tomorrow_training import (
    TOMORROW_TRAINING_COMPUTE_THREADS,
    TOMORROW_TRAINING_PEAK_RSS_MIB,
    TomorrowPartitionValidationProgress,
    TomorrowTrainingProgress,
    TomorrowTrainingProgressPort,
    TomorrowTrainingWindow,
)
from trader.domain.market.feature_contracts import TOMORROW_MODEL_FEATURE_MANIFEST
from trader.domain.recommendation.model_scoring import V3_EXPOSURE_CONTRACT
from trader.domain.research.baostock_daily import BaoStockTrainingSplit
from trader.domain.research.history_control import (
    HistoryActiveSnapshot,
    HistoryTrainingDueReason,
    HistoryTrainingDueState,
)
from trader.domain.research.tomorrow_training_input import FrozenDailyInputDescriptor, evaluate_tomorrow_training_input
from trader.infra.research.history_control_repository import (
    HistoryMaintenanceAlreadyRunningError,
    HistoryMaintenanceLock,
)
from trader.infra.research.history_archive_repack import (
    HistoryArchiveRepackFenceError,
    require_history_repack_inactive,
)
from trader.infra.research.history_month_partition import HistoryPartitionVerificationPhase
from trader.infra.research.history_training_due import HistoryTrainingDueEvaluation, evaluate_history_training_due
from trader.infra.research.history_training_input import (
    HistoryTrainingInputError,
    HistoryTrainingInputSnapshot,
    SQLiteHistoryTrainingInputArchive,
)
from trader.infra.scoring.artifact_hashing import artifact_content_hash
from trader.infra.scoring.profiles.v3.model_fitting import fit_industry_models
from trader.infra.scoring.profiles.v3.sample_builder import TrainingWindowArchive, build_training_samples
from trader.infra.scoring.profiles.v3.training_bundle_repository import (
    make_bundle_staging_directory,
    publish_tomorrow_bundle,
)
from trader.infra.scoring.profiles.v3.training_sample_repository import (
    SQLiteTomorrowTrainingSampleRepository,
)

_MODEL_ID = "industry_ridge_lightgbm"
_SOURCE_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHANGHAI = ZoneInfo("Asia/Shanghai")
TomorrowTrainingStatus = Literal["blocked", "rejected", "engineering_ready", "already_current", "not_due"]


class _TrainingInputArchive(TrainingWindowArchive, Protocol):
    snapshot: HistoryTrainingInputSnapshot

    @property
    def active_snapshot(self) -> HistoryActiveSnapshot: ...

    def describe_frozen_daily_input(self) -> FrozenDailyInputDescriptor: ...

    def verify_partitions(
        self,
        progress: Callable[[int, int, int, int, int, HistoryPartitionVerificationPhase], None] | None = None,
    ) -> None: ...


@dataclass(frozen=True)
class TomorrowTrainingResult:
    status: TomorrowTrainingStatus
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


@dataclass(frozen=True)
class _TrainingArtifactContext:
    training_input_scope: Literal["complete_manifest"]
    training_input_hash: str
    label_cutoff: date
    source_identity_hash: str
    training_input_document_hash: str
    training_input_codes: int
    training_universe_codes: int
    split: BaoStockTrainingSplit
    source_commit: str
    training_contract_hash: str
    training_rows: int
    validation_rows: int


@dataclass(frozen=True)
class _TrainingExecution:
    archive: _TrainingInputArchive
    snapshot: HistoryTrainingInputSnapshot
    due: HistoryTrainingDueEvaluation
    label_cutoff: date
    output: Path
    run_id: str
    progress: TomorrowTrainingProgressPort | None
    source_commit: str
    split: BaoStockTrainingSplit


@dataclass(frozen=True)
class _TrainingInvocation:
    history_root: Path
    train_root: Path
    progress: TomorrowTrainingProgressPort | None
    source_commit: str
    observed_at: datetime | None
    expected_history_snapshot_hash: str | None


def run_tomorrow_training(
    history_root: Path,
    train_root: Path,
    *,
    progress: TomorrowTrainingProgressPort | None = None,
    source_commit: str = "",
    observed_at: datetime | None = None,
) -> TomorrowTrainingResult:
    return _run_training_invocation(
        _TrainingInvocation(history_root, train_root, progress, source_commit, observed_at, None)
    )


def run_repack_tomorrow_training(
    history_root: Path,
    train_root: Path,
    *,
    expected_history_snapshot_hash: str,
    progress: TomorrowTrainingProgressPort | None = None,
    source_commit: str = "",
) -> TomorrowTrainingResult:
    """Run the explicit post-repack gate for one exact activated snapshot."""

    return _run_training_invocation(
        _TrainingInvocation(
            history_root,
            train_root,
            progress,
            source_commit,
            None,
            expected_history_snapshot_hash,
        )
    )


def _run_training_invocation(invocation: _TrainingInvocation) -> TomorrowTrainingResult:
    _publish_progress(invocation.progress, TomorrowTrainingProgress("resource_preflight", "completed", 1, 1))
    try:
        archive = SQLiteHistoryTrainingInputArchive.open(invocation.history_root)
    except (HistoryTrainingInputError, OSError, ValueError) as exc:
        return TomorrowTrainingResult("blocked", "unavailable", None, "", 0, 0, "", "", 0, 0, 0, (_reason(exc),))
    try:
        with HistoryMaintenanceLock(archive.archive_root / ".maintenance.lock"):
            try:
                require_history_repack_inactive(archive.archive_root)
            except HistoryArchiveRepackFenceError:
                if invocation.expected_history_snapshot_hash != archive.snapshot.active_snapshot_hash:
                    raise
            return _run_tomorrow_training_locked(invocation)
    except HistoryMaintenanceAlreadyRunningError:
        return _history_maintenance_blocked(archive, "history_maintenance_running")
    except HistoryArchiveRepackFenceError:
        return _history_maintenance_blocked(archive, "history_repack_activation_pending")


def _history_maintenance_blocked(
    archive: SQLiteHistoryTrainingInputArchive,
    reason: str,
) -> TomorrowTrainingResult:
    snapshot = archive.snapshot
    return TomorrowTrainingResult(
        "blocked",
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
        (reason,),
        label_cutoff=snapshot.label_cutoff,
    )


def _run_tomorrow_training_locked(
    invocation: _TrainingInvocation,
) -> TomorrowTrainingResult:
    if invocation.expected_history_snapshot_hash is not None:
        archive = SQLiteHistoryTrainingInputArchive.open(invocation.history_root)
        if archive.snapshot.active_snapshot_hash != invocation.expected_history_snapshot_hash:
            return _history_maintenance_blocked(archive, "history_repack_target_mismatch")
    prepared = _prepare_training(
        invocation.history_root,
        invocation.train_root,
        invocation.progress,
        invocation.source_commit,
        invocation.observed_at,
    )
    if isinstance(prepared, TomorrowTrainingResult):
        return prepared
    return _execute_training(prepared)


def _prepare_training(
    history_root: Path,
    train_root: Path,
    progress: TomorrowTrainingProgressPort | None,
    source_commit: str,
    observed_at: datetime | None,
) -> _TrainingExecution | TomorrowTrainingResult:
    try:
        archive = SQLiteHistoryTrainingInputArchive.open(history_root)
    except (HistoryTrainingInputError, OSError, ValueError) as exc:
        return TomorrowTrainingResult("blocked", "unavailable", None, "", 0, 0, "", "", 0, 0, 0, (_reason(exc),))
    snapshot = archive.snapshot
    split, expected_training_contract_hash = _expected_training_contract(snapshot, source_commit)
    due = evaluate_history_training_due(
        archive.archive_root,
        train_root,
        observed_at.astimezone(_SHANGHAI) if observed_at is not None else datetime.now(_SHANGHAI),
        expected_training_contract_hash,
    )
    if due is None:
        return TomorrowTrainingResult(
            "blocked",
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
            ("history_manifest_unavailable",),
        )
    label_cutoff = due.state.current_label_cutoff
    invalidated_dates = due.invalidated_cache_dates
    if due.state.reason == "data_incomplete":
        return _with_due(
            TomorrowTrainingResult(
                "blocked",
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
                ("history_training_data_incomplete",),
            ),
            due.state,
            invalidated_dates,
        )
    if not due.state.training_due:
        is_current = (
            due.bundle is not None
            and due.bundle.training_input_hash == snapshot.active_snapshot_hash
            and due.bundle.label_cutoff == label_cutoff
        )
        return _with_due(
            TomorrowTrainingResult(
                "already_current" if is_current else "not_due",
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
            ),
            due.state,
            invalidated_dates,
        )
    compatibility = evaluate_tomorrow_training_input(
        archive.describe_frozen_daily_input(),
        expected_manifest_hash=snapshot.active_snapshot_hash,
        expected_source_cutoff=snapshot.source_cutoff,
    )
    preflight_reasons = list(compatibility.failure_reasons)
    if label_cutoff is None:
        preflight_reasons.append("label_outcome_incomplete")
    if split is None:
        preflight_reasons.append("training_split_unavailable")
    if _SOURCE_COMMIT.fullmatch(source_commit) is None:
        preflight_reasons.append("source_commit_unavailable")
    if preflight_reasons:
        return _with_due(
            TomorrowTrainingResult(
                "blocked",
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
                tuple(preflight_reasons),
            ),
            due.state,
            invalidated_dates,
        )
    output = _training_output_directory(train_root)
    run_id = hashlib.sha256(f"{snapshot.active_snapshot_hash}:tomorrow-v3".encode()).hexdigest()
    return _TrainingExecution(
        archive,
        snapshot,
        due,
        cast(date, label_cutoff),
        output,
        run_id,
        progress,
        source_commit,
        cast(BaoStockTrainingSplit, split),
    )


def _expected_training_contract(
    snapshot: HistoryTrainingInputSnapshot,
    source_commit: str,
) -> tuple[BaoStockTrainingSplit | None, str | None]:
    if snapshot.label_cutoff is None:
        return None, None
    try:
        split = _build_split(snapshot.calendar.open_dates, snapshot.active_snapshot_hash)
    except ValueError:
        return None, None
    return split, _training_contract_hash(snapshot, split, source_commit, snapshot.label_cutoff)


def _execute_training(plan: _TrainingExecution) -> TomorrowTrainingResult:
    archive = plan.archive
    snapshot = plan.snapshot
    due = plan.due
    invalidated_dates = due.invalidated_cache_dates
    label_cutoff = plan.label_cutoff
    output = plan.output
    run_id = plan.run_id
    progress = plan.progress
    source_commit = plan.source_commit
    staging: Path | None = None
    try:
        split = plan.split
        training_contract_hash = _training_contract_hash(snapshot, split, source_commit, label_cutoff)
        training_input = _training_input_document(
            snapshot,
            label_cutoff,
            source_commit,
            training_contract_hash,
        )
        training_input_hash = cast(str, training_input["content_hash"])
        window = TomorrowTrainingWindow(split)
        partition_total = len(archive.active_snapshot.partitions)
        _publish_progress(
            progress,
            TomorrowTrainingProgress("partition_validation", "started", 0, partition_total),
        )
        archive.verify_partitions(
            lambda completed, total, current, completed_bytes, total_bytes, phase: _publish_progress(
                progress,
                TomorrowTrainingProgress(
                    "partition_validation",
                    "completed" if completed == total else "running",
                    completed,
                    total,
                    partition_validation=TomorrowPartitionValidationProgress(
                        current,
                        total,
                        completed_bytes,
                        total_bytes,
                        phase,
                    ),
                ),
            )
        )
        output.mkdir(parents=True, exist_ok=True)
        _cleanup_abandoned_sample_workspaces(output)
        with tempfile.TemporaryDirectory(prefix=".sample-workspace.", dir=output) as workspace:
            with SQLiteTomorrowTrainingSampleRepository(Path(workspace) / "samples.sqlite3") as samples:
                build_training_samples(archive, snapshot.training_codes, window, samples, progress=progress)
                if samples.count() == 0:
                    return _with_due(
                        TomorrowTrainingResult(
                            "blocked",
                            snapshot.input_scope,
                            run_id,
                            snapshot.active_snapshot_hash,
                            len(snapshot.training_codes),
                            snapshot.universe_count,
                            "",
                            "",
                            0,
                            0,
                            0,
                            ("v3_training_rows_empty",),
                        ),
                        due.state,
                        invalidated_dates,
                    )
                models, training_rows, validation_rows = fit_industry_models(samples, split, progress=progress)
        context = _TrainingArtifactContext(
            snapshot.input_scope,
            snapshot.active_snapshot_hash,
            label_cutoff,
            snapshot.source_identity_hash,
            training_input_hash,
            len(snapshot.training_codes),
            snapshot.universe_count,
            split,
            source_commit,
            training_contract_hash,
            training_rows,
            validation_rows,
        )
        _publish_progress(progress, TomorrowTrainingProgress("artifact_publish", "started", 0, 1))
        provisional_model = _model_document(context, "0" * 64, models)
        model_payload_hash = cast(str, provisional_model["model_payload_hash"])
        report = _build_report(context, models, model_payload_hash)
        report_hash = artifact_content_hash(report)
        report["content_hash"] = report_hash
        if not report["validation_passed"]:
            return _with_due(
                TomorrowTrainingResult(
                    "rejected",
                    snapshot.input_scope,
                    run_id,
                    snapshot.active_snapshot_hash,
                    len(snapshot.training_codes),
                    snapshot.universe_count,
                    report_hash,
                    "",
                    len(models),
                    training_rows,
                    validation_rows,
                    tuple(cast(list[str], report["failure_reasons"])),
                ),
                due.state,
                invalidated_dates,
            )
        model = _model_document(context, report_hash, models)
        model_hash = artifact_content_hash(model)
        model["content_hash"] = model_hash
        staging = make_bundle_staging_directory(output)
        _write_json(staging / "training-input.json", training_input)
        _write_json(staging / "report.json", report)
        _write_json(staging / "model.json", model)
        publish_tomorrow_bundle(
            staging,
            output,
            training_input_hash=snapshot.active_snapshot_hash,
            source_identity_hash=snapshot.source_identity_hash,
            label_cutoff=label_cutoff,
        )
        staging = None
        _publish_progress(progress, TomorrowTrainingProgress("artifact_publish", "completed", 1, 1))
        return _after_success(
            TomorrowTrainingResult(
                "engineering_ready",
                snapshot.input_scope,
                run_id,
                snapshot.active_snapshot_hash,
                len(snapshot.training_codes),
                snapshot.universe_count,
                report_hash,
                model_hash,
                len(models),
                training_rows,
                validation_rows,
                (),
            ),
            label_cutoff,
            invalidated_dates,
        )
    except (HistoryTrainingInputError, OSError, ValueError, RuntimeError) as exc:
        return _with_due(
            TomorrowTrainingResult(
                "blocked",
                snapshot.input_scope,
                run_id,
                snapshot.active_snapshot_hash,
                len(snapshot.training_codes),
                snapshot.universe_count,
                "",
                "",
                0,
                0,
                0,
                (_reason(exc),),
            ),
            due.state,
            invalidated_dates,
        )
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


def _build_split(dates: tuple[date, ...], manifest_hash: str) -> BaoStockTrainingSplit:
    from trader.domain.research.baostock_daily import build_baostock_training_split

    return build_baostock_training_split(dates, parent_manifest_hash=manifest_hash)


def _build_report(
    context: _TrainingArtifactContext,
    models: dict[str, dict[str, object]],
    model_payload_hash: str,
) -> dict[str, object]:
    reasons = [] if models and context.validation_rows > 0 else ["v3_industry_model_validation_insufficient"]
    return {
        "schema_version": "tomorrow_training_report",
        "model_id": _MODEL_ID,
        "training_input_scope": context.training_input_scope,
        "training_input_hash": context.training_input_hash,
        "label_cutoff": context.label_cutoff.isoformat(),
        "source_identity_hash": context.source_identity_hash,
        "training_input_document_hash": context.training_input_document_hash,
        "training_input_codes": context.training_input_codes,
        "training_universe_codes": context.training_universe_codes,
        "feature_manifest_hash": TOMORROW_MODEL_FEATURE_MANIFEST.content_hash,
        "split_hash": context.split.content_hash,
        "source_commit": context.source_commit,
        "training_contract_hash": context.training_contract_hash,
        "model_payload_hash": model_payload_hash,
        "label_target": "pre_cost_excess_return",
        "training_cost_bps": 0,
        "training_anchor": "15:00_close_proxy",
        "runtime_anchor": "14:50",
        "point_in_time_parity": False,
        "validation_scope": "daily_close_engineering_proxy",
        "industry_count": len(models),
        "training_rows": context.training_rows,
        "validation_rows": context.validation_rows,
        "validation_passed": not reasons,
        "failure_reasons": reasons,
        "historical_status": "historical_data_insufficient",
        "historical_failure_reasons": list(_historical_failure_reasons(context.training_input_scope)),
        "automatic_model_update": False,
        "production_authority": False,
    }


def _model_document(
    context: _TrainingArtifactContext,
    report_hash: str,
    models: dict[str, dict[str, object]],
) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": "tomorrow_scoring_model",
        "profile_id": "v3",
        "model_id": _MODEL_ID,
        "strategy_head": "tomorrow",
        "feature_ids": list(TOMORROW_MODEL_FEATURE_MANIFEST.names),
        "feature_units": list(TOMORROW_MODEL_FEATURE_MANIFEST.units),
        "exposure_contract": {
            "market": True,
            "board": True,
            "industry": True,
            "log_average_amount_20d": True,
            "order": list(V3_EXPOSURE_CONTRACT.order),
        },
        "training_input_scope": context.training_input_scope,
        "training_input_hash": context.training_input_hash,
        "label_cutoff": context.label_cutoff.isoformat(),
        "source_identity_hash": context.source_identity_hash,
        "training_input_document_hash": context.training_input_document_hash,
        "training_input_codes": context.training_input_codes,
        "training_universe_codes": context.training_universe_codes,
        "split_hash": context.split.content_hash,
        "report_hash": report_hash,
        "source_commit": context.source_commit,
        "feature_manifest_hash": TOMORROW_MODEL_FEATURE_MANIFEST.content_hash,
        "training_contract_hash": context.training_contract_hash,
        "label_target": "pre_cost_excess_return",
        "training_cost_bps": 0,
        "validation_scope": "daily_close_engineering_proxy",
        "historical_status": "historical_data_insufficient",
        "historical_failure_reasons": list(_historical_failure_reasons(context.training_input_scope)),
        "training_anchor": "15:00_close_proxy",
        "runtime_anchor": "14:50",
        "point_in_time_parity": False,
        "training_rows": context.training_rows,
        "validation_rows": context.validation_rows,
        "industry_count": len(models),
        "ensemble_weights": {"ridge": 0.5, "lightgbm": 0.5},
        "industries": models,
        "dependencies": {"lightgbm": version("lightgbm"), "numpy": version("numpy")},
        "automatic_model_update": False,
        "production_authority": False,
    }
    document["model_payload_hash"] = _model_payload_hash(document)
    return document


def _training_contract_hash(
    snapshot: HistoryTrainingInputSnapshot,
    split: BaoStockTrainingSplit,
    _source_commit: str,
    label_cutoff: date,
) -> str:
    contract: dict[str, object] = {
        "schema_version": "tomorrow_training_contract",
        "compute_threads": TOMORROW_TRAINING_COMPUTE_THREADS,
        "peak_rss_mib": TOMORROW_TRAINING_PEAK_RSS_MIB,
        "training_input_scope": snapshot.input_scope,
        "training_input_hash": snapshot.active_snapshot_hash,
        "source_identity_hash": snapshot.source_identity_hash,
        "calendar_hash": snapshot.calendar_hash,
        "source_cutoff": snapshot.source_cutoff.isoformat(),
        "label_cutoff": label_cutoff.isoformat(),
        "input_descriptor_hash": snapshot.input_descriptor_hash,
        "feature_manifest_hash": TOMORROW_MODEL_FEATURE_MANIFEST.content_hash,
        "feature_ids": list(TOMORROW_MODEL_FEATURE_MANIFEST.names),
        "feature_units": list(TOMORROW_MODEL_FEATURE_MANIFEST.units),
        "split_hash": split.content_hash,
        "label_target": "pre_cost_excess_return",
        "benchmark": "daily_close_equal_weight_engineering_proxy",
        "training_cost_bps": 0,
        "evaluation_cost_bps": [20, 50, 100],
        "ensemble_weights": {"ridge": 0.5, "lightgbm": 0.5},
        "training_anchor": "15:00_close_proxy",
        "runtime_anchor": "14:50",
        "point_in_time_parity": False,
        "validation_scope": "daily_close_engineering_proxy",
        "terminal_holdout_opened": False,
        "automatic_model_update": False,
        "production_authority": False,
    }
    return artifact_content_hash(contract)


def _training_input_document(
    snapshot: HistoryTrainingInputSnapshot,
    label_cutoff: date,
    source_commit: str,
    training_contract_hash: str,
) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": "tomorrow_training_input",
        "training_input_scope": snapshot.input_scope,
        "training_input_hash": snapshot.active_snapshot_hash,
        "label_cutoff": label_cutoff.isoformat(),
        "source_identity_hash": snapshot.source_identity_hash,
        "calendar_hash": snapshot.calendar_hash,
        "source_cutoff": snapshot.source_cutoff.isoformat(),
        "requested_sessions": len(snapshot.calendar.open_dates),
        "input_descriptor_hash": snapshot.input_descriptor_hash,
        "training_input_codes": len(snapshot.training_codes),
        "training_universe_codes": snapshot.universe_count,
        "codes": list(snapshot.training_codes),
        "source_commit": source_commit,
        "feature_manifest_hash": TOMORROW_MODEL_FEATURE_MANIFEST.content_hash,
        "training_contract_hash": training_contract_hash,
        "label_target": "pre_cost_excess_return",
        "training_cost_bps": 0,
        "validation_scope": "daily_close_engineering_proxy",
        "production_authority": False,
    }
    document["content_hash"] = artifact_content_hash(document)
    return document


def _model_payload_hash(document: dict[str, object]) -> str:
    excluded = {"content_hash", "report_hash", "model_payload_hash"}
    return artifact_content_hash({key: value for key, value in document.items() if key not in excluded})


def _historical_failure_reasons(_training_input_scope: str) -> tuple[str, ...]:
    return ("daily_close_proxy_not_point_in_time",)


def _training_output_directory(train_root: Path) -> Path:
    return train_root / "tomorrow-v3"


def _cleanup_abandoned_sample_workspaces(output: Path) -> None:
    for candidate in output.glob(".sample-workspace.*"):
        if candidate.is_dir() and not candidate.is_symlink():
            shutil.rmtree(candidate)


def _with_due(
    result: TomorrowTrainingResult,
    state: HistoryTrainingDueState,
    invalidated_dates: tuple[date, ...],
) -> TomorrowTrainingResult:
    return replace(
        result,
        label_cutoff=state.current_label_cutoff,
        matured_label_days_since_training=state.matured_label_days_since_training,
        training_due=state.training_due,
        training_due_reason=state.reason,
        invalidated_cache_dates=invalidated_dates,
    )


def _after_success(
    result: TomorrowTrainingResult,
    label_cutoff: date,
    invalidated_dates: tuple[date, ...],
) -> TomorrowTrainingResult:
    return replace(
        result,
        label_cutoff=label_cutoff,
        matured_label_days_since_training=0,
        training_due=False,
        training_due_reason="not_due",
        invalidated_cache_dates=invalidated_dates,
    )


def _publish_progress(
    progress: TomorrowTrainingProgressPort | None,
    update: TomorrowTrainingProgress,
) -> None:
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
    text = str(exc).lower()
    if text in {"history_manifest_unavailable", "history_manifest_parent_unavailable"}:
        return "history_manifest_unavailable"
    return "history_manifest_unavailable" if "manifest" in text or "no such file" in text else "v3_training_failed"


__all__ = ["TomorrowTrainingResult", "run_tomorrow_training"]
