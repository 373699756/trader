"""Download-owned, digest-verified supplier industry queries."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy
from dataclasses import field

import hashlib
import json
import sqlite3
import re
from collections.abc import Callable, Generator
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from datetime import datetime
from datetime import timedelta
from pathlib import Path

from trader.download.domain.industry_evidence import OfficialIndustryEvidence
from trader.download.application.prepare_reference import IndustryDatabaseIncompatibleError
from trader.download.infra.industry_evidence_codec import IndustryEvidencePayload, ParseIndustryEvidence

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
_MAX_HISTORICAL_EVIDENCE = 256_000


def _source_byte_limit(request_key: str) -> int:
    if re.fullmatch(r"cninfo:p_(?:public0002:008001|stock2110:[0-9]{6})", request_key):
        return 2 * 1024 * 1024
    if request_key == "capco:catalog:root" or re.fullmatch(r"capco:announcement:[0-9a-f]{64}", request_key):
        return 2 * 1024 * 1024
    if re.fullmatch(r"capco:catalog:[0-9a-f]{64}", request_key):
        return 8 * 1024 * 1024
    # Parsed keys hold a verified derived row cache, never supplier response bytes.
    if re.fullmatch(r"capco:(?:pdf|parsed):[0-9a-f]{64}", request_key):
        return 32 * 1024 * 1024
    return 0


def _evidence_source(item: OfficialIndustryEvidence) -> tuple[str, str, str, str]:
    return item.code, item.announcement_url, item.announcement_sha256, item.document_sha256


@dataclass(frozen=True)
class IndustrySourceDocument:
    request_key: str
    requested_from: date
    requested_to: date
    received_at: datetime
    content: bytes

    def __post_init__(self) -> None:
        if (
            _source_byte_limit(self.request_key) == 0
            or type(self.requested_from) is not date or type(self.requested_to) is not date
            or self.requested_from > self.requested_to
            or self.received_at.utcoffset() != timedelta(hours=8)
            or self.requested_to > self.received_at.date()
            or not isinstance(self.content, bytes) or not 0 < len(self.content) <= _source_byte_limit(self.request_key)
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
class IndustrySourceDocumentRead:
    request_key: str
    document: IndustrySourceDocument | None
    failure_category: str | None = None


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
        if as_of.utcoffset() != timedelta(hours=8) or not _source_byte_limit(request_key):
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
        return self._source_document(row, as_of)

    def iter_source_documents(
        self,
        request_keys: tuple[str, ...],
        observed_at: datetime,
        *,
        cancel_requested: Callable[[], bool],
    ) -> Generator[IndustrySourceDocumentRead, None, None]:
        """Read diagnostic CNINFO originals in bounded chunks through one checked connection."""
        if (
            type(request_keys) is not tuple or not 1 <= len(request_keys) <= 12_000
            or len(set(request_keys)) != len(request_keys)
            or any(re.fullmatch(r"cninfo:p_stock2110:[0-9]{6}", key) is None for key in request_keys)
            or observed_at.utcoffset() != timedelta(hours=8)
        ):
            raise ValueError("industry_source_observation_invalid")
        if cancel_requested():
            raise RuntimeError("supplier_cancelled")
        if not self.path.is_file():
            for key in request_keys:
                if cancel_requested():
                    raise RuntimeError("supplier_cancelled")
                yield IndustrySourceDocumentRead(key, None)
            return
        # A chunk contains at most 128 MiB of declared supplier payloads and 65 parameters.
        batch_rows = min(64, self.sqlite_policy.read_window_rows)
        try:
            with SqliteSession(
                self.path, SqliteConnectionOptions(read_only=True), sqlite_policy=self.sqlite_policy,
                cancel_requested=cancel_requested,
            ) as connection:
                EnsureSqliteSchema(connection, _SCHEMA)
                for start in range(0, len(request_keys), batch_rows):
                    if cancel_requested():
                        raise RuntimeError("supplier_cancelled")
                    keys = request_keys[start:start + batch_rows]
                    values = ",".join(f"({position},?)" for position in range(len(keys)))
                    result = connection.run(
                        f"WITH wanted(position,request_key) AS (VALUES {values}) "
                        "SELECT d.request_key,d.requested_start,d.requested_end,d.received_at,d.payload,"
                        "d.content_hash,d.receipt_hash,w.request_key FROM wanted w "
                        "LEFT JOIN industry_source_document d ON (d.request_key,d.received_at,d.content_hash)=("
                        "SELECT p.request_key,p.received_at,p.content_hash FROM industry_source_document p "
                        "WHERE p.request_key=w.request_key AND p.received_at<=? "
                        "ORDER BY p.received_at DESC,p.content_hash DESC LIMIT 1) ORDER BY w.position",
                        (*keys, observed_at.isoformat()),
                    )
                    with closing(result):
                        for row in result:
                            if cancel_requested():
                                raise RuntimeError("supplier_cancelled")
                            key = str(row[7])
                            try:
                                document = self._source_document(row, observed_at) if row[0] is not None else None
                            except ValueError as exc:
                                reason = str(exc)
                                if re.fullmatch(r"industry_source_[a-z_]{1,100}", reason) is None:
                                    reason = "industry_source_document_invalid"
                                yield IndustrySourceDocumentRead(key, None, reason)
                            else:
                                yield IndustrySourceDocumentRead(key, document)
        except sqlite3.Error as exc:
            raise RuntimeError("industry_query_cache_unavailable") from exc

    def read_previous_source_document(
        self,
        request_key: str,
        observed_at: datetime,
        *,
        differing_from: str,
    ) -> IndustrySourceDocument | None:
        """Find the latest different original even after repeated copies of revised bytes."""
        if observed_at.utcoffset() != timedelta(hours=8) or not _source_byte_limit(request_key):
            raise ValueError("industry_source_observation_invalid")
        current_hash = EncodeHash(differing_from)
        if not self.path.is_file():
            return None
        try:
            with SqliteSession(self.path, SqliteConnectionOptions(read_only=True), sqlite_policy=self.sqlite_policy) as connection:
                EnsureSqliteSchema(connection, _SCHEMA)
                row = connection.run(
                    "SELECT request_key,requested_start,requested_end,received_at,payload,content_hash,receipt_hash "
                    "FROM industry_source_document WHERE request_key=? AND received_at<=? AND content_hash!=? "
                    "ORDER BY received_at DESC LIMIT 1",
                    (request_key, observed_at.isoformat(), current_hash),
                ).fetchone()
        except sqlite3.Error as exc:
            raise RuntimeError("industry_query_cache_unavailable") from exc
        return self._source_document(row, observed_at)

    def _source_document(self, row: sqlite3.Row | None, observed_at: datetime) -> IndustrySourceDocument | None:
        if row is None:
            return None
        document = IndustrySourceDocument(
            row[0], date.fromisoformat(row[1]), date.fromisoformat(row[2]), datetime.fromisoformat(row[3]), row[4],
        )
        if DecodeHash(row[5]) != document.content_hash or DecodeHash(row[6]) != document.receipt_hash:
            raise ValueError("industry_source_document_hash_conflict")
        if document.received_at > observed_at:
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
        single_official = len(documents) == 1 and documents[0].request_key.startswith("capco:")
        if sum(len(document.content) for document in documents) > self.source_batch_bytes and not single_official:
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
        """Replace same-source derived bounds atomically; preserve all original documents."""
        if (
            not records or len(records) > 1000
            or len({_evidence_source(item) for item in records}) != len(records)
        ):
            raise ValueError("industry_evidence_batch_invalid")
        sources = {_evidence_source(item) for item in records}
        identities = {_evidence_source(item): item.identity for item in records}
        by_code: dict[str, list[OfficialIndustryEvidence]] = {}
        for item in records:
            by_code.setdefault(item.code, []).append(item)
        previous_ids: set[str] = set()
        replace_ids: set[str] = set()
        self.prepare()
        with (
            SqliteSession(self.path, SqliteConnectionOptions(), sqlite_policy=self.sqlite_policy) as connection,
            SqliteTransaction(connection, immediate=True),
        ):
            previous_by_code: dict[str, list[OfficialIndustryEvidence]] = {}
            codes = tuple(by_code)
            for start in range(0, len(codes), 400):
                selected = codes[start:start + 400]
                placeholders = ",".join("?" for _ in selected)
                rows = connection.run(
                    "SELECT evidence_id,code,disclosed_at,effective_from,effective_to,received_at,payload "
                    f"FROM official_industry_evidence WHERE code IN ({placeholders}) ORDER BY code,evidence_id",
                    selected,
                )
                with closing(rows):
                    for row in rows:
                        old = self._evidence(row)
                        previous = previous_by_code.setdefault(old.code, [])
                        if len(previous) >= 1000:
                            raise ValueError("industry_evidence_overlap_limit")
                        previous.append(old)
            for item in records:
                previous = previous_by_code.get(item.code, [])
                for other in previous:
                    previous_ids.add(other.identity)
                    if _evidence_source(other) == _evidence_source(item) and (
                        other.industry != item.industry
                        or other.disclosed_on != item.disclosed_on
                        or other.page != item.page
                        or other.excerpt != item.excerpt
                    ):
                        raise ValueError("industry_evidence_source_conflict")
                    if _evidence_source(other) in sources and other.identity != identities[_evidence_source(other)]:
                        replace_ids.add(other.identity)
                remaining = tuple(other for other in previous if _evidence_source(other) not in sources)
                for other in (*remaining, *by_code[item.code]):
                    if (
                        item.code == other.code
                        and _evidence_source(item) != _evidence_source(other)
                        and max(item.effective_from, other.effective_from) < min(item.effective_to, other.effective_to)
                        and item.industry != other.industry
                    ):
                        raise ValueError("industry_evidence_interval_conflict")
            additions = tuple(item for item in records if item.identity not in previous_ids)
            if not additions and not replace_ids:
                return 0
            if replace_ids:
                connection.run_many("DELETE FROM official_industry_evidence WHERE evidence_id=?", ((identity,) for identity in replace_ids))
            connection.run_many(
                "INSERT OR IGNORE INTO official_industry_evidence VALUES(?,?,?,?,?,?,?)",
                (
                    (
                        item.identity, item.code, item.disclosed_on.isoformat(), item.effective_from.isoformat(),
                        item.effective_to.isoformat(), item.received_at.isoformat(),
                        json.dumps(IndustryEvidencePayload(item), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    ) for item in additions
                ),
            )
            return len(additions)

    def read_evidence_window(
        self,
        first_date: date,
        last_date: date,
        observed_at: datetime,
    ) -> tuple[OfficialIndustryEvidence, ...]:
        if (
            type(first_date) is not date
            or type(last_date) is not date
            or first_date > last_date
            or observed_at.utcoffset() != timedelta(hours=8)
            or last_date > observed_at.date()
        ):
            raise ValueError("industry_evidence_time_invalid")
        if not self.path.is_file():
            return ()
        try:
            with SqliteSession(self.path, SqliteConnectionOptions(read_only=True), sqlite_policy=self.sqlite_policy) as connection:
                EnsureSqliteSchema(connection, _SCHEMA)
                result = connection.run(
                    "SELECT evidence_id,code,disclosed_at,effective_from,effective_to,received_at,payload "
                    "FROM official_industry_evidence WHERE effective_from<=? AND effective_to>? "
                    "ORDER BY code,disclosed_at,evidence_id LIMIT ?",
                    (last_date.isoformat(), first_date.isoformat(), _MAX_HISTORICAL_EVIDENCE + 1),
                )
                records: list[OfficialIndustryEvidence] = []
                inspected = 0
                with closing(result):
                    while rows := result.fetchmany(4096):
                        inspected += len(rows)
                        if inspected > _MAX_HISTORICAL_EVIDENCE:
                            raise ValueError("industry_evidence_rows_limit")
                        for row in rows:
                            item = self._evidence(row)
                            if item.disclosed_on <= observed_at.date() and item.received_at <= observed_at:
                                records.append(item)
        except sqlite3.Error as exc:
            raise RuntimeError("industry_evidence_unavailable") from exc
        return tuple(records)

    def _evidence(self, row: sqlite3.Row) -> OfficialIndustryEvidence:
        item = ParseIndustryEvidence(json.loads(row[6]))
        expected = (
            item.identity, item.code, item.disclosed_on.isoformat(), item.effective_from.isoformat(),
            item.effective_to.isoformat(), item.received_at.isoformat(),
        )
        if tuple(row)[:6] != expected:
            raise ValueError("industry_evidence_hash_conflict")
        return item



__all__ = [
    "IndustryQuery",
    "IndustrySourceDocument",
    "IndustrySourceDocumentRead",
    "SQLiteIndustryQueryIndex",
]
