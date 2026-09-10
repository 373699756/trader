"""Disk-backed V3 sample workspace with bounded cross-sectional reads."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import cast


@dataclass(frozen=True)
class V3StoredSample:
    code: str
    trade_date: date
    board: str
    industry: str
    average_amount_20d: float
    features: tuple[float, ...]
    target: float


class V3SampleStore:
    """Own one disposable SQLite database used only during a training run."""

    def __init__(self, path: Path) -> None:
        self._connection = sqlite3.connect(path)
        self._connection.executescript(
            """
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            PRAGMA temp_store=FILE;
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
            CREATE INDEX samples_industry_date ON samples(industry, trade_date, code);
            """
        )

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> V3SampleStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def add_raw(self, samples: Iterable[V3StoredSample]) -> None:
        self._connection.executemany(
            "INSERT INTO raw_samples VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_encode(sample) for sample in samples),
        )
        self._connection.commit()

    def raw_dates(self) -> tuple[date, ...]:
        rows = self._connection.execute("SELECT DISTINCT trade_date FROM raw_samples ORDER BY trade_date")
        return tuple(date.fromisoformat(row[0]) for row in rows)

    def raw_for_date(self, day: date) -> tuple[V3StoredSample, ...]:
        rows = self._connection.execute(
            "SELECT * FROM raw_samples WHERE trade_date=? ORDER BY code", (day.isoformat(),)
        )
        return tuple(_decode(row) for row in rows)

    def add_final(self, samples: Iterable[V3StoredSample]) -> None:
        self._connection.executemany(
            "INSERT INTO samples VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_encode(sample) for sample in samples),
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

    def samples_for(self, industry: str, dates: frozenset[date]) -> tuple[V3StoredSample, ...]:
        if not dates:
            return ()
        start, end = min(dates).isoformat(), max(dates).isoformat()
        rows = self._connection.execute(
            "SELECT * FROM samples WHERE industry=? AND trade_date BETWEEN ? AND ? ORDER BY trade_date, code",
            (industry, start, end),
        )
        return tuple(sample for row in rows if (sample := _decode(row)).trade_date in dates)

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


def _encode(sample: V3StoredSample) -> tuple[object, ...]:
    if len(sample.features) != 6:
        raise ValueError("V3 stored sample feature width is invalid")
    return (
        sample.code,
        sample.trade_date.isoformat(),
        sample.board,
        sample.industry,
        sample.average_amount_20d,
        *sample.features,
        sample.target,
    )


def _decode(row: tuple[object, ...]) -> V3StoredSample:
    return V3StoredSample(
        str(row[0]),
        date.fromisoformat(str(row[1])),
        str(row[2]),
        str(row[3]),
        float(cast(float, row[4])),
        tuple(float(cast(float, value)) for value in row[5:11]),
        float(cast(float, row[11])),
    )


__all__ = ["V3SampleStore", "V3StoredSample"]
