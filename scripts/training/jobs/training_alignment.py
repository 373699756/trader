"""Train all alignment contracts against an explicitly external frozen history copy."""

from __future__ import annotations

from scripts.core.paths import PROJECT_ROOT as TOOL_ROOT

import argparse
import hashlib
import io
import json
import math
import os
import shutil
import sys
import time
from collections import Counter
from contextlib import AbstractContextManager, nullcontext, redirect_stdout
from datetime import datetime
from datetime import time as daytime
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Literal, NoReturn, TypedDict
from zoneinfo import ZoneInfo

from scripts.core.reporting import ReadJsonReports
from scripts.core.reporting import emit_report
from scripts.training.resources.training_process_measurement import TrainingProcessMeasurement
from scripts.training.output_paths import RequireIndependentOutput

if TYPE_CHECKING:
    from scripts.training.samples.computation_candidates import ComputationCandidate
    from scripts.training.samples.computation_candidates import ComputationCandidateCounts
    from scripts.training.resources.scheduling_candidates import SchedulingCandidate
    from scripts.training.resources.scheduling_candidates import SchedulingExperiment
    from trader.infra.settings import RuntimeSettings
    from trader.infra.sqlite_policy import SqliteIoPolicy
    from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
    from trader.training.infra.sample_index import SQLiteTrainingSampleIndex


class _SqliteArguments(TypedDict, total=False):
    sqlite_policy: SqliteIoPolicy


class _MeasurementOptions(TypedDict, total=False):
    published_history_read_only: bool
    sample_cache_transfer: Literal["copy", "hardlink"]
    scheduling_candidate: SchedulingCandidate
    scheduling_evidence: Path
    computation_candidate: ComputationCandidate


def PrepareTrainingHistoryCopy(
    source: Path,
    destination: Path,
    *,
    timeout_seconds: float,
    sqlite_policy: SqliteIoPolicy,
) -> dict[str, object]:
    """Back up a published input and its references into a new external directory."""
    from trader.infra.atomic_files.json import AtomicWriteJson
    from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession
    from trader.infra.sqlite_publication import PublishSqliteFile, VerifySqliteFile
    from trader.training.infra.history.history_training_input import SQLiteHistoryTrainingInputSnapshot

    project = TOOL_ROOT
    if destination.is_symlink():
        raise ValueError("training_history_copy_destination_invalid")
    source, destination = source.resolve(strict=True), destination.resolve()
    if (destination.exists() or destination.is_relative_to(project) or project.is_relative_to(destination)
            or destination.is_relative_to(source) or source.is_relative_to(destination)
            or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
        raise ValueError("training_history_copy_destination_invalid")
    started = time.monotonic()
    _require_history_copy_reference(source, sqlite_policy)
    snapshot = SQLiteHistoryTrainingInputSnapshot.open(source, sqlite_policy=sqlite_policy).snapshot
    files = ((source / "catalog.sqlite3", Path("history/catalog.sqlite3")), *(
        (path, Path("history") / path.name) for path in sorted(source.glob("prices_*.sqlite3"))
    ), (source.parent / "reference/company_st.sqlite3", Path("reference/company_st.sqlite3")))
    if len(files) < 3 or any(path.is_symlink() or not path.is_file() for path, _relative in files):
        raise ValueError("training_history_copy_source_incomplete")
    required = sum(path.stat().st_size for path, _relative in files)
    existing = destination.parent
    while not existing.exists():
        existing = existing.parent
    if shutil.disk_usage(existing).free < required * 2 + 1024**3:
        raise ValueError("training_history_copy_space_insufficient")
    deadline = started + timeout_seconds
    source_files = _published_input_signature(source)

    def check_deadline() -> None:
        if time.monotonic() >= deadline:
            raise TimeoutError("training_history_copy_timed_out")

    for path, relative in files:
        if relative.parent == Path("history"):
            VerifySqliteFile(path, 2, sqlite_policy=sqlite_policy, check=check_deadline)
    destination.mkdir(parents=True, exist_ok=False)
    receipts = 0
    for number, (path, relative) in enumerate(files, 1):
        check_deadline()
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        pending = target.with_suffix(".pending")
        with (
            SqliteSession(path, SqliteConnectionOptions(read_only=True, timeout_seconds=5),
                          sqlite_policy=sqlite_policy) as original,
            SqliteSession(pending, SqliteConnectionOptions(timeout_seconds=5), sqlite_policy=sqlite_policy) as replica,
        ):
            original.backup_to(replica, deadline=deadline, monotonic=time.monotonic)
        pending.replace(target)
        if relative.parent == Path("history"):
            PublishSqliteFile(target, 2, sqlite_policy=sqlite_policy, check=check_deadline)
            receipts += 1
        print(f"训练历史副本 | 已备份 {number}/{len(files)} 个数据库", file=sys.stderr, flush=True)
    _require_history_copy_reference(destination / "history", sqlite_policy)
    _require_history_copy_reference(source, sqlite_policy)
    copied = SQLiteHistoryTrainingInputSnapshot.open(destination / "history", sqlite_policy=sqlite_policy).snapshot
    after = SQLiteHistoryTrainingInputSnapshot.open(source, sqlite_policy=sqlite_policy).snapshot
    if copied != snapshot or after != snapshot or source_files != _published_input_signature(source):
        raise ValueError("training_history_copy_source_changed")
    check_deadline()
    report: dict[str, object] = {
        "snapshot_hash": snapshot.snapshot_hash,
        "source_identity_hash": snapshot.source_identity_hash,
        "source_cutoff": snapshot.source_cutoff.isoformat(),
        "source_codes": len(snapshot.training_codes),
        "files": len(files),
        "bytes": sum((destination / relative).stat().st_size for _path, relative in files),
        "seconds": time.monotonic() - started,
        "source_data_writes": 0,
        "source_identity_unchanged": True,
        "source_files_unchanged": True,
        "publication_receipts": receipts,
        "copy_method": "sqlite_online_backup; verified_source; copied_file_receipts; catalog_and_reference_identity_verified",
    }
    AtomicWriteJson(destination / "copy-summary.json", report)
    return report


def _require_history_copy_reference(history: Path, sqlite_policy: SqliteIoPolicy) -> None:
    from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession

    catalog = history / "catalog.sqlite3"
    reference = history.parent / "reference" / "company_st.sqlite3"
    if catalog.is_symlink() or reference.is_symlink() or reference.parent.is_symlink():
        raise ValueError("training_history_copy_reference_invalid")
    with SqliteSession(catalog, SqliteConnectionOptions(read_only=True), sqlite_policy=sqlite_policy) as connection:
        row = connection.run("SELECT reference_file FROM schema_contract WHERE singleton=1").fetchone()
    if row is None or not isinstance(row[0], str):
        raise ValueError("training_history_copy_reference_invalid")
    binding = Path(row[0])
    if (binding.is_absolute() or binding != Path("../reference/company_st.sqlite3")
            or (history / binding).resolve() != reference.resolve()
            or not reference.resolve().is_relative_to(history.parent)):
        raise ValueError("training_history_copy_reference_invalid")


def _inputs(history: Path, runtime_config: Path, *, sqlite_policy: SqliteIoPolicy | None) -> dict[str, object]:
    from trader.download.application.read_published_history import ReadPublishedHistoryUseCase
    from trader.download.infra.published_history_snapshot import SQLitePublishedHistorySnapshot
    from trader.infra.settings import LoadRuntimeSettings
    from trader.recommendation.infra.market_data.published_history_cache import PublishedHistoryCache
    from trader.recommendation.infra.market_data.shared_history_projection import SharedHistoryProjection

    started = time.monotonic()
    settings = LoadRuntimeSettings(runtime_config)
    sqlite_arguments: _SqliteArguments = {}
    if sqlite_policy is not None:
        sqlite_arguments["sqlite_policy"] = sqlite_policy
    source = ReadPublishedHistoryUseCase(SQLitePublishedHistorySnapshot(history, **sqlite_arguments))
    manifest = source.manifest()
    if manifest is None:
        raise ValueError("history_manifest_unavailable")
    cache = PublishedHistoryCache(
        source,
        lookback_sessions=251,
        monotonic=time.monotonic,
        open_dates=lambda: manifest.calendar_dates,
        learned_rank_inputs=True,
        projection_owner=SharedHistoryProjection(
            source, monotonic=time.monotonic, settings=settings.pipeline.history_preload,
        ),
    )
    cache.initialize(cancelled=lambda: False)
    as_of = datetime.combine(manifest.data_cutoff, daytime(20, 0), ZoneInfo("Asia/Shanghai"))
    inputs = cache.completed_model_inputs(manifest.universe_codes, as_of)
    features = {item.code: item.learned_rank_values for item in inputs if item.learned_rank_values}
    reasons = Counter(item.quality.failure or "ready" for item in inputs)
    status = cache.status()
    return {
        "schema_version": "model-inputs-diagnostic",
        "status": "passed" if features else "degraded",
        "summary": {
            "profile": "v1",
            "source_codes": len(manifest.universe_codes),
            "input_ready_codes": len(features),
            "processed_codes": status.maintenance_completed_units,
            "history_scan_count": status.history_scan_count,
            "history_window_count": status.history_window_count,
            "history_quality": dict(reasons),
            "estimated_history_codes": status.estimated_history_codes,
            "suspended_history_codes": status.suspended_history_codes,
            "failure": status.maintenance_reason,
            "seconds": round(time.monotonic() - started, 3),
            "source_identity": manifest.snapshot_hash,
            "signal_date": manifest.data_cutoff.isoformat(),
            "source_data_writes": 0,
            "network_calls": 0,
            "model_predictions": 0,
        },
    }


def _probe(
    history: Path,
    work: Path,
    profiles: tuple[ScoringProfileId, ...] | None = None,
    *,
    runtime_config: Path | None = None, sqlite_policy: SqliteIoPolicy | None,
    require_reuse: bool = True,
    published_history_read_only: bool = False,
    sample_cache_transfer: Literal["copy", "hardlink"] = "copy",
    scheduling_candidate: SchedulingCandidate = "baseline",
    scheduling_evidence: Path | None = None,
    computation_candidate: ComputationCandidate = "baseline",
) -> dict[str, object]:
    # Load diagnostic types only after main selects the measured product checkout.
    from scripts.training.jobs.training_performance import TrainingReuseFailures
    from scripts.training.jobs.training_source_guard import ReadOnlyTrainingSource

    from trader.infra.settings import LoadRuntimeSettings
    from trader.recommendation.domain.market.history_alignment import HISTORY_ALIGNMENT_CONTRACT
    from trader.recommendation.domain.scoring.profile_identity import SCORING_PROFILE_IDS
    from trader.training.infra.history.history_training_input import SQLiteHistoryTrainingInputSnapshot
    from trader.training.infra.implementation_identity import TrainingImplementationIdentity

    project = TOOL_ROOT
    history, work = history.resolve(), work.resolve()
    if (history.is_relative_to(project) and not published_history_read_only) or work.is_relative_to(project) or work == history:
        raise ValueError("training_alignment_requires_external_history_copy")
    if work.exists() or history.is_relative_to(work) or work.is_relative_to(history):
        raise ValueError("training_alignment_requires_new_separate_output")
    config = (runtime_config or project / "config/runtime.json").resolve()
    runtime = LoadRuntimeSettings(config)
    if published_history_read_only and history != runtime.data_paths.history.resolve():
        raise ValueError("training_alignment_requires_configured_published_history")
    if not published_history_read_only and history == runtime.data_paths.history.resolve():
        raise ValueError("training_alignment_published_history_requires_explicit_read_only_mode")
    if published_history_read_only:
        if sqlite_policy is None:
            raise ValueError("training_alignment_published_history_requires_sqlite_policy")
        _require_history_copy_reference(history, sqlite_policy)
    if work.is_relative_to(runtime.data_paths.root.resolve()) or work.is_relative_to(runtime.project_root.resolve()):
        raise ValueError("training_alignment_requires_external_output")
    source_files = _published_input_signature(history) if published_history_read_only else ()
    sqlite_arguments: _SqliteArguments = {}
    if sqlite_policy is not None:
        sqlite_arguments["sqlite_policy"] = sqlite_policy
    source = SQLiteHistoryTrainingInputSnapshot.open(history, **sqlite_arguments)
    identity = source.snapshot.snapshot_hash
    work.mkdir(parents=True)
    started = time.monotonic()
    profiles = SCORING_PROFILE_IDS if profiles is None else profiles
    configuration_hash = hashlib.sha256(
        b"".join(
            hashlib.sha256((runtime.project_root / "config" / name).read_bytes()).digest()
            for name in ("training.json", "features.json")
        )
    ).hexdigest()
    schedule_scope: AbstractContextManager[SchedulingExperiment | None] = nullcontext(None)
    scheduling_report: dict[str, object] = {"name": "baseline", "production_enabled": False, "performance_evidence": False}
    if scheduling_candidate != "baseline":
        if scheduling_evidence is None:
            raise ValueError("training_scheduling_requires_measured_evidence")
        from scripts.training.resources.scheduling_candidates import LoadSchedulingEvidence
        from scripts.training.resources.scheduling_candidates import ProjectSchedulingExperiment
        from scripts.training.resources.scheduling_candidates import UseSchedulingCandidate
        from trader.infra.disk_probe import DiskProbe

        medium = runtime.disk_medium or DiskProbe().inspect(runtime.data_paths.root).kind
        evidence = LoadSchedulingEvidence(
            scheduling_evidence,
            source_hash=identity,
            configuration_hash=configuration_hash,
            implementation_hash=TrainingImplementationIdentity(),
            profiles=profiles,
            medium=medium,
        )
        schedule_scope = UseSchedulingCandidate(scheduling_candidate, evidence)
    elif scheduling_evidence is not None:
        raise ValueError("training_scheduling_evidence_requires_candidate")
    computation_scope: AbstractContextManager[ComputationCandidateCounts | None] = nullcontext(None)
    computation_report: dict[str, object] = {"name": "baseline", "production_enabled": False, "performance_evidence": False}
    if computation_candidate != "baseline":
        from scripts.training.samples.computation_candidates import ProjectComputationCandidate
        from scripts.training.samples.computation_candidates import UseComputationCandidate

        computation_scope = UseComputationCandidate(computation_candidate)
    with (
        schedule_scope as schedule,
        computation_scope as computation,
        ReadOnlyTrainingSource(history) if published_history_read_only else nullcontext(),
    ):
        initial = _train_once(runtime, history, work, profiles)
        repeat = _train_once(runtime, history, work, profiles) if initial["complete"] else None
        sample_reuse = (
            _sample_reuse(runtime, history, work, profiles, initial, transfer=sample_cache_transfer)
            if initial["complete"] else None
        )
    if schedule is not None:
        scheduling_report = ProjectSchedulingExperiment(schedule)
    if computation is not None:
        computation_report = ProjectComputationCandidate(computation)
    unchanged = SQLiteHistoryTrainingInputSnapshot.open(history, **sqlite_arguments).snapshot.snapshot_hash == identity
    files_unchanged = not published_history_read_only or source_files == _published_input_signature(history)
    released = scheduling_report.get("pending_requests", 0) == scheduling_report.get("reserved_threads", 0) == 0
    complete = bool(initial["complete"] and repeat is not None and repeat["complete"] and unchanged and files_unchanged and released)
    equal = repeat is not None and initial["numeric_fingerprints"] == repeat["numeric_fingerprints"]
    reuse_failures = TrainingReuseFailures(initial, repeat, profiles)
    return {
        "schema_version": "training-alignment-diagnostic",
        "status": "passed" if complete and equal and sample_reuse is not None
        and not sample_reuse["failures"] and (not require_reuse or not reuse_failures) else "degraded",
        "summary": {
            "alignment_contract": HISTORY_ALIGNMENT_CONTRACT,
            "profiles": initial["profiles"],
            "execution": initial["execution"],
            "initial": initial,
            "repeat": repeat,
            "sample_reuse": sample_reuse,
            "scheduling_candidate": scheduling_report,
            "computation_candidate": computation_report,
            "repeat_model_values_equal": equal,
            "repeat_reuse_verified": not reuse_failures,
            "repeat_reuse_required": require_reuse,
            "repeat_reuse_failures": reuse_failures,
            "source_codes": len(source.snapshot.training_codes),
            "source_identity_unchanged": unchanged,
            "input_mode": "published_history_read_only" if published_history_read_only else "external_history_copy",
            "source_files_unchanged": files_unchanged,
            "source_data_writes": 0,
            "published_source_guard": "sqlite_mode_ro; due_projection_persist_false; fitter_unmodified"
            if published_history_read_only else "not_required_for_isolated_copy",
            "reloaded_heads": initial["reloaded_heads"],
            "process_peak_rss_bytes": _peak_rss_bytes(),
            "seconds": initial["seconds"],
            "total_seconds": round(time.monotonic() - started, 6),
            "settings": "configured_unmodified",
            "training_configuration_sha256": configuration_hash,
            "training_implementation_sha256": TrainingImplementationIdentity(),
            "production_authority": False,
            "return_validation": "not_evaluated",
            "prediction_comparison": "persisted_model_values_and_validation_metrics; no separate input replay",
            "cache_conditions": "operating_system_cache_uncontrolled; sample_copy_verification_precedes_refit",
        },
    }


def _published_input_signature(history: Path) -> tuple[tuple[str, int, int, int, int, int], ...]:
    files = (
        history / "catalog.sqlite3", *sorted(history.glob("prices_*.sqlite3")),
        history.parent / "reference/company_st.sqlite3",
    )
    paths = tuple(
        path for file in files
        for path in (file, file.with_name(file.name + "-wal"), file.with_name(file.name + ".sha256"))
        if path.exists() or path.is_symlink()
    )
    if any(path.is_symlink() or not path.is_file() for path in paths):
        raise ValueError("training_alignment_published_input_invalid")
    return tuple(
        (
            path.relative_to(history.parent).as_posix(), stamp.st_dev, stamp.st_ino,
            stamp.st_size, stamp.st_mtime_ns, stamp.st_ctime_ns,
        )
        for path in paths for stamp in (path.stat(),)
        if not path.name.endswith("-wal") or stamp.st_size > 0
    )


def _sample_reuse(
    runtime: RuntimeSettings,
    history: Path,
    work: Path,
    profiles: tuple[ScoringProfileId, ...],
    initial: dict[str, object],
    *,
    transfer: Literal["copy", "hardlink"] = "copy",
) -> dict[str, object]:
    from scripts.training.jobs.training_performance import TrainingSamplePreparationFailures
    from scripts.training.jobs.training_performance import TrainingSampleReuseFailures

    selected = tuple(profile for profile in profiles if profile != "v1")
    result: dict[str, object] = {
        "profiles": selected,
        "not_applicable_profiles": tuple(profile for profile in profiles if profile == "v1"),
        "run": None,
        "preparation": None,
        "preparation_seconds": None,
        "copy_verification_seconds": None,
        "copied_bytes": 0,
        "linked_bytes": 0,
        "cache_transfer_method": transfer,
        "preparation_cache_released_after_verification": False,
        "cache_verified": False,
        "model_files_copied": 0,
        "source_cache_unchanged": None,
        "target_cache_cleared": None,
        "failures": (),
    }
    if not selected:
        return result
    target = work / "sample-reuse"
    preparation_root = work / "sample-preparation"
    started = time.monotonic()
    try:
        if target.exists() or target.is_symlink():
            raise ValueError("training_sample_probe_output_exists")
        preparation = _prepare_sample_reuse(runtime, history, preparation_root, selected)
        result["preparation"] = preparation
        failures = TrainingSamplePreparationFailures(initial, preparation, selected)
        if failures:
            result["failures"] = failures
            result["preparation_seconds"] = time.monotonic() - started
            return result
        copy_started = time.monotonic()
        before = _sample_signatures(preparation_root, selected)
        result["source_cache_signatures_before"] = before
        copied = 0
        for profile in selected:
            source = preparation_root / "samples" / profile
            destination = target / "samples" / profile
            if source.is_symlink() or not source.is_dir():
                raise ValueError("training_sample_probe_cache_missing")
            destination.mkdir(parents=True)
            for name in ("samples.sqlite3", "complete.json"):
                path = source / name
                if path.is_symlink() or not path.is_file():
                    raise ValueError("training_sample_probe_cache_missing")
                if transfer == "hardlink":
                    os.link(path, destination / name)
                else:
                    shutil.copy2(path, destination / name)
                copied += path.stat().st_size
            manifest = json.loads((destination / "complete.json").read_text(encoding="utf-8"))
            database = destination / "samples.sqlite3"
            if (not isinstance(manifest, dict) or manifest.get("sha256") != _file_sha256(database)
                    or manifest.get("size_bytes") != database.stat().st_size
                    or manifest.get("modified_ns") != database.stat().st_mtime_ns):
                raise ValueError("training_sample_probe_cache_invalid")
        result["cache_verified"] = True
        result["copy_verification_seconds"] = time.monotonic() - copy_started
        result["preparation_seconds"] = time.monotonic() - started
        result["linked_bytes" if transfer == "hardlink" else "copied_bytes"] = copied
        result["run"] = _train_once(runtime, history, target, selected)
        after = _sample_signatures(preparation_root, selected)
        result["source_cache_signatures_after"] = after
        result["source_cache_unchanged"] = before == after
        result["target_cache_cleared"] = all(
            not (target / "samples" / profile).exists() and not (target / "samples" / profile).is_symlink()
            for profile in selected
        )
        result["failures"] = TrainingSampleReuseFailures(initial, result, profiles)
        if transfer == "hardlink" and not result["failures"]:
            _release_prepared_samples(preparation_root, selected)
            result["preparation_cache_released_after_verification"] = True
    except (OSError, ValueError) as exc:
        result["failures"] = (str(exc) if isinstance(exc, ValueError) and str(exc).startswith("training_sample_probe_")
                              else "training_sample_probe_" + type(exc).__name__,)
    return result


def _release_prepared_samples(work: Path, profiles: tuple[ScoringProfileId, ...]) -> None:
    root = work / "samples"
    if root.is_symlink() or {path.name for path in root.iterdir()} != set(profiles):
        raise ValueError("training_sample_probe_preparation_cleanup_unowned")
    directories = tuple(root / profile for profile in profiles)
    for directory in directories:
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("training_sample_probe_preparation_cleanup_unowned")
        if {path.name for path in directory.iterdir()} != {"samples.sqlite3", "complete.json"} or any(
            path.is_symlink() or not path.is_file() for path in directory.iterdir()
        ):
            raise ValueError("training_sample_probe_preparation_cleanup_unowned")
    for directory in directories:
        for path in directory.iterdir():
            path.unlink()
        directory.rmdir()
    root.rmdir()


def _prepare_sample_reuse(
    runtime: RuntimeSettings,
    history: Path,
    work: Path,
    profiles: tuple[ScoringProfileId, ...],
) -> dict[str, object]:
    """Exercise the normal failed-fit cache retention path in an isolated preparation run."""
    from trader.bootstrap import ExecuteSelectedTraining
    from trader.training.infra import engine

    if work.exists() or work.is_symlink():
        raise ValueError("training_sample_probe_preparation_exists")
    work.mkdir(parents=True)
    expected = {(work / "samples" / profile).resolve(): profile for profile in profiles}
    calls = dict.fromkeys(profiles, 0)
    unexpected_workspaces = 0
    lock = Lock()
    original_fit = engine.FitIndustryModels

    def stop_before_fit(samples: SQLiteTrainingSampleIndex, *_args: object, **_kwargs: object) -> NoReturn:
        nonlocal unexpected_workspaces
        with lock:
            profile = expected.get(samples.workspace.resolve())
            if profile is None:
                unexpected_workspaces += 1
            else:
                calls[profile] += 1
        raise ValueError("training_sample_probe_controlled_fit_failure")

    captured = io.StringIO()
    started = time.monotonic()
    try:
        engine.FitIndustryModels = stop_before_fit
        with TrainingProcessMeasurement(disk_root=work) as measurement, redirect_stdout(captured):
            code = ExecuteSelectedTraining(runtime, profiles, history_root=history, training_root=work)
    finally:
        engine.FitIndustryModels = original_fit
    reports = ReadJsonReports(captured.getvalue())
    return {
        "method": "controlled_failure_before_fit",
        "fault_injected": True,
        "scope": "isolated_sample_preparation_only; not_a_training_workload_result",
        "fitter_restored": engine.FitIndustryModels is original_fit,
        "fit_entry_counts": calls,
        "unexpected_workspaces": unexpected_workspaces,
        "model_files_created": sum(len(tuple((work / "models").rglob(name))) for name in ("model.json", "metadata.json")),
        "exit_code": code,
        "seconds": time.monotonic() - started,
        "resources": measurement.result,
        "profiles": [item for item in reports if item.get("schema_version") == "profile_training_result"],
        "execution": next((item for item in reports if item.get("schema_version") == "selected_training_result"), None),
        "cache_conditions": "preparation_scans_history_and_warms_operating_system_cache",
    }


def _sample_signatures(
    work: Path, profiles: tuple[ScoringProfileId, ...]
) -> tuple[tuple[str, str, int, int, str], ...]:
    return tuple(
        (
            profile,
            hashlib.sha256((work / "samples" / profile / "complete.json").read_bytes()).hexdigest(),
            (work / "samples" / profile / "samples.sqlite3").stat().st_size,
            (work / "samples" / profile / "samples.sqlite3").stat().st_mtime_ns,
            _file_sha256(work / "samples" / profile / "samples.sqlite3"),
        )
        for profile in profiles
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024**2), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _train_once(
    runtime: RuntimeSettings, history: Path, work: Path, profiles: tuple[ScoringProfileId, ...]
) -> dict[str, object]:
    from trader.bootstrap import ExecuteSelectedTraining
    from trader.recommendation.infra.scoring.profile_factory import LoadScoringProfile

    captured = io.StringIO()
    started = time.monotonic()
    with TrainingProcessMeasurement(disk_root=work) as measurement, redirect_stdout(captured):
        code = ExecuteSelectedTraining(runtime, profiles, history_root=history, training_root=work)
    seconds = time.monotonic() - started
    reports = ReadJsonReports(captured.getvalue())
    results = [item for item in reports if item.get("schema_version") == "profile_training_result"]
    reloaded_heads = sum(
        len(LoadScoringProfile(profile, training_root=work / "models").heads)
        for profile in profiles
        if any(item.get("profile") == profile and item.get("status") == "engineering_ready" for item in results)
    )
    complete = code == 0 and reloaded_heads == len(profiles) * 2 and len(results) == len(profiles)
    return {
        "complete": complete,
        "exit_code": code,
        "profiles": results,
        "execution": next((item for item in reports if item.get("schema_version") == "selected_training_result"), None),
        "reloaded_heads": reloaded_heads,
        "seconds": seconds,
        "resources": measurement.result,
        "numeric_fingerprints": _numeric_fingerprints(work / "models", profiles) if complete else (),
    }


def _numeric_fingerprints(model_root: Path, profiles: tuple[ScoringProfileId, ...]) -> tuple[dict[str, object], ...]:
    fingerprints: list[dict[str, object]] = []
    metric_fields = (
        "prediction_metrics",
        "industry_prediction_metrics",
        "calibration_metrics",
        "rejected_industries",
        "evaluation_coverage",
        "target_metrics",
        "training_rows",
        "validation_rows",
    )
    for profile in profiles:
        for strategy in ("tomorrow", "d25"):
            directory = model_root / profile / strategy
            documents = []
            for name in ("model.json", "metadata.json"):
                path = directory / name
                if path.stat().st_size > 32 * 1024**2:
                    raise ValueError("training_alignment_model_document_too_large")
                value = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(value, dict):
                    raise ValueError("training_alignment_model_document_invalid")
                documents.append(value)
            model, metadata = documents
            report = metadata["validation_report"]
            if not isinstance(report, dict):
                raise ValueError("training_alignment_validation_report_invalid")
            components = model["components"] if profile == "v1" else model["industries"]
            metrics = report["validation"] if profile == "v1" else {field: report[field] for field in metric_fields}
            numeric = json.dumps((components, metrics), ensure_ascii=True, sort_keys=True, allow_nan=False).encode()
            fingerprints.append(
                {
                    "profile": profile,
                    "strategy": strategy,
                    "model_values_and_metrics_sha256": hashlib.sha256(numeric).hexdigest(),
                }
            )
    return tuple(fingerprints)


def _select_product(checkout: Path | None) -> Path:
    project = TOOL_ROOT
    expected = (checkout or project).resolve(strict=True)
    if not (expected / "src/trader").is_dir():
        raise ValueError("training_alignment_product_checkout_invalid")
    if checkout is not None:
        sys.path.insert(0, str(expected / "src"))
    import trader

    if trader.__file__ is None or Path(trader.__file__).resolve().parents[2] != expected:
        raise ValueError("training_alignment_product_import_mismatch")
    return expected


def _peak_rss_bytes() -> int | None:
    try:
        import resource

        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(value if sys.platform == "darwin" else value * 1024)
    except ImportError:
        return None


def Main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-root", type=Path, required=True)
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--inputs-only", action="store_true")
    parser.add_argument("--profiles")
    parser.add_argument("--runtime-config", type=Path)
    parser.add_argument("--product-checkout", type=Path)
    parser.add_argument("--published-history-read-only", action="store_true")
    parser.add_argument("--sample-cache-transfer", choices=("copy", "hardlink"), default="copy")
    parser.add_argument("--scheduling-candidate", choices=("baseline", "fixed", "long_task_first", "borrowing"), default="baseline")
    parser.add_argument("--scheduling-evidence", type=Path)
    parser.add_argument(
        "--computation-candidate", choices=("baseline", "indexed_anchors", "schema_cache", "combined"), default="baseline"
    )
    parser.add_argument(
        "--benchmark-mode",
        choices=("serial_before", "parallel", "serial_after", "baseline_before", "candidate", "baseline_after"),
    )
    args = parser.parse_args()
    try:
        if args.inputs_only and (args.scheduling_candidate != "baseline" or args.scheduling_evidence is not None):
            raise ValueError("training_scheduling_requires_training_mode")
        if args.inputs_only and args.computation_candidate != "baseline":
            raise ValueError("training_computation_requires_training_mode")
        product = _select_product(args.product_checkout)
        if args.work_root is not None:
            RequireIndependentOutput(args.work_root, product)
        from trader.recommendation.domain.scoring.profile_identity import SCORING_PROFILE_IDS
        from trader.recommendation.domain.scoring.profile_selection import ParseProfileSelection

        runtime_config = args.runtime_config or product / "config/runtime.json"
        sqlite_policy = None
        # Explicit historical checkouts predate the injected disk policy.
        if (product / "src/trader/infra/sqlite_policy.py").is_file():
            from trader.bootstrap import BuildSqlitePolicy
            from trader.infra.settings import LoadRuntimeSettings

            sqlite_policy = BuildSqlitePolicy(LoadRuntimeSettings(runtime_config))
        profiles = ParseProfileSelection(args.profiles) if args.profiles is not None else SCORING_PROFILE_IDS
        if args.inputs_only:
            report = _inputs(args.history_root, runtime_config, sqlite_policy=sqlite_policy)
        elif args.work_root is not None:
            measurement_options: _MeasurementOptions = {}
            if args.published_history_read_only:
                measurement_options["published_history_read_only"] = True
            if args.sample_cache_transfer != "copy":
                measurement_options["sample_cache_transfer"] = args.sample_cache_transfer
            if args.scheduling_candidate != "baseline":
                measurement_options["scheduling_candidate"] = args.scheduling_candidate
            if args.scheduling_evidence is not None:
                measurement_options["scheduling_evidence"] = args.scheduling_evidence
            if args.computation_candidate != "baseline":
                measurement_options["computation_candidate"] = args.computation_candidate
            report = _probe(
                args.history_root, args.work_root, profiles, runtime_config=runtime_config, sqlite_policy=sqlite_policy,
                require_reuse=args.benchmark_mode not in {"baseline_before", "baseline_after"},
                **measurement_options,
            )
            report["benchmark_mode"] = args.benchmark_mode
            report["product_checkout"] = str(product)
        else:
            raise ValueError("training_alignment_requires_output")
    except (OSError, RuntimeError, ValueError) as exc:
        report = {
            "schema_version": "training-alignment-diagnostic",
            "status": "failed",
            "summary": {"failure": type(exc).__name__},
        }
    emit_report(report)
    return int(report["status"] == "failed")
