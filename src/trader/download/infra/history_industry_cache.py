"""Bounded successful supplier-query evidence, never a published industry mapping."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from trader.download.domain.baostock_daily import BaoStockIndustryInterval

_LOGGER = logging.getLogger(__name__)
_CONTRACT = "baostock_dated_industry_queries"


@dataclass(frozen=True)
class HistoryIndustryQueryCache:
    """Used under the history maintenance lock; each complete date commits separately."""

    path: Path

    @contextmanager
    def _connection(self, *, read_only: bool = False) -> Iterator[sqlite3.Connection]:
        target = f"file:{self.path.as_posix()}?mode=ro" if read_only else str(self.path)
        try:
            with closing(sqlite3.connect(target, uri=read_only, timeout=5)) as connection:
                yield connection
        except sqlite3.Error as exc:
            raise RuntimeError("industry_query_cache_unavailable") from exc

    def prepare(self, source: str, dates: tuple[date, ...]) -> None:
        if not dates or len(dates) > 64 or len(set(dates)) != len(dates):
            raise ValueError("industry_cache_dates_invalid")
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise ValueError("industry_cache_path_unsafe")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection, connection:
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(
                "BEGIN IMMEDIATE; CREATE TABLE IF NOT EXISTS queries ("
                "day TEXT PRIMARY KEY, source TEXT NOT NULL, observed_on TEXT NOT NULL, "
                "payload TEXT NOT NULL, digest TEXT NOT NULL) WITHOUT ROWID;"
            )
            connection.execute(
                "DELETE FROM queries WHERE source!=? OR day NOT IN (" + ",".join("?" for _ in dates) + ")",
                (source, *(day.isoformat() for day in dates)),
            )

    def read(self, source: str, day: date, as_of: date) -> tuple[BaoStockIndustryInterval, ...] | None:
        with self._connection(read_only=True) as connection:
            row = connection.execute(
                "SELECT source,observed_on,payload,digest FROM queries WHERE day=?", (day.isoformat(),)
            ).fetchone()
        if row is None or row[0] != source:
            return None
        try:
            observed = date.fromisoformat(row[1])
            age = (as_of - observed).days
            # Recent snapshots refresh each data-cutoff day; old facts refresh monthly.
            lifetime = 30 if (as_of - day).days >= 180 else 1
            if observed < day or not 0 <= age < lifetime:
                return None
            if row[3] != _digest(source, day, observed, row[2]):
                raise ValueError("industry_cache_hash_mismatch")
            return _decode(row[2], day)
        except (TypeError, ValueError):
            _LOGGER.warning("industry_query_cache_invalid_requery day=%s", day)
            return None

    def save(self, source: str, day: date, as_of: date, facts: tuple[BaoStockIndustryInterval, ...]) -> None:
        if not facts:
            return
        payload = json.dumps(
            [[item.code, item.effective_from.isoformat(), item.industry, item.classification] for item in facts],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        _decode(payload, day)
        if as_of < day:
            raise ValueError("industry_cache_observation_invalid")
        with self._connection() as connection, connection:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(
                "INSERT OR REPLACE INTO queries VALUES (?,?,?,?,?)",
                (day.isoformat(), source, as_of.isoformat(), payload, _digest(source, day, as_of, payload)),
            )


def _digest(source: str, day: date, observed: date, payload: str) -> str:
    return hashlib.sha256(
        json.dumps((_CONTRACT, source, day.isoformat(), observed.isoformat(), payload)).encode()
    ).hexdigest()


def _decode(payload: str, day: date) -> tuple[BaoStockIndustryInterval, ...]:
    values = json.loads(payload)
    if not isinstance(values, list) or not values or len(values) > 20000:
        raise ValueError("industry_cache_rows_invalid")
    facts = []
    for row in values:
        if not isinstance(row, list) or len(row) != 4 or not all(isinstance(item, str) for item in row):
            raise ValueError("industry_cache_row_invalid")
        fact = BaoStockIndustryInterval(row[0], date.fromisoformat(row[1]), None, row[2], row[3])
        if fact.effective_from > day:
            raise ValueError("industry_cache_future_fact")
        facts.append(fact)
    if len({item.code for item in facts}) != len(facts):
        raise ValueError("industry_cache_duplicate_code")
    return tuple(facts)


__all__ = ["HistoryIndustryQueryCache"]
