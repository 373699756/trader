"""SQLite date cache for bounded, deterministic history training reads."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import closing
from datetime import date
from pathlib import Path
from typing import cast

from trader.domain.research.baostock_daily import BaoStockTrainingRow
from trader.domain.research.history_monthly import HistoryMonthlyRevision
from trader.infra.research.history_month_codec import (
    decode_history_monthly_revision,
    encode_history_monthly_revision,
)

_SCHEMA_IDENTITY = "history_training_cache"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CODE = re.compile(r"^[0-9]{6}$")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    schema_identity TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS training_rows (
    trade_date TEXT NOT NULL,
    code TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    PRIMARY KEY (trade_date, code)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS history_training_code_date_idx
ON training_rows(code, trade_date);
"""


class HistoryTrainingCacheError(RuntimeError):
    """The temporary training cache is inconsistent with its snapshot."""


class SQLiteHistoryTrainingCache:
    def __init__(self, path: Path, snapshot_hash: str) -> None:
        if _SHA256.fullmatch(snapshot_hash) is None:
            raise ValueError("history training snapshot hash is invalid")
        self._path = path
        self._snapshot_hash = snapshot_hash

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with closing(self._connection()) as connection, connection:
                connection.executescript(_SCHEMA)
                connection.execute(
                    "INSERT OR IGNORE INTO metadata(singleton, schema_identity, snapshot_hash) VALUES (1, ?, ?)",
                    (_SCHEMA_IDENTITY, self._snapshot_hash),
                )
                self._require_metadata(connection)
        except HistoryTrainingCacheError:
            raise
        except sqlite3.Error as exc:
            raise HistoryTrainingCacheError("history training cache initialization failed") from exc

    def write_revisions(self, revisions: Iterable[HistoryMonthlyRevision]) -> None:
        try:
            with closing(self._connection()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                self._require_metadata(connection)
                for value in revisions:
                    if value.training_row is None:
                        raise ValueError("history training cache only accepts complete training rows")
                    payload_json = encode_history_monthly_revision(value)
                    existing = connection.execute(
                        "SELECT payload_json, content_hash FROM training_rows WHERE trade_date=? AND code=?",
                        (value.trade_date.isoformat(), value.code),
                    ).fetchone()
                    if existing is not None and existing != (payload_json, value.content_hash):
                        raise HistoryTrainingCacheError("history training row conflicts")
                    connection.execute(
                        "INSERT OR IGNORE INTO training_rows"
                        "(trade_date, code, payload_json, content_hash) VALUES (?, ?, ?, ?)",
                        (value.trade_date.isoformat(), value.code, payload_json, value.content_hash),
                    )
        except HistoryTrainingCacheError:
            raise
        except sqlite3.Error as exc:
            raise HistoryTrainingCacheError("history training row write failed") from exc

    def read_date(self, trade_date: date) -> tuple[BaoStockTrainingRow, ...]:
        return tuple(
            self._iter_query(
                "SELECT trade_date, code, payload_json, content_hash FROM training_rows "
                "WHERE trade_date=? ORDER BY code",
                (trade_date.isoformat(),),
            )
        )

    def iter_code(self, code: str) -> Iterator[BaoStockTrainingRow]:
        if _CODE.fullmatch(code) is None:
            raise ValueError("history training cache code is invalid")
        yield from self._iter_query(
            "SELECT trade_date, code, payload_json, content_hash FROM training_rows WHERE code=? ORDER BY trade_date",
            (code,),
        )

    def _iter_query(self, sql: str, parameters: tuple[str, ...]) -> Iterator[BaoStockTrainingRow]:
        try:
            with closing(self._read_connection()) as connection:
                self._require_metadata(connection)
                cursor = connection.execute(sql, parameters)
                while rows := cursor.fetchmany(512):
                    for row in rows:
                        yield _decode_training_row(row)
        except HistoryTrainingCacheError:
            raise
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise HistoryTrainingCacheError("history training cache read failed") from exc

    def _require_metadata(self, connection: sqlite3.Connection) -> None:
        metadata = connection.execute(
            "SELECT schema_identity, snapshot_hash FROM metadata WHERE singleton=1"
        ).fetchone()
        if metadata != (_SCHEMA_IDENTITY, self._snapshot_hash):
            raise HistoryTrainingCacheError("history training cache identity conflicts")

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5.0)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _read_connection(self) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{self._path.as_posix()}?mode=ro", uri=True, timeout=5.0)


def _decode_training_row(row: tuple[object, ...]) -> BaoStockTrainingRow:
    if len(row) != 4:
        raise HistoryTrainingCacheError("history training row shape is invalid")
    trade_date_value, code, payload_json, content_hash = row
    if not all(isinstance(value, str) for value in row):
        raise HistoryTrainingCacheError("history training row fields are invalid")
    revision = decode_history_monthly_revision(cast(str, payload_json))
    training_row = revision.training_row
    if (
        revision.trade_date.isoformat() != trade_date_value
        or revision.code != code
        or revision.content_hash != content_hash
        or training_row is None
    ):
        raise HistoryTrainingCacheError("history training row identity is inconsistent")
    return training_row


__all__ = ["HistoryTrainingCacheError", "SQLiteHistoryTrainingCache"]
