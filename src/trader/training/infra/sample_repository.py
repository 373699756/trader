"""SQLite repository for disposable profile-scoped multi-target samples."""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal, cast

import numpy as np

from trader.download.domain.baostock_daily import BaoStockTrainingSplit
from trader.training.infra.artifacts.contracts import TrainedHeadContract

TrainingSplitName = Literal["training", "early_stopping", "calibration", "validation"]
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
class TrainingIndustryCounts:
    industry: str
    training: int
    early_stopping: int
    calibration: int
    validation: int


@dataclass(frozen=True)
class TrainingIndustryData:
    training: TrainingSampleMatrix
    early_stopping: TrainingSampleMatrix
    calibration: TrainingSampleMatrix
    validation_count: int


@dataclass(frozen=True)
class TargetMetric:
    target: str
    count: int
    mean: float | None
    standard_deviation: float | None


class SQLiteTrainingSampleRepository:
    """Own one temporary SQLite database shared by all three sequential fits."""

    def __init__(self, path: Path, feature_count: int) -> None:
        if feature_count < 1:
            raise ValueError("training sample feature count is invalid")
        self._path = path
        self._feature_count = feature_count
        self._prepared_split_hash: str | None = None
        self._connection = sqlite3.connect(path)
        self._connection.execute(f"PRAGMA cache_size=-{TRAINING_SAMPLE_CACHE_MIB * 1024}")  # noqa: S608
        self._connection.execute(f"PRAGMA mmap_size={TRAINING_SAMPLE_MMAP_BYTES}")  # noqa: S608
        feature_columns = ", ".join(f"f{position} REAL NOT NULL" for position in range(feature_count))
        self._connection.executescript(
            f"""
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            PRAGMA temp_store=FILE;
            CREATE TABLE samples (
                trade_date TEXT NOT NULL,
                code TEXT NOT NULL,
                board TEXT NOT NULL,
                industry TEXT NOT NULL,
                average_amount_20d REAL NOT NULL,
                {feature_columns},
                target_t1 REAL NOT NULL,
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
                    split_name IN ('training', 'early_stopping', 'calibration', 'validation')
                )
            ) WITHOUT ROWID;
            """
        )
        self._connection.execute("BEGIN IMMEDIATE")

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> SQLiteTrainingSampleRepository:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def add_final(self, samples: Iterable[TrainingSample]) -> None:
        placeholders = ", ".join("?" for _ in range(5 + self._feature_count + len(_TARGET_COLUMNS)))
        self._connection.executemany(
            f"INSERT INTO samples VALUES ({placeholders})",  # noqa: S608
            (self._encode(sample) for sample in samples),
        )

    def prepare_for_model_fitting(self, split: BaoStockTrainingSplit) -> None:
        if self._prepared_split_hash is not None:
            raise RuntimeError("training sample split is already prepared")
        split_dates = (
            *((day.isoformat(), "training") for day in split.model_fit_dates),
            *((day.isoformat(), "early_stopping") for day in split.early_stopping_dates),
            *((day.isoformat(), "calibration") for day in split.calibration_dates),
            *((day.isoformat(), "validation") for day in (*split.confirmation_dates, *split.daily_proxy_holdout_dates)),
        )
        self._connection.executemany("INSERT INTO sample_split_dates VALUES (?, ?)", split_dates)
        self._connection.execute("CREATE INDEX samples_industry_date ON samples(industry, trade_date, code)")
        self._connection.commit()
        self._prepared_split_hash = split.content_hash

    def require_split(self, split: BaoStockTrainingSplit) -> None:
        if self._prepared_split_hash != split.content_hash:
            raise RuntimeError("training sample split identity is inconsistent")

    def industry_counts(self, contract: TrainedHeadContract) -> tuple[TrainingIndustryCounts, ...]:
        target = _require_target_column(contract.target_column)
        rows = self._connection.execute(
            f"SELECT samples.industry, splits.split_name, COUNT(*) FROM samples "  # noqa: S608
            "JOIN sample_split_dates AS splits ON splits.trade_date=samples.trade_date "
            f"WHERE samples.{target} IS NOT NULL "  # noqa: S608
            "GROUP BY samples.industry, splits.split_name ORDER BY samples.industry, splits.split_name"
        )
        counts: dict[str, dict[TrainingSplitName, int]] = {}
        for industry, split_name, count in rows:
            name = cast(TrainingSplitName, str(split_name))
            counts.setdefault(str(industry), {})[name] = int(count)
        return tuple(
            TrainingIndustryCounts(
                industry,
                values.get("training", 0),
                values.get("early_stopping", 0),
                values.get("calibration", 0),
                values.get("validation", 0),
            )
            for industry, values in counts.items()
            if values.get("training", 0) > 0
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
            "validation": 0,
        }
        feature_sql = ", ".join(f"samples.f{position}" for position in contract.feature_positions)
        target = _require_target_column(contract.target_column)
        cursor = self._connection.execute(
            f"SELECT splits.split_name, {feature_sql}, samples.{target} FROM samples "  # noqa: S608
            "JOIN sample_split_dates AS splits ON splits.trade_date=samples.trade_date "
            f"WHERE samples.industry=? AND samples.{target} IS NOT NULL "  # noqa: S608
            "ORDER BY samples.trade_date, samples.code",
            (counts.industry,),
        )
        while rows := cursor.fetchmany(4_096):
            for row in rows:
                split_name = cast(TrainingSplitName, str(row[0]))
                position = positions[split_name]
                positions[split_name] += 1
                if split_name == "validation":
                    continue
                matrix = matrices[split_name]
                matrix.features[position] = tuple(float(cast(float, value)) for value in row[1:-1])
                matrix.labels[position] = float(cast(float, row[-1]))
        expected = {
            "training": counts.training,
            "early_stopping": counts.early_stopping,
            "calibration": counts.calibration,
            "validation": counts.validation,
        }
        if positions != expected:
            raise RuntimeError("training sample split count changed during its read")
        return TrainingIndustryData(
            matrices["training"], matrices["early_stopping"], matrices["calibration"], positions["validation"]
        )

    def split_count(self, split_name: TrainingSplitName, contract: TrainedHeadContract) -> int:
        target = _require_target_column(contract.target_column)
        row = self._connection.execute(
            "SELECT COUNT(*) FROM samples JOIN sample_split_dates AS splits "
            f"ON splits.trade_date=samples.trade_date WHERE splits.split_name=? AND samples.{target} IS NOT NULL",  # noqa: S608
            (split_name,),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def validation_target_metrics(self) -> tuple[TargetMetric, ...]:
        metrics: list[TargetMetric] = []
        for target in _TARGET_COLUMNS:
            row = self._connection.execute(
                f"SELECT COUNT(samples.{target}), AVG(samples.{target}), "  # noqa: S608
                f"AVG(samples.{target} * samples.{target}) FROM samples "  # noqa: S608
                "JOIN sample_split_dates AS splits ON splits.trade_date=samples.trade_date "
                f"WHERE splits.split_name='validation' AND samples.{target} IS NOT NULL"  # noqa: S608
            ).fetchone()
            count = int(row[0]) if row is not None else 0
            mean = float(row[1]) if row is not None and row[1] is not None else None
            second = float(row[2]) if row is not None and row[2] is not None else None
            deviation = math.sqrt(max(0.0, second - mean * mean)) if mean is not None and second is not None else None
            metrics.append(TargetMetric(target, count, mean, deviation))
        return tuple(metrics)

    def count(self) -> int:
        row = self._connection.execute("SELECT COUNT(*) FROM samples").fetchone()
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
    "SQLiteTrainingSampleRepository",
    "TRAINING_SAMPLE_CACHE_MIB",
    "TRAINING_SAMPLE_MMAP_BYTES",
    "TargetMetric",
    "TrainingIndustryCounts",
    "TrainingIndustryData",
    "TrainingSample",
    "TrainingSampleMatrix",
    "TrainingSplitName",
]
