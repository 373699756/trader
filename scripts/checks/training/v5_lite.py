"""Bounded paired V5 lightweight compute and real-sample acceptance diagnostic."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy

import argparse
import hashlib
import json
import math
import os
import sqlite3
import shutil
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import cast

import numpy as np

from trader.infra.atomic_files.json import AtomicReadJson, AtomicWriteJson
from trader.infra.settings.parser import Integer, ProfileMapping, ReadJsonObject, RequireExactKeys, RequireMapping
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession, SqliteTransaction
from trader.training.application.tomorrow_training import TrainingFitDetail
from trader.training.infra.model_bundles.bundle_codec import TrainedTcnModelSnapshot
from trader.training.infra.model_bundles.contracts import TrainedHeadContract
from trader.training.infra.model_bundles.documents import BuildIndustryModelDocument
from trader.training.infra.model_evaluation import PredictionMetricsAccumulator
from trader.training.infra.model_fitting import IndustryCoverageFailure
from trader.training.infra.model_fitting_results import FittedIndustryModel
from trader.training.infra.profile.v5.contracts import V5_TRAINING_PROFILE
from trader.training.infra.profile.v5.configuration import ParseTcnParameters, TcnConfigurationDocument
from trader.training.infra.profile.v5.settings import TcnArchitecture, TcnFittingParameters
from trader.training.infra.sample_index import SQLiteTrainingSampleIndex, TrainingIndustryCounts
from trader.training.infra.sample_sequences import SampleSequenceLayout
from trader.training.infra.sample_schema import SampleFormatId


def ProbeLiteKernels() -> dict[str, object]:
    import torch

    from trader.training.infra.profile.v5.network import ConfigureTcnRuntime, TcnNetwork

    available = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 1)
    cases = [(width, threads) for width in (8, 4) for threads in (1, 2, 4) if threads <= available]
    measurements: dict[str, list[float]] = {f"channels_{width}_threads_{threads}": [] for width, threads in cases}
    for repetition in range(5):
        for width, threads in cases if repetition % 2 else reversed(cases):
            ConfigureTcnRuntime(training=True, compute_threads=threads)
            torch.manual_seed(repetition)
            values = torch.randn(256, 366, dtype=torch.float64)
            labels = torch.randn(256, dtype=torch.float64)
            model = TcnNetwork(6, 0.1, TcnArchitecture(width, "float32"))
            optimizer = torch.optim.AdamW(model.parameters(), lr=0.001, weight_decay=0.0001)
            for step in range(7):
                if step == 2:
                    started = time.perf_counter()
                optimizer.zero_grad(set_to_none=True)
                # Include the production batch dtype conversions in measured work.
                loss = torch.square(model(values.to(model.compute_dtype)) - labels.to(model.compute_dtype)).mean()
                loss.backward()
                optimizer.step()
            measurements[f"channels_{width}_threads_{threads}"].append((time.perf_counter() - started) / 5)
    return {
        "seconds_per_batch": measurements,
        "median_seconds_per_batch": {name: statistics.median(values) for name, values in measurements.items()},
        "quality_evidence": False,
        "batch_size": 256,
        "torch_version": torch.__version__,
    }


def QualityPassed(reference: dict[str, object], candidate: dict[str, object]) -> bool:
    baseline_rmse, trial_rmse = reference.get("root_mean_squared_error"), candidate.get("root_mean_squared_error")
    baseline_ic, trial_ic = reference.get("mean_daily_rank_ic"), candidate.get("mean_daily_rank_ic")
    if any(
        type(value) not in (int, float) or not math.isfinite(cast(float, value))
        for value in (baseline_rmse, trial_rmse, baseline_ic, trial_ic)
    ):
        return False
    return (
        cast(float, trial_rmse) <= cast(float, baseline_rmse) * 1.05
        and cast(float, trial_ic) >= cast(float, baseline_ic) - 0.01
    )


def _digest(path: Path, deadline: float) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024**2):
            _check(deadline)
            digest.update(chunk)
    return digest.hexdigest()


def _check(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise TimeoutError("v5_lite_comparison_budget_exhausted")


def _peak_rss_mib() -> float | None:
    try:
        import resource
    except ImportError:
        return None
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value / (1024**2 if sys.platform == "darwin" else 1024)


def _sample_signature(source: Path) -> tuple[tuple[int, int, int, int, int], ...]:
    stamps = tuple((source / name).stat() for name in ("complete.json", "samples.sqlite3"))
    return tuple((stamp.st_dev, stamp.st_ino, stamp.st_size, stamp.st_mtime_ns, stamp.st_ctime_ns) for stamp in stamps)


def _prepare(source: Path, work: Path, deadline: float, fit_dates: int, *, sqlite_policy: SqliteIoPolicy) -> tuple[Path, Path]:
    if source.is_symlink() or any((source / name).is_symlink() for name in ("complete.json", "samples.sqlite3")):
        raise ValueError("V5 comparison source must not contain symbolic links")
    source_files = (source / "complete.json", source / "samples.sqlite3")
    if not all(path.is_file() for path in source_files):
        raise ValueError("V5 comparison source requires regular files")

    before = _sample_signature(source)
    manifest = AtomicReadJson(source / "complete.json")
    if not isinstance(manifest, dict) or manifest.get("sample_format") != SampleFormatId(V5_TRAINING_PROFILE.heads[0].feature_manifest.names, SampleSequenceLayout(6, 60, 6)):
        raise ValueError("V5 comparison requires a sealed compact sample cache")
    database = source / "samples.sqlite3"
    if shutil.disk_usage(work).free < database.stat().st_size * 2 + 512 * 1024**2:
        raise ValueError("V5 comparison snapshot space insufficient")
    baseline, candidate = work / "reference.sqlite3", work / "candidate.sqlite3"
    # Exact external snapshots; source and its manifest are never modified.
    for target in (baseline, candidate):
        with database.open("rb") as reader, target.open("xb") as writer:
            while chunk := reader.read(1024**2):
                _check(deadline)
                writer.write(chunk)
    if before != _sample_signature(source):
        raise ValueError("V5 sample source changed during snapshot")
    if _digest(baseline, deadline) != manifest.get("sha256") or _digest(candidate, deadline) != manifest.get("sha256"):
        raise ValueError("V5 sample snapshot digest mismatch")
    for path in (baseline, candidate):
        with SqliteSession(path, SqliteConnectionOptions(timeout_seconds=1), sqlite_policy=sqlite_policy) as connection:
            days = connection.run(
                "SELECT trade_date FROM sample_split_dates WHERE split_name='training' ORDER BY trade_date"
            ).fetchall()
            if fit_dates < 61 or len(days) < fit_dates:
                raise ValueError("V5 sample cache has insufficient fitting dates")
            if len(days) > fit_dates:
                with SqliteTransaction(connection, immediate=True):
                    connection.run(
                        "DELETE FROM sample_split_dates WHERE split_name='training' AND trade_date<?", (days[-fit_dates][0],)
                    )
                    previous = connection.run("SELECT split_hash FROM sample_contract WHERE singleton=1").require_one()[0]
                    diagnostic_hash = hashlib.sha256(previous + f"diagnostic_tail_{fit_dates}".encode()).digest()
                    connection.run("UPDATE sample_contract SET split_hash=? WHERE singleton=1", (diagnostic_hash,))
    return baseline, candidate


def _sample_evidence(source: Path) -> dict[str, object]:
    manifest = AtomicReadJson(source / "complete.json")
    if not isinstance(manifest, dict):
        raise ValueError("V5 sample manifest is not an object")
    projection = {
        name: manifest[name]
        for name in ("identity", "sha256", "sample_format", "split_hash", "size_bytes", "modified_ns")
        if name in manifest and type(manifest[name]) in (str, int)
    }
    input_path = source / "quality-input.json"
    provenance: dict[str, object] | None = None
    if input_path.is_file():
        value = AtomicReadJson(input_path)
        if not isinstance(value, dict):
            raise ValueError("V5 quality input is not an object")
        provenance = {
            name: value[name]
            for name in (
                "snapshot_hash", "source_identity_hash", "input_descriptor_hash", "source_cutoff", "split_hash",
                "sample_identity", "rows", "source_codes", "fit_dates", "reused", "source_data_writes", "models_published",
            )
            if name in value and type(value[name]) in (str, int, bool)
        }
    return {"sample_manifest": projection, "quality_input": provenance}


def _parameters(path: Path, *, archived: bool = False) -> tuple[int, TcnFittingParameters]:
    from trader.infra.settings.training import ReadTrainingConfiguration

    if archived:
        # An explicitly supplied frozen comparison may predate the resource schema.
        document = ReadJsonObject(path)
        RequireExactKeys(document, {"profiles"} | ({"resources"} & document.keys()), "training")
    else:
        document = ReadTrainingConfiguration(path)
    profile = ProfileMapping(document.get("profiles"), "v5", "training.profiles")
    RequireExactKeys(profile, {"fit_dates", "minimum_industry_training_rows", "tcn"}, "training.v5")
    return Integer(profile, "fit_dates", minimum=61), ParseTcnParameters(
        RequireMapping(profile, "tcn"), Integer(profile, "minimum_industry_training_rows", minimum=1)
    )


@dataclass
class _FitTimer:
    work: Path
    deadline: float
    phase: str = "preparation"
    changed: float = field(default_factory=time.monotonic)
    last_report: float = 0.0
    epoch: int = 0
    seconds: dict[str, float] = field(default_factory=dict)

    def switch(self, phase: str) -> None:
        now = time.monotonic()
        self.seconds[self.phase] = self.seconds.get(self.phase, 0.0) + now - self.changed
        self.phase, self.changed = phase, now

    def progress(self, detail: TrainingFitDetail) -> None:
        _check(self.deadline)
        self.switch(detail.phase)
        self.epoch = max(self.epoch, detail.epoch)
        if time.monotonic() - self.last_report >= 10:
            AtomicWriteJson(self.work / "progress.json", {**asdict(detail), "phase_seconds": self.seconds}, indent=2)
            self.last_report = time.monotonic()


def _quality(
    source: Path,
    work: Path,
    deadline: float,
    baseline: tuple[int, TcnFittingParameters],
    candidate: tuple[int, TcnFittingParameters], *, sqlite_policy: SqliteIoPolicy,
) -> dict[str, object]:
    if baseline[0] != candidate[0] or baseline[1].minimum_industry_training_rows != candidate[1].minimum_industry_training_rows:
        raise ValueError("V5 compute comparison requires identical fit dates and industry thresholds")
    from trader.training.infra.profile.v5.fitting import FitTcnIndustry
    from trader.training.infra.profile.v5.network import ConfigureTcnRuntime, PreserveTcnRuntime, TcnNetwork

    prepared = time.monotonic()
    paths = _prepare(source, work, deadline, baseline[0], sqlite_policy=sqlite_policy)
    preparation_seconds = time.monotonic() - prepared
    reference, options = baseline[1], candidate[1]
    summaries: list[dict[str, object]] = []

    split_hashes = []
    for path in paths:
        with SqliteSession(path, SqliteConnectionOptions(read_only=True), sqlite_policy=sqlite_policy) as connection:
            split_hashes.append(connection.run("SELECT split_hash FROM sample_contract WHERE singleton=1").require_one()[0].hex())
    feature_names = V5_TRAINING_PROFILE.heads[0].feature_manifest.names
    with (
        PreserveTcnRuntime(),
        SQLiteTrainingSampleIndex(
            paths[0], feature_names, prepared_split_hash=split_hashes[0], sequence_layout=SampleSequenceLayout(6, 60, 6), sqlite_policy=sqlite_policy
        ) as old,
        SQLiteTrainingSampleIndex(
            paths[1], feature_names, prepared_split_hash=split_hashes[1], sequence_layout=SampleSequenceLayout(6, 60, 6), sqlite_policy=sqlite_policy
        ) as new,
    ):
        by_head = [{count.industry: count for count in new.industry_counts(head)} for head in V5_TRAINING_PROFILE.heads]
        eligible = sorted(
            (
                name
                for name in by_head[0]
                if all(name in counts and not IndustryCoverageFailure(counts[name], options) for counts in by_head)
            ),
            key=lambda name: (by_head[0][name].training, name),
        )
        if len(eligible) < 3:
            raise ValueError("V5 needs three eligible industries in the configured fitting window")
        selected = (eligible[0], eligible[len(eligible) // 2], eligible[-1])
        coverage = {
            "fit_dates": baseline[0],
            "eligible_industries": len(eligible),
            "snapshot_and_verification_seconds": preparation_seconds,
            "candidate_industries": len(by_head[0]),
            "selected": selected,
            "excluded": sorted(set(by_head[0]) - set(eligible)),
            **_sample_evidence(source),
        }
        AtomicWriteJson(work / "coverage.json", coverage, indent=2)
        for industry in selected:
            for head_index, head in enumerate(V5_TRAINING_PROFILE.heads):
                pair: dict[str, dict[str, object]] = {}
                cases = (("candidate", new, options), ("reference", old, reference))
                for name, samples, parameters in cases if len(summaries) % 2 == 0 else reversed(cases):
                    _check(deadline)
                    counts = by_head[head_index][industry]
                    AtomicWriteJson(
                        work / "current.json", {"case": name, "head": head.strategy.value, "industry": industry}, indent=2
                    )
                    started = time.monotonic()
                    timer = _FitTimer(work, deadline)
                    ConfigureTcnRuntime(training=True, compute_threads=parameters.compute_threads)
                    fitted = FitTcnIndustry(
                        samples, counts, head, parameters, lambda: _check(deadline), report=timer.progress
                    )
                    timer.switch("reload_and_validation")
                    model = fitted.model
                    if not isinstance(model, TrainedTcnModelSnapshot):
                        raise ValueError("V5 comparison model type mismatch")
                    network = TcnNetwork(len(model.transformer_means) - 360, model.dropout, model.architecture)
                    network.load_tensors(model.weights)
                    ConfigureTcnRuntime(training=False)
                    metrics = PredictionMetricsAccumulator()
                    for batch in samples.iter_batches(industry, head, "validation"):
                        _check(deadline)
                        values = (batch.features - np.asarray(model.transformer_means)) / np.asarray(
                            model.transformer_scales
                        )
                        predicted = model.calibration_intercept + model.calibration_slope * network.predict(values)
                        for day, code, target, prediction in zip(
                            batch.trade_dates, batch.codes, batch.labels, predicted, strict=True
                        ):
                            metrics.add(day, code, industry, float(target), float(prediction))
                    timer.switch("completed")
                    pair[name] = {
                        **asdict(metrics.finish()),
                        "seconds": time.monotonic() - started,
                        "training_rows": counts.training,
                        "actual_epochs": timer.epoch,
                        "phase_seconds": timer.seconds,
                        "process_peak_rss_mib": _peak_rss_mib(),
                        "parameters": asdict(parameters),
                    }
                    AtomicWriteJson(work / "latest_pair.json", pair, indent=2)
                passed = QualityPassed(pair["reference"], pair["candidate"])
                summaries.append({
                    "industry": industry, "head": head.strategy.value, "passed": passed, "cases": pair,
                    "time_ratio": cast(float, pair["candidate"]["seconds"]) / cast(float, pair["reference"]["seconds"]),
                })
                AtomicWriteJson(work / "comparisons.json", {"comparisons": summaries}, indent=2)
                if not passed:
                    return {
                        "passed": False,
                        "reason": "quality_gate_failed",
                        "coverage": coverage,
                        "comparisons": summaries,
                        "stopped_after_failure": True,
                    }
    return {"passed": True, "coverage": coverage, "comparisons": summaries}


class _FitCancelled(InterruptedError):
    """Explicit diagnostic cancellation at a completed training batch."""


def _reload_model(fitted: FittedIndustryModel, path: Path) -> TrainedTcnModelSnapshot:
    from trader.training.infra.profile.v5.tensors import DecodeTcnNetwork

    model = fitted.model
    if not isinstance(model, TrainedTcnModelSnapshot):
        raise ValueError("V5 lifecycle model type mismatch")
    AtomicWriteJson(path, BuildIndustryModelDocument((fitted,)), indent=2)
    document = ReadJsonObject(path)
    industries = RequireMapping(document, "industries")
    values = RequireMapping(industries, fitted.industry)
    dropout, weights, architecture = DecodeTcnNetwork(values["tcn_network"], len(model.transformer_means) - 360)
    # The production document owner projects the normalization and calibration too.
    expected = {
        "transformer_means": list(model.transformer_means),
        "transformer_scales": list(model.transformer_scales),
        "calibration_intercept": model.calibration_intercept,
        "calibration_slope": model.calibration_slope,
    }
    if any(values.get(name) != value for name, value in expected.items()):
        raise ValueError("V5 lifecycle normalization or calibration changed during reload")
    reloaded = replace(model, dropout=dropout, weights=weights, architecture=architecture)
    if reloaded != model:
        raise ValueError("V5 lifecycle tensors changed during reload")
    return reloaded


def _validate_reload(
    samples: SQLiteTrainingSampleIndex,
    counts: TrainingIndustryCounts,
    head: TrainedHeadContract,
    fitted: FittedIndustryModel,
    work: Path,
    deadline: float,
) -> dict[str, object]:
    from trader.training.infra.profile.v5.network import ConfigureTcnRuntime, TcnNetwork

    model = fitted.model
    if not isinstance(model, TrainedTcnModelSnapshot):
        raise ValueError("V5 lifecycle model type mismatch")
    loaded = _reload_model(fitted, work / f"{head.strategy.value}-model.json")
    original = TcnNetwork(len(model.transformer_means) - 360, model.dropout, model.architecture)
    original.load_tensors(model.weights)
    reloaded = TcnNetwork(len(loaded.transformer_means) - 360, loaded.dropout, loaded.architecture)
    reloaded.load_tensors(loaded.weights)
    ConfigureTcnRuntime(training=False)
    metrics = PredictionMetricsAccumulator()
    for batch in samples.iter_batches(counts.industry, head, "validation"):
        _check(deadline)
        values = (batch.features - np.asarray(model.transformer_means)) / np.asarray(model.transformer_scales)
        expected = model.calibration_intercept + model.calibration_slope * original.predict(values)
        actual = loaded.calibration_intercept + loaded.calibration_slope * reloaded.predict(values)
        if not np.all(np.isfinite(actual)) or not np.array_equal(expected, actual):
            raise ValueError("V5 lifecycle reload predictions changed or are nonfinite")
        for day, code, target, prediction in zip(batch.trade_dates, batch.codes, batch.labels, actual, strict=True):
            metrics.add(day, code, counts.industry, float(target), float(prediction))
    finished = metrics.finish()
    if finished.predicted_rows != counts.validation or finished.predicted_rows == 0:
        raise ValueError("V5 lifecycle validation coverage changed")
    return {**asdict(finished), "reload_predictions_exact": True, "model_snapshot_exact": True}


def _largest(
    source: Path,
    work: Path,
    deadline: float,
    configuration: tuple[int, TcnFittingParameters],
    *,
    sqlite_policy: SqliteIoPolicy,
    cancel_after_batches: int | None = None,
) -> dict[str, object]:
    from trader.training.infra.implementation_identity import TrainingImplementationIdentity
    from trader.training.infra.profile.v5.fitting import FitTcnIndustry
    from trader.training.infra.profile.v5.network import ConfigureTcnRuntime, PreserveTcnRuntime

    paths = _prepare(source, work, deadline, configuration[0], sqlite_policy=sqlite_policy)
    source_stamps = _sample_signature(source)
    manifest_hash = _digest(source / "complete.json", deadline)
    sample_hash = ReadJsonObject(source / "complete.json")["sha256"]
    with SqliteSession(paths[1], SqliteConnectionOptions(read_only=True), sqlite_policy=sqlite_policy) as connection:
        split_hash = connection.run("SELECT split_hash FROM sample_contract WHERE singleton=1").require_one()[0].hex()
    options = configuration[1]
    heads: list[dict[str, object]] = []
    try:
        with PreserveTcnRuntime(), SQLiteTrainingSampleIndex(
            paths[1], V5_TRAINING_PROFILE.heads[0].feature_manifest.names, prepared_split_hash=split_hash,
            sequence_layout=SampleSequenceLayout(6, 60, 6), sqlite_policy=sqlite_policy,
        ) as samples:
            by_head = [{count.industry: count for count in samples.industry_counts(head)} for head in V5_TRAINING_PROFILE.heads]
            eligible = [
                name for name in by_head[0]
                if all(name in counts and not IndustryCoverageFailure(counts[name], options) for counts in by_head)
            ]
            if not eligible:
                raise ValueError("V5 lifecycle requires a dual-head eligible industry")
            industry = max(eligible, key=lambda name: (by_head[0][name].training, name))
            coverage = {
                "selection": "largest_dual_head_eligible_by_tomorrow_training_rows",
                "industry": industry,
                "fit_dates": configuration[0],
                "eligible_industries": len(eligible),
                "training_implementation_identity": TrainingImplementationIdentity(),
                **_sample_evidence(source),
            }
            AtomicWriteJson(work / "coverage.json", coverage, indent=2)
            for index, head in enumerate(V5_TRAINING_PROFILE.heads):
                _check(deadline)
                counts = by_head[index][industry]
                timer = _FitTimer(work, deadline)

                def progress(detail: TrainingFitDetail, timer: _FitTimer = timer) -> None:
                    timer.progress(detail)
                    if cancel_after_batches is not None and detail.phase == "training" and detail.completed_batches >= cancel_after_batches:
                        raise _FitCancelled("explicit_batch_cancellation")

                AtomicWriteJson(work / "current.json", {"head": head.strategy.value, "industry": industry}, indent=2)
                ConfigureTcnRuntime(training=True, compute_threads=options.compute_threads)
                try:
                    fitted = FitTcnIndustry(samples, counts, head, options, lambda: _check(deadline), report=progress)
                except _FitCancelled:
                    timer.switch("cancelled")
                    outcome: dict[str, object] = {"fit_completed": False, "cancellation_observed": True}
                else:
                    if cancel_after_batches is not None:
                        raise ValueError("V5 lifecycle cancellation point was not reached")
                    timer.switch("reload_and_validation")
                    outcome = {"fit_completed": True, **_validate_reload(samples, counts, head, fitted, work, deadline)}
                    timer.switch("completed")
                if tuple(work.glob("tcn-batches-*")):
                    raise ValueError("V5 lifecycle batch workspace was not released")
                heads.append({
                    "head": head.strategy.value, "industry": industry, "counts": asdict(counts),
                    "actual_epochs": timer.epoch, "phase_seconds": timer.seconds,
                    "batch_workspaces_released": True, **outcome,
                })
                AtomicWriteJson(work / "heads.json", {"heads": heads}, indent=2)
    finally:
        if (
            _sample_signature(source) != source_stamps
            or _digest(source / "complete.json", deadline) != manifest_hash
            or _digest(source / "samples.sqlite3", deadline) != sample_hash
        ):
            raise ValueError("V5 lifecycle source changed")
    return {
        "passed": True, "coverage": coverage, "heads": heads, "parameters": asdict(options),
        "quality_evidence": False, "performance_evidence": False, "source_unchanged": True,
        "cancellation_only": cancel_after_batches is not None, "models_published": 0,
    }


def Main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--sample-root", type=Path)
    parser.add_argument("--candidate-config", type=Path)
    parser.add_argument("--reference-config", type=Path)
    parser.add_argument("--runtime-config", type=Path)
    parser.add_argument("--quality-only", action="store_true")
    parser.add_argument("--largest-only", action="store_true", help="single configured fit/reload per head; no relative quality evidence")
    parser.add_argument("--cancel-after-batches", type=int, help="largest-only cancellation exercise per head")
    parser.add_argument("--prepare-samples", action="store_true")
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    from trader.bootstrap import BuildSqlitePolicy
    from trader.infra.settings import LoadRuntimeSettings
    work = args.work_root.resolve()
    project = Path(__file__).resolve().parents[3]
    if args.largest_only and (args.quality_only or args.reference_config is not None or args.prepare_only):
        raise ValueError("V5 largest-only cannot run a quality comparison or prepare-only")
    if args.cancel_after_batches is not None and (not args.largest_only or args.cancel_after_batches < 1):
        raise ValueError("V5 cancellation requires largest-only and a positive batch count")
    if args.prepare_only and not args.prepare_samples:
        raise ValueError("V5 prepare-only requires explicit prepare-samples")
    if args.prepare_samples and args.sample_root is None:
        raise ValueError("V5 sample preparation requires an explicit sample root")
    if (args.quality_only or args.largest_only) and (args.sample_root is None or args.candidate_config is None):
        raise ValueError("V5 quality-only/largest-only requires samples and a candidate configuration")
    runtime = LoadRuntimeSettings(args.runtime_config or project / "config/runtime.json")
    sqlite_policy = BuildSqlitePolicy(runtime)
    if work.exists() or work.is_relative_to(project):
        raise ValueError("V5 lite diagnostic requires a new external work directory")
    if args.sample_root is not None and (
        work.is_relative_to(args.sample_root.resolve()) or args.sample_root.resolve().is_relative_to(work)
    ):
        raise ValueError("V5 lite output must be independent of source")
    work.mkdir(parents=True)
    report: dict[str, object] = {
        "schema_version": "v5-lite-diagnostic",
        "status": "degraded",
        "summary": {"full_training": False, "production_authority": False},
    }
    summary = report["summary"]
    assert isinstance(summary, dict)
    try:
        deadline = time.monotonic() + 3 * 3600
        baseline = _parameters(
            args.reference_config or project / "config" / "training.json", archived=args.reference_config is not None
        )
        if not args.largest_only:
            summary["reference_configuration"] = TcnConfigurationDocument(*baseline)
        candidate = _parameters(args.candidate_config, archived=True) if args.candidate_config is not None else None
        if candidate is not None:
            summary["candidate_configuration"] = TcnConfigurationDocument(*candidate)
        AtomicWriteJson(work / "result.json", report, indent=2)
        if args.prepare_samples:
            from scripts.checks.training.v5_quality_samples import PrepareV5QualitySamples

            prepared = PrepareV5QualitySamples(
                runtime,
                project,
                args.sample_root,
                work,
                deadline,
                sqlite_policy=sqlite_policy,
            )
            summary["sample_preparation"] = {
                "sample_root": str(prepared.root),
                "snapshot_hash": prepared.snapshot_hash,
                "source_identity_hash": prepared.source_identity_hash,
                "input_descriptor_hash": prepared.input_descriptor_hash,
                "source_cutoff": prepared.source_cutoff,
                "split_hash": prepared.split_hash,
                "sample_identity": prepared.sample_identity,
                "rows": prepared.rows,
                "source_codes": prepared.source_codes,
                "fit_dates": prepared.fit_dates,
                "reused": prepared.reused,
                "source_data_writes": 0,
                "models_published": 0,
            }
            AtomicWriteJson(prepared.root / "quality-input.json", summary["sample_preparation"], indent=2)
        if args.prepare_only:
            summary["reason"] = "sample_preparation_only"
            summary["quality_evidence"] = False
            report["status"] = "passed"
        elif args.quality_only or args.largest_only:
            summary["compute_probe_skipped"] = True
        else:
            summary["compute"] = ProbeLiteKernels()
        AtomicWriteJson(work / "result.json", report, indent=2)
        if args.largest_only:
            if candidate is None:
                raise ValueError("V5 largest-only requires a candidate configuration")
            summary["quality_evidence"] = False
            summary["performance_evidence"] = False
            summary["lifecycle"] = _largest(
                args.sample_root.absolute(), work, deadline, candidate, sqlite_policy=sqlite_policy,
                cancel_after_batches=args.cancel_after_batches,
            )
            report["status"] = "passed"
        elif not args.prepare_only and args.sample_root is not None and candidate is not None:
            summary["quality"] = quality = _quality(
                args.sample_root.absolute(), work, deadline, baseline, candidate, sqlite_policy=sqlite_policy
            )
            report["status"] = "passed" if quality["passed"] else "degraded"
        elif not args.prepare_only:
            summary["reason"] = "quality_not_requested"
    except (OSError, ValueError, RuntimeError, sqlite3.Error, ImportError) as exc:
        summary["failure"] = f"{type(exc).__name__}: {exc}"
    except KeyboardInterrupt:
        summary["failure"] = "interrupted"
    finally:
        # Only the two exact diagnostic snapshots are disposable; retain reports.
        for name in ("reference.sqlite3", "candidate.sqlite3"):
            (work / name).unlink(missing_ok=True)
        AtomicWriteJson(work / "result.json", report, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1
