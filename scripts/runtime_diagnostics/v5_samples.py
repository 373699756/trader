"""Isolated exact-value comparison of V5 wide columns and compact sequence blocks."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy

import argparse
import json
import time
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import groupby
from pathlib import Path

import numpy as np

from trader.infra.settings import LoadRuntimeSettings
from trader.infra.sqlite_codec import DecodeDate
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession
from trader.infra.storage_probe import StorageProbe
from trader.training.domain.training_dates import BuildTrainingDateSplit
from trader.training.infra.profile.v5.contracts import V5_TRAINING_PROFILE
from trader.training.infra.sample_cache import TrainingSampleCache
from trader.training.infra.sample_index import SQLiteTrainingSampleIndex, TrainingSample
from trader.training.infra.sample_sequences import SampleSequenceLayout


@dataclass
class _Measurement:
    name: str
    cache: TrainingSampleCache
    samples: SQLiteTrainingSampleIndex
    build_seconds: float = 0.0
    seal_seconds: float = 0.0
    read_seconds: float = 0.0


def ProbeV5Samples(
    work: Path,
    *,
    days: int = 256,
    codes: int = 32,
    wide_source: Path | None = None, sqlite_policy: SqliteIoPolicy,
) -> dict[str, object]:
    project = Path(__file__).resolve().parents[2]
    work = work.expanduser().resolve()
    if work.is_relative_to(project) or work.exists() or not 64 <= days <= 500 or not 2 <= codes <= 128:
        raise ValueError("v5_samples_requires_new_external_directory_and_bounded_workload")
    storage = StorageProbe().inspect(work)
    work.mkdir(parents=True)
    dates = tuple(date(2020, 1, 1) + timedelta(days=index) for index in range(2000))
    source_started = time.perf_counter()
    source_samples = _read_wide_samples(wide_source, sqlite_policy=sqlite_policy) if wide_source else ()
    source_seconds = time.perf_counter() - source_started
    if wide_source:
        observed_dates = tuple(sorted({sample.trade_date for sample in source_samples}))
        dates = (
            tuple(observed_dates[0] - timedelta(days=i) for i in range(60, 0, -1))
            + observed_dates
            + tuple(observed_dates[-1] + timedelta(days=i + 1) for i in range(1940 - len(observed_dates)))
        )
    split = BuildTrainingDateSplit(dates, parent_manifest_hash="a" * 64)
    source_batches = (
        (tuple(group) for _, group in groupby(source_samples, key=lambda item: item.trade_date))
        if wide_source
        else _synthetic_batches(split.model_fit_dates[:days], codes)
    )
    industry = source_samples[0].industry if source_samples else "industry"
    row_count = 0
    measurements = []
    with ExitStack() as scope:
        for name, layout in (("wide", None), ("compact", SampleSequenceLayout(6, 60, 6))):
            cache = TrainingSampleCache(
                work / name, "synthetic-v5-samples", V5_TRAINING_PROFILE.heads[0].feature_manifest.names,
                split.content_hash, sequence_layout=layout, sqlite_policy=sqlite_policy
            )
            measurements.append(_Measurement(name, cache, scope.enter_context(cache.open())))
        for index, batch in enumerate(source_batches):
            row_count += len(batch)
            for measurement in measurements[:: 1 if index % 2 else -1]:
                started = time.perf_counter()
                measurement.samples.add_final(batch)
                measurement.build_seconds += time.perf_counter() - started
        for measurement in measurements:
            started = time.perf_counter()
            measurement.samples.prepare_for_model_fitting(split)
            measurement.build_seconds += time.perf_counter() - started
            started = time.perf_counter()
            measurement.cache.seal(measurement.samples)
            measurement.seal_seconds = time.perf_counter() - started
        equal = True
        for contract in V5_TRAINING_PROFILE.heads:
            iterators = [item.samples.iter_batches(industry, contract, "training") for item in measurements]
            while True:
                batches = []
                for measurement, iterator in zip(measurements, iterators, strict=True):
                    started = time.perf_counter()
                    batches.append(next(iterator, None))
                    measurement.read_seconds += time.perf_counter() - started
                if all(batch is None for batch in batches):
                    break
                left, right = batches
                if left is None or right is None:
                    equal = False
                    break
                equal &= (
                    np.array_equal(left.features, right.features)
                    and np.array_equal(left.labels, right.labels)
                    and left.codes == right.codes
                    and left.trade_dates == right.trade_dates
                )
        sizes = tuple(item.cache.path.stat().st_size for item in measurements)
        return {
            "schema_version": "v5-samples-v1",
            "status": "passed" if equal else "failed",
            "summary": {
                "synthetic": wide_source is None,
                "full_training": False,
                "rows": row_count,
                "source_read_seconds": source_seconds,
                "source_storage_kind": StorageProbe().inspect(wide_source).kind if wide_source else None,
                "equal_values_and_order": bool(equal),
                "storage_kind": storage.kind,
                "storage_source": storage.source,
                "sqlite_bytes_ratio": sizes[1] / sizes[0],
                "build_time_ratio": measurements[1].build_seconds / measurements[0].build_seconds,
                "read_time_ratio": measurements[1].read_seconds / measurements[0].read_seconds,
                "cases": [
                    {
                        "layout": item.name,
                        "sqlite_bytes": size,
                        "build_seconds": item.build_seconds,
                        "seal_seconds": item.seal_seconds,
                        "read_seconds": item.read_seconds,
                    }
                    for item, size in zip(measurements, sizes, strict=True)
                ],
                "limitations": "Storage-only comparison; benchmark partition; dates and values retained. "
                "OS cache uncontrolled; no model fitting or production writes.",
            },
        }


def Main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--wide-sample-source", type=Path)
    args = parser.parse_args()
    from trader.bootstrap import BuildSqlitePolicy
    sqlite_policy = BuildSqlitePolicy(LoadRuntimeSettings(args.config))
    report = ProbeV5Samples(args.work_root, wide_source=args.wide_sample_source, sqlite_policy=sqlite_policy)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


def _synthetic_batches(days: tuple[date, ...], codes: int) -> Iterator[tuple[TrainingSample, ...]]:
    channels = np.random.default_rng(254).normal(size=(codes, len(days) + 60, 6))
    for index, day in enumerate(days):
        yield tuple(
            TrainingSample(
                f"{code:06d}",
                day,
                "main",
                "industry",
                1e7,
                (*tuple(float(code + i + index / 100) for i in range(6)), *channels[code, index : index + 60].ravel()),
                (0.1 + index / 10000, 0.2, 0.3, 0.4, 0.5, 0.35),
            )
            for code in range(codes)
        )


def _read_wide_samples(path: Path, *, sqlite_policy: SqliteIoPolicy) -> tuple[TrainingSample, ...]:
    """Diagnostic-only import of a bounded wide-table prefix; never a product cache fallback."""
    deadline = time.monotonic() + 30.0
    result = []
    dates: set[date] = set()
    with SqliteSession(path, SqliteConnectionOptions(read_only=True, timeout_seconds=1.0), sqlite_policy=sqlite_policy) as connection:
        connection.observe_progress(lambda: int(time.monotonic() >= deadline), 1000)
        industry = connection.run("SELECT industry FROM samples ORDER BY trade_date,code LIMIT 1").require_one()[0]
        fields = ",".join(V5_TRAINING_PROFILE.heads[0].feature_manifest.names)
        rows = connection.run(
            f"SELECT trade_date,code,board,industry,avg_amount_20d,{fields},"
            "target_t1,target_t2,target_t3,target_t4,target_t5,target_d25_aggregate "
            "FROM samples INDEXED BY samples_industry_date WHERE industry=? ORDER BY trade_date,code LIMIT 4096",
            (industry,),
        )
        try:
            for row in rows:
                if time.monotonic() >= deadline:
                    raise TimeoutError("wide_sample_read_deadline")
                day = DecodeDate(row[0])
                dates.add(day)
                if len(dates) > 500:
                    break
                labels = tuple(None if value is None else float(value) for value in row[-6:])
                result.append(
                    TrainingSample(
                        str(row[1]),
                        day,
                        str(row[2]),
                        str(row[3]),
                        float(row[4]),
                        tuple(float(value) for value in row[5:-6]),
                        (labels[0], labels[1], labels[2], labels[3], labels[4], labels[5]),
                    )
                )
        finally:
            rows.close()
    if not result:
        raise ValueError("wide_sample_source_empty")
    return tuple(result)


if __name__ == "__main__":
    raise SystemExit(Main())
