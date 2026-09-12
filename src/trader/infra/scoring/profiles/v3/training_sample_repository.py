"""SQLite repository for disposable Tomorrow training samples."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import cast

import numpy as np


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


class SQLiteTomorrowTrainingSampleRepository:
    """Own one disposable SQLite database used only during a training run."""

    def __init__(self, path: Path) -> None:
        self._connection = sqlite3.connect(path)
        self._connection.executescript(
            """
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            PRAGMA temp_store=FILE;
            PRAGMA cache_size=-131072;
            PRAGMA mmap_size=0;
            CREATE TABLE raw_samples (
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
            """
        )

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> SQLiteTomorrowTrainingSampleRepository:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def add_raw(self, samples: Iterable[TomorrowTrainingSample]) -> None:
        self._connection.executemany(
            "INSERT INTO raw_samples VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_encode(sample) for sample in samples),
        )
        self._connection.commit()

    def raw_dates(self) -> tuple[date, ...]:
        rows = self._connection.execute("SELECT DISTINCT trade_date FROM raw_samples ORDER BY trade_date")
        return tuple(date.fromisoformat(row[0]) for row in rows)

    def raw_count(self) -> int:
        row = self._connection.execute("SELECT COUNT(*) FROM raw_samples").fetchone()
        return int(row[0]) if row is not None else 0

    def raw_for_date(self, day: date) -> tuple[TomorrowTrainingSample, ...]:
        rows = self._connection.execute(
            "SELECT * FROM raw_samples WHERE trade_date=? ORDER BY code", (day.isoformat(),)
        )
        return tuple(_decode(row) for row in rows)

    def add_final(self, samples: Iterable[TomorrowTrainingSample]) -> None:
        self._connection.executemany(
            "INSERT INTO samples VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_encode(sample) for sample in samples),
        )
        self._connection.commit()

    def prepare_for_model_fitting(self) -> None:
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS samples_industry_date ON samples(industry, trade_date, code)"
        )
        self._connection.commit()

    def discard_raw_date(self, day: date) -> None:
        self._connection.execute("DELETE FROM raw_samples WHERE trade_date=?", (day.isoformat(),))
        self._connection.commit()

    def industries(self, dates: frozenset[date]) -> tuple[str, ...]:
        if not dates:
            return ()
        start, end = min(dates).isoformat(), max(dates).isoformat()
        rows = self._connection.execute(
            "SELECT DISTINCT industry, trade_date FROM samples WHERE trade_date BETWEEN ? AND ? "
            "ORDER BY industry, trade_date",
            (start, end),
        )
        return tuple(sorted({row[0] for row in rows if date.fromisoformat(row[1]) in dates}))

    def samples_for(self, industry: str, dates: frozenset[date]) -> tuple[TomorrowTrainingSample, ...]:
        if not dates:
            return ()
        start, end = min(dates).isoformat(), max(dates).isoformat()
        rows = self._connection.execute(
            "SELECT * FROM samples WHERE industry=? AND trade_date BETWEEN ? AND ? ORDER BY trade_date, code",
            (industry, start, end),
        )
        return tuple(sample for row in rows if (sample := _decode(row)).trade_date in dates)

    def matrix_for(self, industry: str, dates: frozenset[date]) -> TomorrowTrainingSampleMatrix:
        selected_dates = frozenset(day.isoformat() for day in dates)
        if not selected_dates:
            return TomorrowTrainingSampleMatrix(np.empty((0, 6), dtype=np.float64), np.empty(0, dtype=np.float64))
        start, end = min(selected_dates), max(selected_dates)
        query = (
            "SELECT trade_date, f0, f1, f2, f3, f4, f5, target FROM samples "
            "WHERE industry=? AND trade_date BETWEEN ? AND ? ORDER BY trade_date, code"
        )
        row_count = sum(
            str(row[0]) in selected_dates
            for row in self._connection.execute(
                "SELECT trade_date FROM samples WHERE industry=? AND trade_date BETWEEN ? AND ?",
                (industry, start, end),
            )
        )
        features = np.empty((row_count, 6), dtype=np.float64)
        labels = np.empty(row_count, dtype=np.float64)
        position = 0
        cursor = self._connection.execute(query, (industry, start, end))
        while rows := cursor.fetchmany(4_096):
            for row in rows:
                if str(row[0]) not in selected_dates:
                    continue
                features[position] = tuple(float(cast(float, value)) for value in row[1:7])
                labels[position] = float(cast(float, row[7]))
                position += 1
        if position != row_count:
            raise RuntimeError("V3 sample matrix count changed during its read")
        return TomorrowTrainingSampleMatrix(features, labels)

    def count_for(self, industry: str, dates: frozenset[date]) -> int:
        if not dates:
            return 0
        selected_dates = frozenset(day.isoformat() for day in dates)
        start, end = min(selected_dates), max(selected_dates)
        return sum(
            str(row[0]) in selected_dates
            for row in self._connection.execute(
                "SELECT trade_date FROM samples WHERE industry=? AND trade_date BETWEEN ? AND ?",
                (industry, start, end),
            )
        )

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


def _decode(row: tuple[object, ...]) -> TomorrowTrainingSample:
    return TomorrowTrainingSample(
        str(row[0]),
        date.fromisoformat(str(row[1])),
        str(row[2]),
        str(row[3]),
        float(cast(float, row[4])),
        tuple(float(cast(float, value)) for value in row[5:11]),
        float(cast(float, row[11])),
    )


__all__ = [
    "SQLiteTomorrowTrainingSampleRepository",
    "TomorrowTrainingSample",
    "TomorrowTrainingSampleMatrix",
]
