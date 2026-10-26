"""SQLite repository for disposable profile-scoped multi-target samples."""

from __future__ import annotations

import math
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date
from itertools import groupby
from pathlib import Path
from typing import Literal, cast

import numpy as np

from trader.infra.sqlite_connection import (
    EnsureSqliteSchema,
    OpenSqliteConnection,
    SqliteConnectionOptions,
    SqliteSchema,
    SqliteTransaction,
)
from trader.training.domain.training_dates import TrainingDateSplit
from trader.training.infra.model_bundles.contracts import TrainedHeadContract
from trader.training.infra.sample_sequences import SampleSequenceLayout, SampleSequenceReader, SampleSequenceWriter

TrainingSplitName = Literal["training", "early_stopping", "calibration", "validation", "holdout"]
TRAINING_SAMPLE_CACHE_MIB = 32
TRAINING_SAMPLE_MMAP_BYTES = 0
_TARGET_COLUMNS = (
    "target_t1",
    "target_t2",
    "target_t3",
    "target_t4",
    "target_t5",
    "target_d25_aggregate",
)


@dataclass(frozen=True)
class TrainingSample:
    code: str
    trade_date: date
    board: str
    industry: str
    average_amount_20d: float
    features: tuple[float, ...]
    targets: tuple[float | None, float | None, float | None, float | None, float | None, float | None]


@dataclass(frozen=True)
class TrainingSampleMatrix:
    features: np.ndarray
    labels: np.ndarray


@dataclass(frozen=True)
class TrainingSampleBatch(TrainingSampleMatrix):
    trade_dates: tuple[date, ...]
    codes: tuple[str, ...]
    industries: tuple[str, ...]


@dataclass(frozen=True)
class TrainingIndustryCounts:
    industry: str
    training: int
    early_stopping: int
    calibration: int
    validation: int
    calibration_dates: int = 0
    holdout: int = 0


@dataclass(frozen=True)
class TrainingIndustryData:
    training: TrainingSampleMatrix
    early_stopping: TrainingSampleMatrix
    calibration: TrainingSampleMatrix
    validation_count: int
    calibration_dates: tuple[date, ...] = ()


@dataclass(frozen=True)
class TargetMetric:
    target: str
    count: int
    mean: float | None
    standard_deviation: float | None


@dataclass(frozen=True)
class SampleDateGap:
    trade_date: date
    sample_rows: int
    candidates: int
    missing_future_returns: tuple[int, ...]


@dataclass(frozen=True)
class SampleCoverage:
    target: str
    partition: TrainingSplitName
    expected_dates: int
    effective_dates: int
    sample_rows: int
    target_rows: int
    missing: tuple[SampleDateGap, ...]


class SQLiteTrainingSampleIndex:
    """Own one temporary SQLite database shared by both sequential fits."""

    def __init__(
        self,
        path: Path,
        feature_count: int,
        *,
        prepared_split_hash: str | None = None,
        sequence_layout: SampleSequenceLayout | None = None,
    ) -> None:
        if feature_count < 1:
            raise ValueError("training sample feature count is invalid")
        self._path = path
        self._feature_count = feature_count
        self._sequence_layout = sequence_layout
        if (
            sequence_layout
            and feature_count != sequence_layout.auxiliary + sequence_layout.steps * sequence_layout.channels
        ):
            raise ValueError("training sequence feature count is invalid")
        self._column_features = sequence_layout.auxiliary if sequence_layout else feature_count
        self._prepared_split_hash = prepared_split_hash
        self._counts_by_target: dict[str, tuple[TrainingIndustryCounts, ...]] = {}
        self._connection = OpenSqliteConnection(
            path,
            SqliteConnectionOptions(
                read_only=prepared_split_hash is not None,
                journal_mode="OFF" if prepared_split_hash is None else None,
                durability="rebuildable" if prepared_split_hash is None else "durable",
                cache_kib=TRAINING_SAMPLE_CACHE_MIB * 1024,
                mmap_bytes=TRAINING_SAMPLE_MMAP_BYTES,
                temp_storage="FILE",
            ),
        )
        self._transaction = SqliteTransaction(self._connection, immediate=True)
        self._sequence_writer = SampleSequenceWriter(self._connection, sequence_layout) if sequence_layout else None
        try:
            self._initialize()
        except BaseException:
            self._connection.close()
            raise

    def _initialize(self) -> None:
        if self._prepared_split_hash is not None:
            return
        feature_columns = ", ".join(f"f{position} REAL NOT NULL" for position in range(self._column_features))
        if self._sequence_layout:
            feature_columns += ", sequence_start INTEGER NOT NULL CHECK(sequence_start>=0)"
        schema_sql = f"""
            CREATE TABLE samples (
                trade_date TEXT NOT NULL,
                code TEXT NOT NULL,
                board TEXT NOT NULL,
                industry TEXT NOT NULL,
                average_amount_20d REAL NOT NULL,
                {feature_columns},
                target_t1 REAL,
                target_t2 REAL,
                target_t3 REAL,
                target_t4 REAL,
                target_t5 REAL,
                target_d25_aggregate REAL,
                PRIMARY KEY (trade_date, code)
            ) WITHOUT ROWID;
            CREATE TABLE sample_split_dates (
                trade_date TEXT PRIMARY KEY,
                split_name TEXT NOT NULL CHECK (
                    split_name IN ('training', 'early_stopping', 'calibration', 'validation', 'holdout')
                )
            ) WITHOUT ROWID;
            CREATE TABLE sample_statistics (
                trade_date TEXT NOT NULL, industry TEXT NOT NULL, target TEXT NOT NULL,
                rows INTEGER NOT NULL, total REAL NOT NULL, squares REAL NOT NULL,
                PRIMARY KEY (trade_date, industry, target)
            ) WITHOUT ROWID;
            CREATE TABLE sample_day_diagnostics (
                trade_date TEXT PRIMARY KEY, candidates INTEGER NOT NULL,
                missing_t1 INTEGER NOT NULL, missing_t2 INTEGER NOT NULL,
                missing_t3 INTEGER NOT NULL, missing_t4 INTEGER NOT NULL, missing_t5 INTEGER NOT NULL
            ) WITHOUT ROWID;
            """
        if self._sequence_layout:
            schema_sql += """
                CREATE TABLE sample_sequence_blocks (
                    code TEXT NOT NULL, block INTEGER NOT NULL, payload BLOB NOT NULL,
                    PRIMARY KEY(code,block)
                );
                """
        schema = SqliteSchema(
            None, tuple(statement.strip() for statement in schema_sql.split(";") if statement.strip())
        )
        EnsureSqliteSchema(self._connection, schema, allow_create=True)
        self._transaction.begin()

    def close(self) -> None:
        try:
            self._transaction.rollback()
        finally:
            self._connection.close()

    def __enter__(self) -> SQLiteTrainingSampleIndex:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def add_final(self, samples: Iterable[TrainingSample]) -> None:
        self._counts_by_target.clear()
        values = tuple(samples)
        column_width = self._column_features + int(self._sequence_layout is not None)
        placeholders = ", ".join("?" for _ in range(5 + column_width + len(_TARGET_COLUMNS)))
        self._connection.run_many(
            f"INSERT INTO samples VALUES ({placeholders})",  # noqa: S608
            (self._encode(sample) for sample in values),
        )
        for (day, industry), group in groupby(
            sorted(values, key=lambda item: (item.trade_date, item.industry)),
            key=lambda item: (item.trade_date, item.industry),
        ):
            population = tuple(group)
            statistics = [(day.isoformat(), industry, "rows", len(population), 0.0, 0.0)]
            for position, target in enumerate(_TARGET_COLUMNS):
                labels = tuple(item.targets[position] for item in population if item.targets[position] is not None)
                statistics.append(
                    (
                        day.isoformat(),
                        industry,
                        target,
                        len(labels),
                        math.fsum(cast(float, value) for value in labels),
                        math.fsum(cast(float, value) ** 2 for value in labels),
                    )
                )
            self._connection.run_many(
                "INSERT INTO sample_statistics VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(trade_date,industry,target) DO UPDATE SET "
                "rows=rows+excluded.rows,total=total+excluded.total,squares=squares+excluded.squares",
                statistics,
            )

    def record_day(self, day: date, candidates: int, missing: tuple[int, ...]) -> None:
        if len(missing) != 5:
            raise ValueError("sample diagnostics require five horizons")
        self._connection.run(
            "INSERT INTO sample_day_diagnostics VALUES (?, ?, ?, ?, ?, ?, ?)", (day.isoformat(), candidates, *missing)
        )

    def prepare_for_model_fitting(self, split: TrainingDateSplit) -> None:
        if self._prepared_split_hash is not None:
            raise RuntimeError("training sample split is already prepared")
        self._counts_by_target.clear()
        split_dates = (
            *((day.isoformat(), "training") for day in split.model_fit_dates),
            *((day.isoformat(), "early_stopping") for day in split.early_stopping_dates),
            *((day.isoformat(), "calibration") for day in split.calibration_dates),
            *((day.isoformat(), "validation") for day in split.confirmation_dates),
            *((day.isoformat(), "holdout") for day in split.daily_proxy_holdout_dates),
        )
        self._connection.run_many("INSERT INTO sample_split_dates VALUES (?, ?)", split_dates)
        if self._sequence_writer is not None:
            self._sequence_writer.finish()
        self._connection.run("CREATE INDEX samples_industry_date ON samples(industry, trade_date, code)")
        self._transaction.commit()
        self._prepared_split_hash = split.content_hash

    def require_split(self, split: TrainingDateSplit) -> None:
        self.require_split_hash(split.content_hash)

    def require_split_hash(self, split_hash: str) -> None:
        if self._prepared_split_hash != split_hash:
            raise RuntimeError("training sample split identity is inconsistent")

    def industry_counts(self, contract: TrainedHeadContract) -> tuple[TrainingIndustryCounts, ...]:
        target = _require_target_column(contract.target_column)
        if not self._counts_by_target:
            self._prepare_industry_counts()
        return self._counts_by_target[target]

    def iter_batches(
        self,
        industry: str,
        contract: TrainedHeadContract,
        split_name: TrainingSplitName,
        *,
        batch_size: int = 256,
    ) -> Iterator[TrainingSampleBatch]:
        """Stream one industry's ordered partition without allocating its full matrix."""
        yield from self._iter_batches(contract, split_name, industry=industry, batch_size=batch_size)

    def iter_validation_batches(
        self, contract: TrainedHeadContract, *, batch_size: int = 256
    ) -> Iterator[TrainingSampleBatch]:
        """Read only the diagnostic confirmation proxy, in complete same-day population order."""
        yield from self._iter_batches(contract, "validation", industry=None, batch_size=batch_size)

    def _iter_batches(
        self,
        contract: TrainedHeadContract,
        split_name: TrainingSplitName,
        *,
        industry: str | None,
        batch_size: int,
    ) -> Iterator[TrainingSampleBatch]:
        if not 1 <= batch_size <= 256:
            raise ValueError("training batch size must be within 1..256")
        if split_name == "holdout":
            raise ValueError("training_holdout_is_sealed")
        target = _require_target_column(contract.target_column)
        if self._sequence_layout is not None:
            columns = ",".join(f"s.f{i}" for i in range(self._column_features)) + ",s.sequence_start"
        else:
            columns = ",".join(f"s.f{i}" for i in contract.feature_positions)
        selected = "AND s.industry=? " if industry is not None else ""
        rows = self._connection.run(
            f"SELECT s.trade_date,s.code,s.industry,{columns},s.{target} FROM samples s "
            "JOIN sample_split_dates d ON d.trade_date=s.trade_date "
            f"WHERE d.split_name=? {selected}AND s.{target} IS NOT NULL ORDER BY s.trade_date,s.code",
            (split_name, industry) if industry is not None else (split_name,),
        )
        decoder = SampleSequenceReader(self._connection, self._sequence_layout) if self._sequence_layout else None
        try:
            while batch := rows.fetchmany(batch_size):
                codes = tuple(str(row[1]) for row in batch)
                if decoder is None:
                    matrix = np.asarray([row[3:] for row in batch], dtype=np.float64)
                    features, labels = matrix[:, :-1], matrix[:, -1]
                else:
                    auxiliary = np.asarray([row[3 : 3 + self._column_features] for row in batch], dtype=np.float64)
                    sequence = decoder.read(tuple((str(row[1]), int(row[-2])) for row in batch))
                    features = np.concatenate((auxiliary, sequence), axis=1)[:, contract.feature_positions]
                    labels = np.asarray([row[-1] for row in batch], dtype=np.float64)
                yield TrainingSampleBatch(
                    features,
                    labels,
                    tuple(date.fromisoformat(str(row[0])) for row in batch),
                    codes,
                    tuple(str(row[2]) for row in batch),
                )
        finally:
            rows.close()

    def _prepare_industry_counts(self) -> None:
        aggregates = ",".join(
            f"SUM(CASE WHEN target='{target}' THEN rows ELSE 0 END),"
            f"COUNT(DISTINCT CASE WHEN target='{target}' AND rows>0 THEN samples.trade_date END)"
            for target in _TARGET_COLUMNS
        )
        rows = self._connection.run(
            f"SELECT samples.industry, splits.split_name, {aggregates} FROM sample_statistics samples "  # noqa: S608
            "JOIN sample_split_dates AS splits ON splits.trade_date=samples.trade_date "
            "GROUP BY samples.industry, splits.split_name ORDER BY samples.industry, splits.split_name"
        )
        grouped: dict[str, dict[str, dict[TrainingSplitName, tuple[int, int]]]] = {
            target: {} for target in _TARGET_COLUMNS
        }
        for industry, split_name, *counts in rows:
            for index, target in enumerate(_TARGET_COLUMNS):
                count, dates = counts[index * 2 : index * 2 + 2]
                if count:
                    grouped[target].setdefault(str(industry), {})[cast(TrainingSplitName, split_name)] = (
                        int(count),
                        int(dates),
                    )
        for target, industries in grouped.items():
            self._counts_by_target[target] = tuple(
                TrainingIndustryCounts(
                    industry,
                    values.get("training", (0, 0))[0],
                    values.get("early_stopping", (0, 0))[0],
                    values.get("calibration", (0, 0))[0],
                    values.get("validation", (0, 0))[0],
                    values.get("calibration", (0, 0))[1],
                    values.get("holdout", (0, 0))[0],
                )
                for industry, values in industries.items()
            )

    def industry_data(
        self,
        counts: TrainingIndustryCounts,
        contract: TrainedHeadContract,
    ) -> TrainingIndustryData:
        width = len(contract.feature_positions)
        matrices = {
            "training": _empty_matrix(counts.training, width),
            "early_stopping": _empty_matrix(counts.early_stopping, width),
            "calibration": _empty_matrix(counts.calibration, width),
        }
        positions: dict[TrainingSplitName, int] = {
            "training": 0,
            "early_stopping": 0,
            "calibration": 0,
        }
        calibration_dates: list[date] = []
        if self._sequence_layout is not None:
            for partition, matrix in matrices.items():
                position = 0
                for batch in self.iter_batches(counts.industry, contract, cast(TrainingSplitName, partition)):
                    end = position + len(batch.labels)
                    matrix.features[position:end] = batch.features
                    matrix.labels[position:end] = batch.labels
                    position = end
                    if partition == "calibration":
                        calibration_dates.extend(batch.trade_dates)
                if position != len(matrix.labels):
                    raise RuntimeError("training sample split count changed during its read")
            return TrainingIndustryData(
                matrices["training"],
                matrices["early_stopping"],
                matrices["calibration"],
                counts.validation,
                tuple(calibration_dates),
            )
        feature_sql = ", ".join(f"samples.f{position}" for position in contract.feature_positions)
        target = _require_target_column(contract.target_column)
        cursor = self._connection.run(
            f"SELECT splits.split_name, samples.trade_date, {feature_sql}, samples.{target} FROM samples "  # noqa: S608
            "JOIN sample_split_dates AS splits ON splits.trade_date=samples.trade_date "
            f"WHERE samples.industry=? AND samples.{target} IS NOT NULL "  # noqa: S608
            "AND splits.split_name IN ('training', 'early_stopping', 'calibration') "
            "ORDER BY samples.trade_date, samples.code",
            (counts.industry,),
        )
        while rows := cursor.fetchmany(4_096):
            for name, group in groupby(rows, key=lambda row: row[0]):
                split_name = cast(TrainingSplitName, name)
                split_rows = tuple(group)
                position = positions[split_name]
                end = position + len(split_rows)
                matrix = matrices[split_name]
                if end > len(matrix.labels):
                    raise RuntimeError("training sample split count changed during its read")
                values = np.asarray([row[2:] for row in split_rows], dtype=np.float64)
                matrix.features[position:end] = values[:, :-1]
                matrix.labels[position:end] = values[:, -1]
                positions[split_name] = end
                if split_name == "calibration":
                    calibration_dates.extend(date.fromisoformat(str(row[1])) for row in split_rows)
        expected = {
            "training": counts.training,
            "early_stopping": counts.early_stopping,
            "calibration": counts.calibration,
        }
        if positions != expected:
            raise RuntimeError("training sample split count changed during its read")
        return TrainingIndustryData(
            matrices["training"],
            matrices["early_stopping"],
            matrices["calibration"],
            counts.validation,
            tuple(calibration_dates),
        )

    def split_count(self, split_name: TrainingSplitName, contract: TrainedHeadContract) -> int:
        target = _require_target_column(contract.target_column)
        row = self._connection.run(
            "SELECT COALESCE(SUM(samples.rows),0) FROM sample_statistics samples JOIN sample_split_dates AS splits "
            "ON splits.trade_date=samples.trade_date WHERE splits.split_name=? AND samples.target=?",
            (split_name, target),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def split_distinct_date_count(self, split_name: TrainingSplitName, contract: TrainedHeadContract) -> int:
        target = _require_target_column(contract.target_column)
        row = self._connection.run(
            "SELECT COUNT(DISTINCT samples.trade_date) FROM sample_statistics samples "
            "JOIN sample_split_dates AS splits "
            "ON splits.trade_date=samples.trade_date WHERE splits.split_name=? AND samples.target=? AND samples.rows>0",
            (split_name, target),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def validation_target_metrics(self) -> tuple[TargetMetric, ...]:
        metrics: list[TargetMetric] = []
        rows = self._connection.run(
            "SELECT target,COALESCE(SUM(rows),0),SUM(total),SUM(squares) FROM sample_statistics s "
            "JOIN sample_split_dates d ON d.trade_date=s.trade_date WHERE d.split_name='validation' GROUP BY target"
        ).fetchall()
        for target in _TARGET_COLUMNS:
            row = next((tuple(value)[1:] for value in rows if value[0] == target), (0, 0.0, 0.0))
            count = int(row[0])
            mean = float(row[1]) / count if count else None
            second = float(row[2]) / count if count else None
            deviation = math.sqrt(max(0.0, second - mean * mean)) if mean is not None and second is not None else None
            metrics.append(TargetMetric(target, count, mean, deviation))
        return tuple(metrics)

    def count(self) -> int:
        row = self._connection.run("SELECT COALESCE(SUM(rows),0) FROM sample_statistics WHERE target='rows'").fetchone()
        return int(row[0]) if row is not None else 0

    def coverage(self, contract: TrainedHeadContract) -> tuple[SampleCoverage, ...]:
        result: list[SampleCoverage] = []
        for partition in ("training", "early_stopping", "calibration", "validation", "holdout"):
            rows = self._connection.run(
                "SELECT d.trade_date,COALESCE(SUM(CASE WHEN s.target='rows' THEN s.rows END),0),"
                "COALESCE(SUM(CASE WHEN s.target=? THEN s.rows END),0),"
                "COALESCE(g.candidates,0),g.missing_t1,g.missing_t2,g.missing_t3,g.missing_t4,g.missing_t5 "
                "FROM sample_split_dates d LEFT JOIN sample_statistics s ON s.trade_date=d.trade_date "
                "LEFT JOIN sample_day_diagnostics g ON g.trade_date=d.trade_date "
                "WHERE d.split_name=? GROUP BY d.trade_date ORDER BY d.trade_date",
                (_require_target_column(contract.target_column), partition),
            ).fetchall()
            gaps = tuple(
                SampleDateGap(
                    date.fromisoformat(str(row[0])),
                    int(row[1]),
                    int(row[3]),
                    tuple(int(value or 0) for value in row[4:]),
                )
                for row in rows
                if not row[2]
            )
            result.append(
                SampleCoverage(
                    contract.target_column,
                    partition,
                    len(rows),
                    len(rows) - len(gaps),
                    sum(int(row[1]) for row in rows),
                    sum(int(row[2]) for row in rows),
                    gaps,
                )
            )
        return tuple(result)

    @property
    def database_size_bytes(self) -> int:
        return self._path.stat().st_size

    @property
    def workspace(self) -> Path:
        return self._path.parent

    def _encode(self, sample: TrainingSample) -> tuple[object, ...]:
        if len(sample.features) != self._feature_count:
            raise ValueError("training sample feature width is invalid")
        encoded_features: tuple[float | int, ...] = sample.features
        if self._sequence_writer is not None:
            start = self._sequence_writer.append(sample.code, sample.features[self._column_features :])
            encoded_features = (*sample.features[: self._column_features], start)
        return (
            sample.trade_date.isoformat(),
            sample.code,
            sample.board,
            sample.industry,
            sample.average_amount_20d,
            *encoded_features,
            *sample.targets,
        )


def _require_target_column(value: str) -> str:
    if value not in _TARGET_COLUMNS:
        raise ValueError("training target column is invalid")
    return value


def _empty_matrix(row_count: int, width: int) -> TrainingSampleMatrix:
    return TrainingSampleMatrix(np.empty((row_count, width), dtype=np.float64), np.empty(row_count, dtype=np.float64))


__all__ = [
    "SQLiteTrainingSampleIndex",
    "TRAINING_SAMPLE_CACHE_MIB",
    "TRAINING_SAMPLE_MMAP_BYTES",
    "TargetMetric",
    "TrainingIndustryCounts",
    "TrainingIndustryData",
    "TrainingSample",
    "TrainingSampleMatrix",
    "TrainingSampleBatch",
    "TrainingSplitName",
]
