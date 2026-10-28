"""One isolated checkout's sample write/read measurements; never trains or publishes models."""

from __future__ import annotations

import argparse
import hashlib
import json
import resource
import platform
import sqlite3
import time
from contextlib import ExitStack, nullcontext
from datetime import date, timedelta
from pathlib import Path
from typing import cast


def MeasureSamples(checkout: Path, root: Path, size: int, sessions: int) -> dict[str, object]:
    # The two explicitly selected offline checkouts have different physical APIs.
    # No compatibility selection is installed in the application.
    import numpy as np
    import trader.training.infra.sample_index as sample_module

    if not Path(sample_module.__file__).resolve().is_relative_to(checkout.resolve()):
        raise ValueError("training_sample_worker_wrong_checkout")

    from trader.recommendation.domain.publication.models import Strategy
    from trader.training.domain.training_dates import BuildTrainingDateSplit
    from trader.training.infra.profile.v1.samples import LearnedRankSampleIndex, _Candidate, _PendingDay
    from trader.training.infra.profile.v2.contracts import V2_TRAINING_PROFILE
    from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE
    from trader.training.infra.profile.v5.contracts import V5_TRAINING_PROFILE
    from trader.training.infra.sample_index import SQLiteTrainingSampleIndex, TrainingIndustryData, TrainingSample
    from trader.training.infra.sample_sequences import SampleSequenceLayout
    from trader.training.infra.implementation_identity import TrainingImplementationIdentity
    from trader.infra.sqlite_connection import SqliteConnection

    counters = {"connections": 0, "closed_connections": 0, "sql_statements": 0, "transactions": 0}
    original_init, original_close = SqliteConnection.__init__, SqliteConnection.close

    def trace(sql: str) -> None:
        counters["sql_statements"] += 1
        counters["transactions"] += int(sql.lstrip().upper().startswith("BEGIN"))

    def open_connection(self: SqliteConnection, driver: sqlite3.Connection) -> None:
        original_init(self, driver)
        counters["connections"] += 1
        self.observe_statements(trace)

    def close_connection(self: SqliteConnection) -> None:
        original_close(self)
        counters["closed_connections"] += 1

    # Bounded diagnostic process only; application owners and APIs are unchanged.
    SqliteConnection.__init__ = open_connection  # type: ignore[method-assign]
    SqliteConnection.close = close_connection  # type: ignore[method-assign]

    named = (checkout / "src/trader/training/infra/sample_schema.py").is_file()
    dates = tuple(date(2020, 1, 1) + timedelta(days=i) for i in range(2000))
    split = BuildTrainingDateSplit(dates, parent_manifest_hash="a" * 64)
    selected = (
        *split.model_fit_dates[:sessions], *split.early_stopping_dates[:32],
        *split.calibration_dates[:32], *split.confirmation_dates[:32],
    )
    root.mkdir(parents=True, exist_ok=False)
    cases: dict[str, object] = {}
    for profile in (V2_TRAINING_PROFILE, V3_TRAINING_PROFILE, V5_TRAINING_PROFILE):
        names = profile.heads[0].feature_manifest.names
        layout = SampleSequenceLayout(6, 60, 6) if profile.profile_id == "v5" else None
        channels = np.random.default_rng(289).normal(size=(size, len(selected) + 60, 6)) if layout else None
        path = root / f"{profile.profile_id}.sqlite3"
        started = time.perf_counter()
        with SQLiteTrainingSampleIndex(path, names if named else len(names), sequence_layout=layout) as samples:  # type: ignore[arg-type]  # Explicit historical checkout API.
            for index, day in enumerate(selected):
                rows = []
                for stock in range(size):
                    features = tuple((stock * 7 + index * 3 + i) / 1000 for i in range(6 if layout else len(names)))
                    if channels is not None:
                        features += tuple(float(value) for value in channels[stock, index:index + 60].ravel())
                    targets = tuple(None if (stock + index + i) % 37 == 0 else (stock - i) / 10000 for i in range(6))
                    rows.append(TrainingSample(f"{600000 + stock}", day, "main", "fixture", 1e7, features, (targets[0], targets[1], targets[2], targets[3], targets[4], targets[5])))
                samples.add_final(rows)
            samples.prepare_for_model_fitting(split)
        write_seconds = time.perf_counter() - started
        digest = hashlib.sha256()
        started = time.perf_counter()
        with SQLiteTrainingSampleIndex(
            path, names if named else len(names), sequence_layout=layout, prepared_split_hash=split.content_hash  # type: ignore[arg-type]
        ) as samples:
            for head in profile.heads:
                digest.update(head.model_id.encode())
                counts = samples.industry_counts(head)[0]
                data = samples.industry_data(counts, head)
                with data if named else nullcontext(cast(TrainingIndustryData, data)) as matrix:
                    for part in (matrix.training, matrix.early_stopping, matrix.calibration):
                        digest.update(part.features.tobytes())
                        digest.update(part.labels.tobytes())
                    digest.update(str(matrix.calibration_dates).encode())
                for batch in samples.iter_batches("fixture", head, "validation"):
                    digest.update(batch.features.tobytes())
                    digest.update(batch.labels.tobytes())
                    digest.update(str(batch.trade_dates).encode())
        cases[profile.profile_id] = {
            "write_seconds": write_seconds, "read_seconds": time.perf_counter() - started,
            "numeric_hash": digest.hexdigest(), "database_bytes": path.stat().st_size,
        }
    path = root / "v1.sqlite3"
    started = time.perf_counter()
    with ExitStack() as resources:
        rank_index = LearnedRankSampleIndex(path)
        resources.callback(rank_index.close)
        for number, day in enumerate(selected):
            pending = _PendingDay(day, number, {
                f"{600000 + stock}": _Candidate(
                    f"{600000 + stock}", "fixture",
                    tuple(-0.0 if i == 0 else (number + stock + i) / 1000 for i in range(10)),
                    10.0, [None if (stock + number + i) % 37 == 0 else (stock - i) / 10000 for i in range(5)],
                ) for stock in range(size)
            })
            rank_index.add_day(pending)
        write_seconds = time.perf_counter() - started
        started = time.perf_counter()
        digest = hashlib.sha256()
        with ExitStack() as matrices:
            if named:
                from trader.training.infra.profile.v1.samples import LearnedRankMatrixRequest

                pair = rank_index.read_matrices(
                    tuple(LearnedRankMatrixRequest(selected, strategy) for strategy in (Strategy.TOMORROW, Strategy.D25)),
                    root / "matrices", matrices,
                )
            else:
                pair = tuple(
                    rank_index.matrix(selected, strategy, root / strategy.value, matrices)  # type: ignore[attr-defined]
                    for strategy in (Strategy.TOMORROW, Strategy.D25)
                )
            for rank_matrix in pair:
                digest.update(rank_matrix.features.tobytes())
                digest.update(rank_matrix.targets.tobytes())
                digest.update(str((rank_matrix.codes, rank_matrix.dates, rank_matrix.industries, rank_matrix.groups)).encode())
        cases["v1"] = {
            "write_seconds": write_seconds, "read_seconds": time.perf_counter() - started,
            "numeric_hash": digest.hexdigest(), "database_bytes": path.stat().st_size,
        }
    usage = resource.getrusage(resource.RUSAGE_SELF)
    if counters["connections"] != counters["closed_connections"]:
        raise RuntimeError("training_sample_connection_leak")
    return {
        "cases": cases, "peak_rss_bytes": usage.ru_maxrss * 1024,
        "io_read_blocks": usage.ru_inblock, "io_write_blocks": usage.ru_oublock,
        "minor_page_faults": usage.ru_minflt, "major_page_faults": usage.ru_majflt,
        "database": counters, "implementation_hash": TrainingImplementationIdentity(),
        "python_version": platform.python_version(), "numpy_version": np.__version__,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--size", type=int, required=True)
    parser.add_argument("--sessions", type=int, required=True)
    args = parser.parse_args()
    print(json.dumps(MeasureSamples(args.checkout, args.root, args.size, args.sessions)))
