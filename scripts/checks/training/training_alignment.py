"""Train all alignment contracts against an explicitly external frozen history copy."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import shutil
import sys
import time
from collections import Counter
from contextlib import redirect_stdout
from datetime import datetime
from datetime import time as daytime
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, NoReturn, TypedDict
from zoneinfo import ZoneInfo

from scripts.checks.reporting import ReadJsonReports, emit_report
from scripts.checks.training.training_process_measurement import TrainingProcessMeasurement

if TYPE_CHECKING:
    from trader.infra.settings import RuntimeSettings
    from trader.infra.sqlite_policy import SqliteIoPolicy
    from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
    from trader.training.infra.sample_index import SQLiteTrainingSampleIndex


class _SqliteArguments(TypedDict, total=False):
    sqlite_policy: SqliteIoPolicy


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
    from trader.training.infra.history.history_training_input import SQLiteHistoryTrainingInputSnapshot

    project = Path(__file__).resolve().parents[3]
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
    ), *(
        (source.parent / "reference" / name, Path("reference") / name)
        for name in ("company_st.sqlite3", "industry.sqlite3")
    ))
    if len(files) < 4 or any(path.is_symlink() or not path.is_file() for path, _relative in files):
        raise ValueError("training_history_copy_source_incomplete")
    required = sum(path.stat().st_size for path, _relative in files)
    existing = destination.parent
    while not existing.exists():
        existing = existing.parent
    if shutil.disk_usage(existing).free < required * 2 + 1024**3:
        raise ValueError("training_history_copy_space_insufficient")
    destination.mkdir(parents=True, exist_ok=False)
    deadline = started + timeout_seconds
    for number, (path, relative) in enumerate(files, 1):
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
        print(f"训练历史副本 | 已备份 {number}/{len(files)} 个数据库", file=sys.stderr, flush=True)
    _require_history_copy_reference(destination / "history", sqlite_policy)
    _require_history_copy_reference(source, sqlite_policy)
    copied = SQLiteHistoryTrainingInputSnapshot.open(destination / "history", sqlite_policy=sqlite_policy).snapshot
    after = SQLiteHistoryTrainingInputSnapshot.open(source, sqlite_policy=sqlite_policy).snapshot
    if copied != snapshot or after != snapshot:
        raise ValueError("training_history_copy_source_changed")
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
        "copy_method": "sqlite_online_backup; catalog_and_reference_identity_verified",
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
) -> dict[str, object]:
    # Load diagnostic types only after main selects the measured product checkout.
    from scripts.checks.training.training_performance import TrainingReuseFailures

    from trader.infra.settings import LoadRuntimeSettings
    from trader.recommendation.domain.market.history_alignment import HISTORY_ALIGNMENT_CONTRACT
    from trader.recommendation.domain.scoring.profile_identity import SCORING_PROFILE_IDS
    from trader.training.entrypoints.commands import ConfigureTrainingResources
    from trader.training.infra.history.history_training_input import SQLiteHistoryTrainingInputSnapshot
    from trader.training.infra.implementation_identity import TrainingImplementationIdentity

    project = Path(__file__).resolve().parents[3]
    history, work = history.resolve(), work.resolve()
    if history.is_relative_to(project) or work.is_relative_to(project) or work == history:
        raise ValueError("training_alignment_requires_external_history_copy")
    if work.exists() or history.is_relative_to(work) or work.is_relative_to(history):
        raise ValueError("training_alignment_requires_new_separate_output")
    sqlite_arguments: _SqliteArguments = {}
    if sqlite_policy is not None:
        sqlite_arguments["sqlite_policy"] = sqlite_policy
    source = SQLiteHistoryTrainingInputSnapshot.open(history, **sqlite_arguments)
    identity = source.snapshot.snapshot_hash
    work.mkdir(parents=True)
    started = time.monotonic()
    profiles = SCORING_PROFILE_IDS if profiles is None else profiles
    config = (runtime_config or project / "config/runtime.json").resolve()
    runtime = LoadRuntimeSettings(config)
    configuration_hash = hashlib.sha256(
        b"".join(
            hashlib.sha256((runtime.project_root / "config" / name).read_bytes()).digest()
            for name in ("training.json", "features.json")
        )
    ).hexdigest()
    ConfigureTrainingResources()
    initial = _train_once(runtime, history, work, profiles)
    repeat = _train_once(runtime, history, work, profiles) if initial["complete"] else None
    sample_reuse = _sample_reuse(runtime, history, work, profiles, initial) if initial["complete"] else None
    unchanged = SQLiteHistoryTrainingInputSnapshot.open(history, **sqlite_arguments).snapshot.snapshot_hash == identity
    complete = bool(initial["complete"] and repeat is not None and repeat["complete"] and unchanged)
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
            "repeat_model_values_equal": equal,
            "repeat_reuse_verified": not reuse_failures,
            "repeat_reuse_required": require_reuse,
            "repeat_reuse_failures": reuse_failures,
            "source_codes": len(source.snapshot.training_codes),
            "source_identity_unchanged": unchanged,
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


def _sample_reuse(
    runtime: RuntimeSettings,
    history: Path,
    work: Path,
    profiles: tuple[ScoringProfileId, ...],
    initial: dict[str, object],
) -> dict[str, object]:
    from scripts.checks.training.training_performance import TrainingSamplePreparationFailures, TrainingSampleReuseFailures

    selected = tuple(profile for profile in profiles if profile != "v1")
    result: dict[str, object] = {
        "profiles": selected,
        "not_applicable_profiles": tuple(profile for profile in profiles if profile == "v1"),
        "run": None,
        "preparation": None,
        "preparation_seconds": None,
        "copy_verification_seconds": None,
        "copied_bytes": 0,
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
                shutil.copy2(path, destination / name)
                copied += path.stat().st_size
            manifest = json.loads((destination / "complete.json").read_text(encoding="utf-8"))
            database = destination / "samples.sqlite3"
            digest = hashlib.sha256()
            with database.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024**2), b""):
                    digest.update(chunk)
            if (not isinstance(manifest, dict) or manifest.get("sha256") != digest.hexdigest()
                    or manifest.get("size_bytes") != database.stat().st_size
                    or manifest.get("modified_ns") != database.stat().st_mtime_ns):
                raise ValueError("training_sample_probe_cache_invalid")
        result["cache_verified"] = True
        result["copy_verification_seconds"] = time.monotonic() - copy_started
        result["preparation_seconds"] = time.monotonic() - started
        result["copied_bytes"] = copied
        result["run"] = _train_once(runtime, history, target, selected)
        result["source_cache_unchanged"] = before == _sample_signatures(preparation_root, selected)
        result["target_cache_cleared"] = all(
            not (target / "samples" / profile).exists() and not (target / "samples" / profile).is_symlink()
            for profile in selected
        )
        result["failures"] = TrainingSampleReuseFailures(initial, result, profiles)
    except (OSError, ValueError) as exc:
        result["failures"] = (str(exc) if isinstance(exc, ValueError) and str(exc).startswith("training_sample_probe_")
                              else "training_sample_probe_" + type(exc).__name__,)
    return result


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
        with TrainingProcessMeasurement() as measurement, redirect_stdout(captured):
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
) -> tuple[tuple[str, str, int, int], ...]:
    return tuple(
        (
            profile,
            hashlib.sha256((work / "samples" / profile / "complete.json").read_bytes()).hexdigest(),
            (work / "samples" / profile / "samples.sqlite3").stat().st_size,
            (work / "samples" / profile / "samples.sqlite3").stat().st_mtime_ns,
        )
        for profile in profiles
    )


def _train_once(
    runtime: RuntimeSettings, history: Path, work: Path, profiles: tuple[ScoringProfileId, ...]
) -> dict[str, object]:
    from trader.bootstrap import ExecuteSelectedTraining
    from trader.recommendation.infra.scoring.profile_factory import LoadScoringProfile

    captured = io.StringIO()
    started = time.monotonic()
    with TrainingProcessMeasurement() as measurement, redirect_stdout(captured):
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
    project = Path(__file__).resolve().parents[3]
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
    parser.add_argument(
        "--benchmark-mode",
        choices=("serial_before", "parallel", "serial_after", "baseline_before", "candidate", "baseline_after"),
    )
    args = parser.parse_args()
    try:
        product = _select_product(args.product_checkout)
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
            report = _probe(
                args.history_root, args.work_root, profiles, runtime_config=runtime_config, sqlite_policy=sqlite_policy,
                require_reuse=args.benchmark_mode not in {"baseline_before", "baseline_after"},
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
