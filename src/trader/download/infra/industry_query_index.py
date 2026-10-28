"""Download-owned, digest-verified supplier industry queries."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from trader.infra.sqlite_codec import DecodeHash, EncodeHash
from trader.infra.sqlite_connection import (
    EnsureSqliteSchema,
    SqliteConnectionOptions,
    SqliteSchema,
    SqliteSession,
    SqliteTransaction,
)

_SCHEMA = SqliteSchema(
    1,
    tuple(
        f"CREATE TABLE {table} ({date_column} TEXT PRIMARY KEY, source TEXT NOT NULL, "
        "checked_date TEXT NOT NULL, members_json TEXT NOT NULL, "
        "content_hash BLOB NOT NULL CHECK(length(content_hash)=32)) WITHOUT ROWID"
        for table, date_column in (("industry_query", "anchor_date"), ("current_industry_query", "as_of_date"))
    ),
)


@dataclass(frozen=True)
class IndustryQuery:
    day: date
    source: str
    observed_on: date
    payload: str
    digest: str


@dataclass(frozen=True)
class SQLiteIndustryQueryIndex:
    path: Path
    contract: str
    table: str = "industry_query"

    def __post_init__(self) -> None:
        if self.table not in {"industry_query", "current_industry_query"}:
            raise ValueError("industry_query_table_invalid")

    @property
    def _date_column(self) -> str:
        return "anchor_date" if self.table == "industry_query" else "as_of_date"

    def prepare(self) -> None:
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise ValueError("industry_query_path_unsafe")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with SqliteSession(self.path, SqliteConnectionOptions(timeout_seconds=5)) as connection:
                EnsureSqliteSchema(connection, _SCHEMA, allow_create=True)
        except sqlite3.Error as exc:
            raise RuntimeError("industry_query_cache_unavailable") from exc

    def read(self, day: date, as_of: date, *, source: str | None = None) -> IndustryQuery | None:
        return self._query(f"{self._date_column}=?", (day.isoformat(),), as_of, source)

    def latest(self, as_of: date) -> IndustryQuery | None:
        if not self.path.is_file():
            return None
        try:
            with SqliteSession(self.path, SqliteConnectionOptions(read_only=True, timeout_seconds=5)) as connection:
                EnsureSqliteSchema(connection, _SCHEMA)
                rows = connection.run(
                    f"SELECT {self._date_column},source,checked_date,members_json,content_hash FROM {self.table} "
                    f"WHERE {self._date_column}<=? AND checked_date<=? ORDER BY {self._date_column} DESC LIMIT 256",
                    (as_of.isoformat(), as_of.isoformat()),
                ).fetchall()
        except sqlite3.Error as exc:
            raise RuntimeError("industry_query_cache_unavailable") from exc
        return next((query for row in rows if (query := self._verified(row, as_of)) is not None), None)

    def _query(
        self, predicate: str, parameters: tuple[str, ...], as_of: date, source: str | None
    ) -> IndustryQuery | None:
        if not self.path.is_file():
            return None
        try:
            with SqliteSession(self.path, SqliteConnectionOptions(read_only=True, timeout_seconds=5)) as connection:
                EnsureSqliteSchema(connection, _SCHEMA)
                row = connection.run(
                    f"SELECT {self._date_column},source,checked_date,members_json,content_hash FROM {self.table} WHERE "
                    + predicate
                    + f" ORDER BY {self._date_column} DESC LIMIT 1",
                    parameters,
                ).fetchone()
        except sqlite3.Error as exc:
            raise RuntimeError("industry_query_cache_unavailable") from exc
        if row is None or (source is not None and row[1] != source):
            return None
        return self._verified(row, as_of)

    def _verified(self, row: sqlite3.Row, as_of: date) -> IndustryQuery | None:
        try:
            query = IndustryQuery(
                date.fromisoformat(row[0]), row[1], date.fromisoformat(row[2]), row[3], DecodeHash(row[4])
            )
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
                SqliteSession(self.path, SqliteConnectionOptions(timeout_seconds=5)) as connection,
                SqliteTransaction(connection),
            ):
                connection.run(
                    f"INSERT OR REPLACE INTO {self.table} VALUES (?,?,?,?,?)",
                    (
                        day.isoformat(),
                        source,
                        observed_on.isoformat(),
                        payload,
                        EncodeHash(self._digest(source, day, observed_on, payload)),
                    ),
                )
        except sqlite3.Error as exc:
            raise RuntimeError("industry_query_cache_unavailable") from exc

    def _digest(self, source: str, day: date, observed_on: date, payload: str) -> str:
        return hashlib.sha256(
            json.dumps((self.contract, source, day.isoformat(), observed_on.isoformat(), payload)).encode()
        ).hexdigest()


__all__ = ["IndustryQuery", "SQLiteIndustryQueryIndex"]
