"""Download-owned, digest-verified supplier industry queries."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy
from dataclasses import field

import hashlib
import json
import sqlite3
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from datetime import datetime
from datetime import timedelta
from pathlib import Path

from trader.download.domain.industry_evidence import OfficialIndustryEvidence
from trader.download.application.prepare_reference import IndustryDatabaseIncompatibleError
from trader.download.infra.industry_evidence_codec import IndustryEvidencePayload, LoadReviewedIndustryEvidence, ParseIndustryEvidence

from trader.infra.sqlite_codec import DecodeHash, EncodeHash
from trader.infra.sqlite_connection import (
    EnsureSqliteSchema,
    SqliteConnectionOptions,
    SqliteSchema,
    SqliteSession,
    SqliteTransaction,
)

_EVIDENCE_DDL = """CREATE TABLE official_industry_evidence(
    evidence_id TEXT PRIMARY KEY, code TEXT NOT NULL, disclosed_at TEXT NOT NULL,
    effective_from TEXT NOT NULL, effective_to TEXT NOT NULL, received_at TEXT NOT NULL,
    payload TEXT NOT NULL
) WITHOUT ROWID"""
_EVIDENCE_INDEX_DDL = "CREATE INDEX official_industry_interval ON official_industry_evidence(code,effective_from,effective_to)"
_SOURCE_DDL = """CREATE TABLE industry_source_document(
    request_key TEXT NOT NULL, requested_start TEXT NOT NULL, requested_end TEXT NOT NULL,
    received_at TEXT NOT NULL, payload BLOB NOT NULL,
    content_hash BLOB NOT NULL CHECK(length(content_hash)=32),
    receipt_hash BLOB NOT NULL CHECK(length(receipt_hash)=32),
    PRIMARY KEY(request_key,received_at,content_hash)
) WITHOUT ROWID"""

_SCHEMA = SqliteSchema(
    tuple(
        f"CREATE TABLE {table} ({date_column} TEXT PRIMARY KEY, source TEXT NOT NULL, "
        "checked_date TEXT NOT NULL, members_json TEXT NOT NULL, "
        "content_hash BLOB NOT NULL CHECK(length(content_hash)=32)) WITHOUT ROWID"
        for table, date_column in (("current_industry_query", "as_of_date"),)
    ) + (_EVIDENCE_DDL, _EVIDENCE_INDEX_DDL, _SOURCE_DDL),
)


@dataclass(frozen=True)
class IndustrySourceDocument:
    request_key: str
    requested_from: date
    requested_to: date
    received_at: datetime
    content: bytes

    def __post_init__(self) -> None:
        if (
            re.fullmatch(r"cninfo:p_(?:public0002:008001|stock2110:[0-9]{6})", self.request_key) is None
            or type(self.requested_from) is not date or type(self.requested_to) is not date
            or self.requested_from > self.requested_to
            or self.received_at.utcoffset() != timedelta(hours=8)
            or self.requested_to > self.received_at.date()
            or not isinstance(self.content, bytes) or not 0 < len(self.content) <= 2 * 1024 * 1024
        ):
            raise ValueError("industry_source_document_invalid")

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    @property
    def receipt_hash(self) -> str:
        return hashlib.sha256(json.dumps(
            (self.request_key, self.requested_from.isoformat(), self.requested_to.isoformat(),
             self.received_at.isoformat(), self.content_hash), separators=(",", ":"),
        ).encode("utf-8")).hexdigest()


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
    table: str = "current_industry_query"

    sqlite_policy: SqliteIoPolicy = field(kw_only=True)

    def __post_init__(self) -> None:
        if self.table != "current_industry_query":
            raise ValueError("industry_query_table_invalid")

    @property
    def _date_column(self) -> str:
        return "as_of_date"

    def prepare(self) -> None:
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise ValueError("industry_query_path_unsafe")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with SqliteSession(self.path, SqliteConnectionOptions(), sqlite_policy=self.sqlite_policy) as connection:
                EnsureSqliteSchema(connection, _SCHEMA, allow_create=True)
        except sqlite3.Error as exc:
            if str(exc) == "sqlite_schema_unsupported":
                raise IndustryDatabaseIncompatibleError("industry_database_incompatible") from exc
            raise RuntimeError("industry_query_cache_unavailable") from exc

    def read_source_document(self, request_key: str, as_of: datetime) -> IndustrySourceDocument | None:
        if as_of.utcoffset() != timedelta(hours=8):
            raise ValueError("industry_source_observation_invalid")
        if not self.path.is_file():
            return None
        try:
            with SqliteSession(self.path, SqliteConnectionOptions(read_only=True), sqlite_policy=self.sqlite_policy) as connection:
                EnsureSqliteSchema(connection, _SCHEMA)
                row = connection.run(
                    "SELECT request_key,requested_start,requested_end,received_at,payload,content_hash,receipt_hash "
                    "FROM industry_source_document WHERE request_key=? AND received_at<=? "
                    "ORDER BY received_at DESC LIMIT 1", (request_key, as_of.isoformat()),
                ).fetchone()
        except sqlite3.Error as exc:
            raise RuntimeError("industry_query_cache_unavailable") from exc
        if row is None:
            return None
        document = IndustrySourceDocument(
            row[0], date.fromisoformat(row[1]), date.fromisoformat(row[2]), datetime.fromisoformat(row[3]), row[4],
        )
        if DecodeHash(row[5]) != document.content_hash or DecodeHash(row[6]) != document.receipt_hash:
            raise ValueError("industry_source_document_hash_conflict")
        if document.received_at > as_of:
            return None
        return document

    @property
    def source_batch_rows(self) -> int:
        return min(self.sqlite_policy.transaction_batch_rows, 64)

    @property
    def source_batch_bytes(self) -> int:
        return 2 * 1024 * 1024

    def save_source_documents(self, documents: tuple[IndustrySourceDocument, ...]) -> None:
        """Commit one bounded set of original replies and receipts atomically."""
        if (
            not isinstance(documents, tuple) or not documents or len(documents) > self.source_batch_rows
            or any(not isinstance(document, IndustrySourceDocument) for document in documents)
            or len({document.request_key for document in documents}) != len(documents)
        ):
            raise ValueError("industry_source_batch_invalid")
        if sum(len(document.content) for document in documents) > self.source_batch_bytes:
            raise ValueError("industry_source_batch_size_limit")
        self.prepare()
        try:
            with (
                SqliteSession(self.path, SqliteConnectionOptions(), sqlite_policy=self.sqlite_policy) as connection,
                SqliteTransaction(connection),
            ):
                for document in documents:
                    previous = connection.run(
                        "SELECT received_at,requested_start,requested_end,content_hash FROM industry_source_document WHERE request_key=? "
                        "ORDER BY received_at DESC LIMIT 1",
                        (document.request_key,),
                    ).fetchone()
                    if previous is not None and (
                        datetime.fromisoformat(previous[0]) > document.received_at
                        or date.fromisoformat(previous[1]) < document.requested_from
                        or date.fromisoformat(previous[2]) > document.requested_to
                    ):
                        raise ValueError("industry_source_document_would_shrink")
                    if previous is not None and datetime.fromisoformat(previous[0]) == document.received_at and (
                        DecodeHash(previous[3]) != document.content_hash
                        or date.fromisoformat(previous[1]) != document.requested_from
                        or date.fromisoformat(previous[2]) != document.requested_to
                    ):
                        raise ValueError("industry_source_document_time_conflict")
                    connection.run(
                        "INSERT OR IGNORE INTO industry_source_document VALUES(?,?,?,?,?,?,?)",
                        (document.request_key, document.requested_from.isoformat(), document.requested_to.isoformat(),
                         document.received_at.isoformat(), document.content, EncodeHash(document.content_hash), EncodeHash(document.receipt_hash)),
                    )
        except sqlite3.Error as exc:
            raise RuntimeError("industry_query_cache_unavailable") from exc

    def read(self, day: date, as_of: date, *, source: str | None = None) -> IndustryQuery | None:
        return self._query(f"{self._date_column}=?", (day.isoformat(),), as_of, source)

    def latest(self, as_of: date) -> IndustryQuery | None:
        if not self.path.is_file():
            return None
        try:
            with SqliteSession(self.path, SqliteConnectionOptions(read_only=True), sqlite_policy=self.sqlite_policy) as connection:
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
            with SqliteSession(self.path, SqliteConnectionOptions(read_only=True), sqlite_policy=self.sqlite_policy) as connection:
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
                SqliteSession(self.path, SqliteConnectionOptions(), sqlite_policy=self.sqlite_policy) as connection,
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

    def build_query(self, source: str, day: date, observed_on: date, payload: str) -> IndustryQuery:
        if not source or day > observed_on:
            raise ValueError("industry_cache_observation_invalid")
        return IndustryQuery(day, source, observed_on, payload, self._digest(source, day, observed_on, payload))

    def import_evidence(self, records: tuple[OfficialIndustryEvidence, ...]) -> int:
        if not records or len(records) > 1000 or len({item.identity for item in records}) != len(records):
            raise ValueError("industry_evidence_batch_invalid")
        self.prepare()
        with (
            SqliteSession(self.path, SqliteConnectionOptions(), sqlite_policy=self.sqlite_policy) as connection,
            SqliteTransaction(connection, immediate=True),
        ):
            for item in records:
                rows = connection.run(
                    "SELECT evidence_id,code,disclosed_at,effective_from,effective_to,received_at,payload "
                    "FROM official_industry_evidence WHERE code=? AND effective_from<? AND effective_to>? LIMIT 1001",
                    (item.code, item.effective_to.isoformat(), item.effective_from.isoformat()),
                ).fetchall()
                if len(rows) > 1000:
                    raise ValueError("industry_evidence_overlap_limit")
                previous = tuple(self._evidence(row) for row in rows)
                for other in (*previous, *records):
                    if (
                        item.code == other.code and item.identity != other.identity
                        and max(item.effective_from, other.effective_from) < min(item.effective_to, other.effective_to)
                        and item.mapping.target_industry != other.mapping.target_industry
                    ):
                        raise ValueError("industry_evidence_interval_conflict")
            before = connection.run("SELECT COUNT(*) FROM official_industry_evidence").require_one()[0]
            connection.run_many(
                "INSERT OR IGNORE INTO official_industry_evidence VALUES(?,?,?,?,?,?,?)",
                (
                    (
                        item.identity, item.code, item.disclosed_at.isoformat(), item.effective_from.isoformat(),
                        item.effective_to.isoformat(), item.received_at.isoformat(),
                        json.dumps(IndustryEvidencePayload(item), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    ) for item in records
                ),
            )
            return int(connection.run("SELECT COUNT(*) FROM official_industry_evidence").require_one()[0] - before)

    def import_reviewed_file(
        self,
        path: Path,
        *,
        observed_at: datetime,
        cancelled: Callable[[], bool],
    ) -> bool:
        if cancelled():
            return False
        records = LoadReviewedIndustryEvidence(path)
        if any(item.received_at > observed_at for item in records):
            raise ValueError("industry_evidence_received_in_future")
        if cancelled():
            return False
        self.import_evidence(records)
        return True

    def read_evidence(self, as_of: datetime) -> tuple[OfficialIndustryEvidence, ...]:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("industry_evidence_time_invalid")
        if not self.path.is_file():
            return ()
        try:
            with SqliteSession(self.path, SqliteConnectionOptions(read_only=True), sqlite_policy=self.sqlite_policy) as connection:
                EnsureSqliteSchema(connection, _SCHEMA)
                rows = connection.run(
                    "SELECT evidence_id,code,disclosed_at,effective_from,effective_to,received_at,payload "
                    "FROM official_industry_evidence WHERE effective_from<=? AND effective_to>? "
                    "ORDER BY code,disclosed_at,evidence_id LIMIT 20001",
                    (as_of.date().isoformat(), as_of.date().isoformat()),
                ).fetchall()
        except sqlite3.Error as exc:
            raise RuntimeError("industry_evidence_unavailable") from exc
        if len(rows) > 20000:
            raise ValueError("industry_evidence_rows_limit")
        records = tuple(self._evidence(row) for row in rows)
        return tuple(item for item in records if item.disclosed_at <= as_of and item.received_at <= as_of)

    def _evidence(self, row: sqlite3.Row) -> OfficialIndustryEvidence:
        item = ParseIndustryEvidence(json.loads(row[6]))
        expected = (
            item.identity, item.code, item.disclosed_at.isoformat(), item.effective_from.isoformat(),
            item.effective_to.isoformat(), item.received_at.isoformat(),
        )
        if tuple(row)[:6] != expected:
            raise ValueError("industry_evidence_hash_conflict")
        return item



__all__ = [
    "IndustryQuery",
    "IndustrySourceDocument",
    "SQLiteIndustryQueryIndex",
]
