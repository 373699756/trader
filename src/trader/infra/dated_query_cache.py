"""SQLite primitive for dated, digest-verified JSON query evidence."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from trader.infra.sqlite_connection import OpenSqliteConnection, SqliteConnection, SqliteConnectionOptions


@dataclass(frozen=True)
class DatedQuery:
    day: date
    source: str
    observed_on: date
    payload: str
    digest: str


@dataclass(frozen=True)
class SQLiteDatedQueryCache:
    path: Path
    contract: str
    table: str = "queries"

    def __post_init__(self) -> None:
        if self.table not in {"queries", "current_industry_queries"}:
            raise ValueError("dated_query_cache_table_invalid")

    def prepare(self) -> None:
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise ValueError("dated_query_cache_path_unsafe")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with (
                closing(OpenSqliteConnection(self.path, SqliteConnectionOptions(timeout_seconds=5))) as connection,
                connection,
            ):
                connection.run(
                    f"CREATE TABLE IF NOT EXISTS {self.table} (day TEXT PRIMARY KEY, source TEXT NOT NULL, "
                    "observed_on TEXT NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL) WITHOUT ROWID"
                )
        except sqlite3.Error as exc:
            raise RuntimeError("industry_query_cache_unavailable") from exc

    def read(self, day: date, as_of: date, *, source: str | None = None) -> DatedQuery | None:
        return self._query("day=?", (day.isoformat(),), as_of, source)

    def latest(self, as_of: date) -> DatedQuery | None:
        if not self.path.is_file():
            return None
        try:
            with closing(
                OpenSqliteConnection(self.path, SqliteConnectionOptions(read_only=True, timeout_seconds=5))
            ) as connection:
                if not self._table_exists(connection):
                    return None
                rows = connection.run(
                    f"SELECT day,source,observed_on,payload,digest FROM {self.table} "
                    "WHERE day<=? AND observed_on<=? ORDER BY day DESC LIMIT 256",
                    (as_of.isoformat(), as_of.isoformat()),
                ).fetchall()
        except sqlite3.Error as exc:
            raise RuntimeError("industry_query_cache_unavailable") from exc
        return next((query for row in rows if (query := self._verified(row, as_of)) is not None), None)

    def _query(self, predicate: str, parameters: tuple[str, ...], as_of: date, source: str | None) -> DatedQuery | None:
        if not self.path.is_file():
            return None
        try:
            with closing(
                OpenSqliteConnection(self.path, SqliteConnectionOptions(read_only=True, timeout_seconds=5))
            ) as connection:
                if not self._table_exists(connection):
                    return None
                row = connection.run(
                    f"SELECT day,source,observed_on,payload,digest FROM {self.table} WHERE "
                    + predicate
                    + " ORDER BY day DESC LIMIT 1",
                    parameters,
                ).fetchone()
        except sqlite3.Error as exc:
            raise RuntimeError("industry_query_cache_unavailable") from exc
        if row is None or (source is not None and row[1] != source):
            return None
        return self._verified(row, as_of)

    def _table_exists(self, connection: SqliteConnection) -> bool:
        return (
            connection.run("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (self.table,)).fetchone()
            is not None
        )

    def _verified(self, row: sqlite3.Row, as_of: date) -> DatedQuery | None:
        try:
            query = DatedQuery(date.fromisoformat(row[0]), row[1], date.fromisoformat(row[2]), row[3], row[4])
            if not query.day <= query.observed_on <= as_of:
                return None
            if query.digest != self._digest(query.source, query.day, query.observed_on, query.payload):
                return None
        except (TypeError, ValueError):
            return None
        return query

    def save(self, source: str, day: date, observed_on: date, payload: str) -> None:
        if not source or not day <= observed_on:
            raise ValueError("industry_cache_observation_invalid")
        self.prepare()
        try:
            with (
                closing(OpenSqliteConnection(self.path, SqliteConnectionOptions(timeout_seconds=5))) as connection,
                connection,
            ):
                connection.run(
                    f"INSERT OR REPLACE INTO {self.table} VALUES (?,?,?,?,?)",
                    (
                        day.isoformat(),
                        source,
                        observed_on.isoformat(),
                        payload,
                        self._digest(source, day, observed_on, payload),
                    ),
                )
        except sqlite3.Error as exc:
            raise RuntimeError("industry_query_cache_unavailable") from exc

    def _digest(self, source: str, day: date, observed_on: date, payload: str) -> str:
        return hashlib.sha256(
            json.dumps((self.contract, source, day.isoformat(), observed_on.isoformat(), payload)).encode()
        ).hexdigest()


__all__ = ["DatedQuery", "SQLiteDatedQueryCache"]
