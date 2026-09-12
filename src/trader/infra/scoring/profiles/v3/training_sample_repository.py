"""SQLite repository for disposable Tomorrow training samples."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal, cast

import numpy as np

from trader.domain.research.baostock_daily import BaoStockTrainingSplit

TomorrowTrainingSplitName = Literal["training", "early_stopping", "calibration", "validation"]


@dataclass(frozen=True)
class TomorrowTrainingSample:
    code: str
    trade_date: date
    board: str
    industry: str
    average_amount_20d: float
    features: tuple[float, ...]
    target: float


@dataclass(frozen=True)
class TomorrowTrainingSampleMatrix:
    features: np.ndarray
    labels: np.ndarray


@dataclass(frozen=True)
class TomorrowTrainingIndustryCounts:
    industry: str
    training: int
    early_stopping: int
    calibration: int
    validation: int


@dataclass(frozen=True)
class TomorrowTrainingIndustryData:
    training: TomorrowTrainingSampleMatrix
    early_stopping: TomorrowTrainingSampleMatrix
    calibration: TomorrowTrainingSampleMatrix
    validation_count: int


class SQLiteTomorrowTrainingSampleRepository:
    """Own one disposable SQLite database used only during a training run."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._prepared_split_hash: str | None = None
        self._connection = sqlite3.connect(path)
        self._connection.executescript(
            """
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            PRAGMA temp_store=FILE;
            PRAGMA cache_size=-32768;
            PRAGMA mmap_size=0;
            CREATE TABLE samples (
                code TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                board TEXT NOT NULL,
                industry TEXT NOT NULL,
                average_amount_20d REAL NOT NULL,
                f0 REAL NOT NULL, f1 REAL NOT NULL, f2 REAL NOT NULL,
                f3 REAL NOT NULL, f4 REAL NOT NULL, f5 REAL NOT NULL,
                target REAL NOT NULL,
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

    def __enter__(self) -> SQLiteTomorrowTrainingSampleRepository:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def add_final(self, samples: Iterable[TomorrowTrainingSample]) -> None:
        self._connection.executemany(
            "INSERT INTO samples VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_encode(sample) for sample in samples),
        )

    def prepare_for_model_fitting(self, split: BaoStockTrainingSplit) -> None:
        if self._prepared_split_hash is not None:
            raise RuntimeError("Tomorrow sample split is already prepared")
        split_dates = (
            *((day.isoformat(), "training") for day in split.model_fit_dates),
            *((day.isoformat(), "early_stopping") for day in split.early_stopping_dates),
            *((day.isoformat(), "calibration") for day in split.calibration_dates),
            *((day.isoformat(), "validation") for day in (*split.confirmation_dates, *split.daily_proxy_holdout_dates)),
        )
        self._connection.executemany("INSERT INTO sample_split_dates VALUES (?, ?)", split_dates)
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS samples_industry_date ON samples(industry, trade_date, code)"
        )
        self._connection.commit()
        self._prepared_split_hash = split.content_hash

    def require_split(self, split: BaoStockTrainingSplit) -> None:
        if self._prepared_split_hash != split.content_hash:
            raise RuntimeError("Tomorrow sample split identity is inconsistent")

    def industry_counts(self) -> tuple[TomorrowTrainingIndustryCounts, ...]:
        rows = self._connection.execute(
            "SELECT samples.industry, splits.split_name, COUNT(*) FROM samples "
            "JOIN sample_split_dates AS splits ON splits.trade_date=samples.trade_date "
            "GROUP BY samples.industry, splits.split_name ORDER BY samples.industry, splits.split_name"
        )
        counts: dict[str, dict[TomorrowTrainingSplitName, int]] = {}
        for industry, split_name, count in rows:
            name = cast(TomorrowTrainingSplitName, str(split_name))
            counts.setdefault(str(industry), {})[name] = int(count)
        return tuple(
            TomorrowTrainingIndustryCounts(
                industry,
                values.get("training", 0),
                values.get("early_stopping", 0),
                values.get("calibration", 0),
                values.get("validation", 0),
            )
            for industry, values in counts.items()
            if values.get("training", 0) > 0
        )

    def industry_data(self, counts: TomorrowTrainingIndustryCounts) -> TomorrowTrainingIndustryData:
        matrices = {
            "training": _empty_matrix(counts.training),
            "early_stopping": _empty_matrix(counts.early_stopping),
            "calibration": _empty_matrix(counts.calibration),
        }
        positions: dict[TomorrowTrainingSplitName, int] = {
            "training": 0,
            "early_stopping": 0,
            "calibration": 0,
            "validation": 0,
        }
        cursor = self._connection.execute(
            "SELECT splits.split_name, samples.f0, samples.f1, samples.f2, samples.f3, samples.f4, "
            "samples.f5, samples.target FROM samples JOIN sample_split_dates AS splits "
            "ON splits.trade_date=samples.trade_date WHERE samples.industry=? "
            "ORDER BY samples.trade_date, samples.code",
            (counts.industry,),
        )
        while rows := cursor.fetchmany(4_096):
            for row in rows:
                split_name = cast(TomorrowTrainingSplitName, str(row[0]))
                position = positions[split_name]
                positions[split_name] += 1
                if split_name == "validation":
                    continue
                matrix = matrices[split_name]
                matrix.features[position] = tuple(float(cast(float, value)) for value in row[1:7])
                matrix.labels[position] = float(cast(float, row[7]))
        expected = {
            "training": counts.training,
            "early_stopping": counts.early_stopping,
            "calibration": counts.calibration,
            "validation": counts.validation,
        }
        if positions != expected:
            raise RuntimeError("Tomorrow sample split count changed during its read")
        return TomorrowTrainingIndustryData(
            matrices["training"],
            matrices["early_stopping"],
            matrices["calibration"],
            positions["validation"],
        )

    def split_count(self, split_name: TomorrowTrainingSplitName) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) FROM samples JOIN sample_split_dates AS splits "
            "ON splits.trade_date=samples.trade_date WHERE splits.split_name=?",
            (split_name,),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def count(self, dates: frozenset[date] | None = None) -> int:
        if dates is None:
            row = self._connection.execute("SELECT COUNT(*) FROM samples").fetchone()
        elif not dates:
            return 0
        else:
            start, end = min(dates).isoformat(), max(dates).isoformat()
            rows = self._connection.execute(
                "SELECT trade_date FROM samples WHERE trade_date BETWEEN ? AND ?", (start, end)
            )
            return sum(date.fromisoformat(row[0]) in dates for row in rows)
        return int(row[0]) if row is not None else 0

    @property
    def database_size_bytes(self) -> int:
        return self._path.stat().st_size


def _encode(sample: TomorrowTrainingSample) -> tuple[object, ...]:
    if len(sample.features) != 6:
        raise ValueError("Tomorrow training sample feature width is invalid")
    return (
        sample.code,
        sample.trade_date.isoformat(),
        sample.board,
        sample.industry,
        sample.average_amount_20d,
        *sample.features,
        sample.target,
    )


def _empty_matrix(row_count: int) -> TomorrowTrainingSampleMatrix:
    return TomorrowTrainingSampleMatrix(
        np.empty((row_count, 6), dtype=np.float64),
        np.empty(row_count, dtype=np.float64),
    )


__all__ = [
    "SQLiteTomorrowTrainingSampleRepository",
    "TomorrowTrainingIndustryCounts",
    "TomorrowTrainingIndustryData",
    "TomorrowTrainingSample",
    "TomorrowTrainingSampleMatrix",
    "TomorrowTrainingSplitName",
]
