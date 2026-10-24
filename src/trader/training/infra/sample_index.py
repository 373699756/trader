"""SQLite repository for disposable profile-scoped multi-target samples."""

from __future__ import annotations

import math
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date
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


class SQLiteTrainingSampleIndex:
    """Own one temporary SQLite database shared by both sequential fits."""

    def __init__(self, path: Path, feature_count: int, *, prepared_split_hash: str | None = None) -> None:
        if feature_count < 1:
            raise ValueError("training sample feature count is invalid")
        self._path = path
        self._feature_count = feature_count
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
        try:
            self._initialize()
        except BaseException:
            self._connection.close()
            raise

    def _initialize(self) -> None:
        if self._prepared_split_hash is not None:
            return
        feature_columns = ", ".join(f"f{position} REAL NOT NULL" for position in range(self._feature_count))
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
        placeholders = ", ".join("?" for _ in range(5 + self._feature_count + len(_TARGET_COLUMNS)))
        self._connection.run_many(
            f"INSERT INTO samples VALUES ({placeholders})",  # noqa: S608
            (self._encode(sample) for sample in samples),
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
        columns = ",".join(f"s.f{i}" for i in contract.feature_positions)
        selected = "AND s.industry=? " if industry is not None else ""
        rows = self._connection.run(
            f"SELECT s.trade_date,s.code,s.industry,{columns},s.{target} FROM samples s "
            "JOIN sample_split_dates d ON d.trade_date=s.trade_date "
            f"WHERE d.split_name=? {selected}AND s.{target} IS NOT NULL ORDER BY s.trade_date,s.code",
            (split_name, industry) if industry is not None else (split_name,),
        )
        batch: list[tuple[float, ...]] = []
        days: list[date] = []
        codes: list[str] = []
        industries: list[str] = []
        for row in rows:
            days.append(date.fromisoformat(str(row[0])))
            codes.append(str(row[1]))
            industries.append(str(row[2]))
            batch.append(tuple(float(v) for v in row[3:]))
            if len(batch) == batch_size:
                matrix = np.asarray(batch, dtype=np.float64)
                yield TrainingSampleBatch(matrix[:, :-1], matrix[:, -1], tuple(days), tuple(codes), tuple(industries))
                batch.clear()
                days.clear()
                codes.clear()
                industries.clear()
        if batch:
            matrix = np.asarray(batch, dtype=np.float64)
            yield TrainingSampleBatch(matrix[:, :-1], matrix[:, -1], tuple(days), tuple(codes), tuple(industries))

    def _prepare_industry_counts(self) -> None:
        aggregates = ",".join(
            f"COUNT(samples.{target}),COUNT(DISTINCT CASE WHEN samples.{target} IS NOT NULL THEN samples.trade_date END)"
            for target in _TARGET_COLUMNS
        )
        rows = self._connection.run(
            f"SELECT samples.industry, splits.split_name, {aggregates} FROM samples "  # noqa: S608
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
            for row in rows:
                split_name = cast(TrainingSplitName, str(row[0]))
                position = positions[split_name]
                positions[split_name] += 1
                matrix = matrices[split_name]
                matrix.features[position] = tuple(float(cast(float, value)) for value in row[2:-1])
                matrix.labels[position] = float(cast(float, row[-1]))
                if split_name == "calibration":
                    calibration_dates.append(date.fromisoformat(str(row[1])))
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
            "SELECT COUNT(*) FROM samples JOIN sample_split_dates AS splits "
            f"ON splits.trade_date=samples.trade_date WHERE splits.split_name=? AND samples.{target} IS NOT NULL",  # noqa: S608
            (split_name,),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def split_distinct_date_count(self, split_name: TrainingSplitName, contract: TrainedHeadContract) -> int:
        target = _require_target_column(contract.target_column)
        row = self._connection.run(
            "SELECT COUNT(DISTINCT samples.trade_date) FROM samples JOIN sample_split_dates AS splits "
            f"ON splits.trade_date=samples.trade_date WHERE splits.split_name=? AND samples.{target} IS NOT NULL",  # noqa: S608
            (split_name,),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def validation_target_metrics(self) -> tuple[TargetMetric, ...]:
        aggregates = ", ".join(
            f"COUNT(samples.{target}), AVG(samples.{target}), AVG(samples.{target} * samples.{target})"
            for target in _TARGET_COLUMNS
        )
        row = self._connection.run(
            f"SELECT {aggregates} FROM samples "  # noqa: S608
            "JOIN sample_split_dates AS splits ON splits.trade_date=samples.trade_date "
            "WHERE splits.split_name='validation'"
        ).fetchone()
        metrics: list[TargetMetric] = []
        for position, target in enumerate(_TARGET_COLUMNS):
            offset = position * 3
            count = int(row[offset]) if row is not None else 0
            mean = float(row[offset + 1]) if row is not None and row[offset + 1] is not None else None
            second = float(row[offset + 2]) if row is not None and row[offset + 2] is not None else None
            deviation = math.sqrt(max(0.0, second - mean * mean)) if mean is not None and second is not None else None
            metrics.append(TargetMetric(target, count, mean, deviation))
        return tuple(metrics)

    def count(self) -> int:
        row = self._connection.run("SELECT COUNT(*) FROM samples").fetchone()
        return int(row[0]) if row is not None else 0

    @property
    def database_size_bytes(self) -> int:
        return self._path.stat().st_size

    def _encode(self, sample: TrainingSample) -> tuple[object, ...]:
        if len(sample.features) != self._feature_count:
            raise ValueError("training sample feature width is invalid")
        return (
            sample.trade_date.isoformat(),
            sample.code,
            sample.board,
            sample.industry,
            sample.average_amount_20d,
            *sample.features,
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
