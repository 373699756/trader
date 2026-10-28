"""Bounded paired V5 lightweight compute and real-sample acceptance diagnostic."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import statistics
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import cast

import numpy as np

from trader.infra.atomic_files.json import AtomicReadJson, AtomicWriteJson
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession, SqliteTransaction
from trader.training.application.tomorrow_training import TrainingFitDetail
from trader.training.infra.model_bundles.bundle_codec import TrainedTcnModelSnapshot
from trader.training.infra.model_evaluation import PredictionMetricsAccumulator
from trader.training.infra.model_fitting import IndustryCoverageFailure
from trader.training.infra.profile.v5.contracts import V5_TRAINING_PROFILE
from trader.training.infra.profile.v5.settings import TcnArchitecture, TcnFittingParameters
from trader.training.infra.sample_index import SQLiteTrainingSampleIndex
from trader.training.infra.sample_sequences import SampleSequenceLayout
from trader.training.infra.sample_schema import SampleStorageId


def ProbeLiteKernels() -> dict[str, object]:
    import torch

    from trader.training.infra.profile.v5.network import ConfigureTcnRuntime, TcnNetwork

    ConfigureTcnRuntime(training=True, compute_threads=2)
    torch.manual_seed(0)
    values = torch.randn(256, 366, dtype=torch.float64)
    labels = torch.randn(256, dtype=torch.float64)
    measurements: dict[str, list[float]] = {"reference": [], "candidate": []}
    for repetition in range(5):
        for name in ("reference", "candidate") if repetition % 2 else ("candidate", "reference"):
            torch.manual_seed(repetition)
            architecture = TcnArchitecture(16, "float64") if name == "reference" else TcnArchitecture(8, "float32")
            model = TcnNetwork(6, 0.1, architecture)
            optimizer = torch.optim.AdamW(model.parameters(), lr=0.001, weight_decay=0.0001)
            for step in range(7):
                if step == 2:
                    started = time.perf_counter()
                optimizer.zero_grad(set_to_none=True)
                # Include the production batch dtype conversions in measured work.
                loss = torch.square(model(values.to(model.compute_dtype)) - labels.to(model.compute_dtype)).mean()
                loss.backward()
                optimizer.step()
            measurements[name].append((time.perf_counter() - started) / 5)
    ratio = statistics.median(b / a for a, b in zip(measurements["reference"], measurements["candidate"], strict=True))
    return {
        "paired_time_ratio": ratio,
        "passed": ratio <= 0.5,
        "seconds_per_batch": measurements,
        "threads": 2,
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


def _prepare(source: Path, work: Path, deadline: float) -> tuple[Path, Path]:
    manifest = AtomicReadJson(source / "complete.json")
    if not isinstance(manifest, dict) or manifest.get("storage") != SampleStorageId(V5_TRAINING_PROFILE.heads[0].feature_manifest.names, SampleSequenceLayout(6, 60, 6)):
        raise ValueError("V5 comparison requires a sealed compact sample cache")
    database = source / "samples.sqlite3"
    identity = database.stat()
    baseline, candidate = work / "reference.sqlite3", work / "candidate.sqlite3"
    # Exact external snapshots; source and its manifest are never modified.
    for target in (baseline, candidate):
        with database.open("rb") as reader, target.open("xb") as writer:
            while chunk := reader.read(1024**2):
                _check(deadline)
                writer.write(chunk)
    after = database.stat()
    if (identity.st_ino, identity.st_size, identity.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError("V5 sample source changed during snapshot")
    if _digest(baseline, deadline) != manifest.get("sha256") or _digest(candidate, deadline) != manifest.get("sha256"):
        raise ValueError("V5 sample snapshot digest mismatch")
    with SqliteSession(candidate, SqliteConnectionOptions(timeout_seconds=1)) as connection:
        days = connection.run(
            "SELECT trade_date FROM sample_split_dates WHERE split_name='training' ORDER BY trade_date"
        ).fetchall()
        if len(days) != 750:
            raise ValueError("V5 reference must contain the original 750 fitting dates")
        with SqliteTransaction(connection, immediate=True):
            connection.run(
                "DELETE FROM sample_split_dates WHERE split_name='training' AND trade_date<?", (days[-250][0],)
            )
            previous = connection.run("SELECT split_hash FROM sample_contract WHERE singleton=1").require_one()[0]
            diagnostic_hash = hashlib.sha256(previous + b"diagnostic_tail_250").digest()
            connection.run("UPDATE sample_contract SET split_hash=? WHERE singleton=1", (diagnostic_hash,))
    return baseline, candidate


def _quality(source: Path, work: Path, deadline: float) -> dict[str, object]:
    from trader.training.infra.profile.v5.fitting import FitTcnIndustry
    from trader.training.infra.profile.v5.network import TcnNetwork

    paths = _prepare(source, work, deadline)
    options = TcnFittingParameters(
        minimum_industry_training_rows=20000,
        learning_rate=0.001,
        weight_decay=0.0001,
        dropout=0.1,
        batch_size=256,
        epochs=30,
        early_stopping_rounds=5,
        seed=0,
        hidden_channels=8,
        compute_dtype="float32",
        early_stopping_relative_delta=0.001,
        compute_threads=2,
    )
    reference = replace(
        options,
        hidden_channels=16,
        compute_dtype="float64",
        epochs=100,
        early_stopping_rounds=10,
        early_stopping_relative_delta=0.0,
    )
    summaries: list[dict[str, object]] = []
    last_report = 0.0

    def progress(detail: TrainingFitDetail) -> None:
        nonlocal last_report
        _check(deadline)
        if time.monotonic() - last_report >= 10:
            AtomicWriteJson(work / "progress.json", asdict(detail))
            last_report = time.monotonic()

    split_hashes = []
    for path in paths:
        with SqliteSession(path, SqliteConnectionOptions(read_only=True)) as connection:
            split_hashes.append(connection.run("SELECT split_hash FROM sample_contract WHERE singleton=1").require_one()[0].hex())
    feature_names = V5_TRAINING_PROFILE.heads[0].feature_manifest.names
    with (
        SQLiteTrainingSampleIndex(
            paths[0], feature_names, prepared_split_hash=split_hashes[0], sequence_layout=SampleSequenceLayout(6, 60, 6)
        ) as old,
        SQLiteTrainingSampleIndex(
            paths[1], feature_names, prepared_split_hash=split_hashes[1], sequence_layout=SampleSequenceLayout(6, 60, 6)
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
            raise ValueError("V5 needs three eligible industries in the 250-day window")
        selected = (eligible[0], eligible[len(eligible) // 2], eligible[-1])
        coverage = {
            "eligible_industries_250": len(eligible),
            "candidate_industries": len(by_head[0]),
            "selected": selected,
            "excluded": sorted(set(by_head[0]) - set(eligible)),
        }
        AtomicWriteJson(work / "coverage.json", coverage)
        for industry in selected:
            for head in V5_TRAINING_PROFILE.heads:
                pair: dict[str, dict[str, object]] = {}
                for name, samples, parameters in (("candidate", new, options), ("reference", old, reference)):
                    _check(deadline)
                    counts = next(count for count in samples.industry_counts(head) if count.industry == industry)
                    AtomicWriteJson(
                        work / "current.json", {"case": name, "head": head.strategy.value, "industry": industry}
                    )
                    started = time.monotonic()
                    fitted = FitTcnIndustry(
                        samples, counts, head, parameters, lambda: _check(deadline), report=progress
                    )
                    model = fitted.model
                    if not isinstance(model, TrainedTcnModelSnapshot):
                        raise ValueError("V5 comparison model type mismatch")
                    network = TcnNetwork(len(model.transformer_means) - 360, model.dropout, model.architecture)
                    network.load_tensors(model.weights)
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
                    pair[name] = {
                        **asdict(metrics.finish()),
                        "seconds": time.monotonic() - started,
                        "training_rows": counts.training,
                        "parameters": asdict(parameters),
                    }
                    AtomicWriteJson(work / "latest_pair.json", pair)
                passed = QualityPassed(pair["reference"], pair["candidate"])
                summaries.append({"industry": industry, "head": head.strategy.value, "passed": passed, "cases": pair})
                AtomicWriteJson(work / "comparisons.json", {"comparisons": summaries})
                if not passed:
                    return {
                        "passed": False,
                        "reason": "quality_gate_failed",
                        "coverage": coverage,
                        "comparisons": summaries,
                        "stopped_after_failure": True,
                    }
    return {"passed": True, "coverage": coverage, "comparisons": summaries}


def Main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--sample-root", type=Path)
    args = parser.parse_args()
    work = args.work_root.resolve()
    project = Path(__file__).resolve().parents[2]
    if work.exists() or work.is_relative_to(project):
        raise ValueError("V5 lite diagnostic requires a new external work directory")
    work.mkdir(parents=True)
    report: dict[str, object] = {
        "schema_version": "v5-lite-diagnostic",
        "status": "degraded",
        "summary": {"full_training": False, "production_authority": False},
    }
    summary = report["summary"]
    assert isinstance(summary, dict)
    try:
        summary["compute"] = compute = ProbeLiteKernels()
        AtomicWriteJson(work / "result.json", report)
        if compute["passed"] and args.sample_root is not None:
            summary["quality"] = quality = _quality(args.sample_root.resolve(), work, time.monotonic() + 3 * 3600)
            report["status"] = "passed" if quality["passed"] else "degraded"
        elif not compute["passed"]:
            summary["reason"] = "compute_gate_failed"
        else:
            summary["reason"] = "quality_not_requested"
    except (OSError, ValueError, RuntimeError, sqlite3.Error, ImportError) as exc:
        summary["failure"] = f"{type(exc).__name__}: {exc}"
    finally:
        # Only the two exact diagnostic snapshots are disposable; retain reports.
        for name in ("reference.sqlite3", "candidate.sqlite3"):
            (work / name).unlink(missing_ok=True)
        AtomicWriteJson(work / "result.json", report)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(Main())
