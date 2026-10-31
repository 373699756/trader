"""One SQLite owner for ST collection and immutable market reference publications."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Literal, cast

from trader.download.domain.baostock_daily import BaoStockBoard, BaoStockSecurity
from trader.download.domain.company_evidence import EvidenceSyncProgress
from trader.download.domain.history_reference import HistoryStEvidence
from trader.download.domain.prepared_reference import PreparedReference
from trader.download.infra.reference_schema import REFERENCE_SCHEMA
from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.infra.sqlite_codec import DecodeDate, DecodeHash, DecodeTime, EncodeDate, EncodeHash, EncodeTime
from trader.infra.sqlite_connection import (
    EnsureSqliteSchema,
    OpenSqliteConnection,
    SqliteConnection,
    SqliteConnectionOptions,
    SqliteTransaction,
)


class SQLiteReferenceIndex:
    def __init__(self, path: Path, *, sqlite_policy: SqliteIoPolicy) -> None:
        self._sqlite_policy = sqlite_policy
        self.path = path

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[SqliteConnection]:
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise ValueError("reference_path_unsafe")
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with closing(self._open(write=write)) as connection, SqliteTransaction(connection, immediate=write):
                EnsureSqliteSchema(
                    connection,
                    REFERENCE_SCHEMA,
                    allow_create=write,
                    initialize=_initialize_contract,
                    validate=_validate_contract,
                )
                yield connection
        except sqlite3.Error as exc:
            raise ValueError("reference_database_unavailable") from exc

    def _open(self, *, write: bool) -> SqliteConnection:
        # Short reference writes use SQLite's rollback journal and FULL durability.
        # Avoid repeated WAL creation/checkpointing when each batch closes its handle.
        return OpenSqliteConnection(self.path, SqliteConnectionOptions(read_only=not write, foreign_keys=True), sqlite_policy=self._sqlite_policy)

    def read_st(self) -> tuple[HistoryStEvidence, ...]:
        if not self.path.is_file():
            return ()
        with self._connection() as connection:
            return _current_st(connection)

    def merge_st(
        self, incoming: tuple[HistoryStEvidence, ...], observed: tuple[HistoryStEvidence, ...] = ()
    ) -> tuple[HistoryStEvidence, ...]:
        if len({item.code for item in incoming}) != len(incoming):
            raise ValueError("reference_st_duplicate")
        with self._connection(write=True) as connection:
            existing = _current_st(connection)
            merged = {item.code: item for item in existing}
            baseline = {item.code: item for item in observed}
            changed = []
            for item in incoming:
                if baseline.get(item.code) == item:
                    continue
                previous = merged.get(item.code)
                if previous is not None and not _incoming_wins(previous, item, baseline.get(item.code)):
                    continue
                merged[item.code] = item
                changed.append(item)
            _insert_st(connection, tuple(changed))
            connection.run_many(
                "INSERT INTO security_st_current VALUES(?,?) ON CONFLICT(code) DO UPDATE SET st_id=excluded.st_id",
                ((item.code, EncodeHash(CanonicalDigest(item))) for item in changed),
            )
            return tuple(sorted(merged.values(), key=lambda item: item.code))

    def publish(self, reference: PreparedReference, evidence: tuple[HistoryStEvidence, ...]) -> None:
        source = tuple(sorted(evidence, key=lambda item: item.code))
        projected = tuple(replace(item, checked_on=reference.checked_at.date()) for item in source)
        if projected != reference.st_evidence or any(item.checked_on > reference.checked_at.date() for item in source):
            raise ValueError("reference_st_publication_conflict")
        # A reused permanent exclusion retains its original supplier check date.
        if any(item.status != "ever_st" and item.checked_on != reference.checked_at.date() for item in source):
            raise ValueError("reference_st_publication_stale")
        calendar_hash = EncodeHash(CanonicalDigest(reference.open_dates))
        with self._connection(write=True) as connection:
            existing = connection.run(
                "SELECT 1 FROM reference_publication WHERE publication_id=?", (reference.identity,)
            ).fetchone()
            if existing is not None:
                previous = _read(connection, reference.identity)
                if previous is None or replace(reference, checked_at=previous.checked_at) != previous:
                    raise ValueError("reference_publication_hash_conflict")
                return
            connection.run_many(
                "INSERT OR IGNORE INTO security VALUES(?,?,?,?,?,?,?,?)",
                (
                    (
                        EncodeHash(item.content_hash),
                        item.code,
                        item.name,
                        item.board,
                        EncodeDate(item.listed_on),
                        EncodeDate(item.delisted_on) if item.delisted_on else None,
                        item.source_version,
                        EncodeHash(item.content_hash),
                    )
                    for item in reference.official_universe
                ),
            )
            _insert_st(connection, source)
            connection.run(
                "INSERT OR IGNORE INTO calendar_version VALUES(?,?,?,?,?)",
                (
                    calendar_hash,
                    calendar_hash,
                    EncodeDate(reference.open_dates[0]),
                    EncodeDate(reference.open_dates[-1]),
                    len(reference.open_dates),
                ),
            )
            for start in range(0, len(reference.open_dates), 256):
                dates = reference.open_dates[start : start + 256]
                connection.run(
                    "INSERT OR IGNORE INTO trading_calendar VALUES" + ",".join("(?,?)" for _ in dates),
                    tuple(value for day in dates for value in (calendar_hash, EncodeDate(day))),
                )
            connection.run(
                "INSERT INTO reference_publication VALUES(?,?,?,?,?,?)",
                (
                    reference.identity,
                    EncodeHash(reference.identity),
                    EncodeTime(reference.checked_at),
                    calendar_hash,
                    reference.industry_version,
                    len(reference.official_universe),
                ),
            )
            connection.run_many(
                "INSERT INTO reference_member VALUES(?,?,?,?)",
                (
                    (
                        reference.identity,
                        security.code,
                        EncodeHash(security.content_hash),
                        EncodeHash(CanonicalDigest(st)),
                    )
                    for security, st in zip(reference.official_universe, source, strict=True)
                ),
            )
            if _read(connection, reference.identity) != reference:
                raise ValueError("reference_publication_hash_conflict")
            connection.run(
                "INSERT INTO reference_current VALUES(1,?) "
                "ON CONFLICT(singleton) DO UPDATE SET publication_id=excluded.publication_id",
                (reference.identity,),
            )

    def read(self, publication_id: str | None = None) -> PreparedReference | None:
        if not self.path.is_file():
            if publication_id is not None:
                raise ValueError("reference_publication_missing")
            return None
        with self._connection() as connection:
            return _read(connection, publication_id)

    def identity(self) -> str | None:
        if not self.path.is_file():
            return None
        with self._connection() as connection:
            row = connection.run(
                "SELECT p.publication_id,p.content_hash FROM reference_current c "
                "JOIN reference_publication p USING(publication_id) WHERE c.singleton=1"
            ).fetchone()
            if row is None:
                return None
            if row[0] != DecodeHash(row[1]):
                raise ValueError("reference_publication_hash_conflict")
            return cast(str, row[0])

    def calendar(
        self,
        checked_on: date,
        *,
        fetch: Callable[[], tuple[date, ...]],
        progress: Callable[[EvidenceSyncProgress], None] | None,
    ) -> tuple[date, ...]:
        failure = ""
        try:
            reference = self.read()
        except (OSError, ValueError):
            reference = None
            failure = "reference_cache_invalid"
        reused = reference is not None and reference.checked_at.date() == checked_on
        if progress is not None:
            progress(
                EvidenceSyncProgress(
                    "calendar",
                    int(reused),
                    0 if reused else None,
                    0,
                    0,
                    int(reused),
                    failure,
                    status="reused" if reused else "running",
                )
            )
        if reused and reference is not None:
            return reference.open_dates
        dates = fetch()
        if progress is not None:
            progress(EvidenceSyncProgress("calendar", 1, 0, 0, 0, 0, status="complete"))
        return dates


def _initialize_contract(connection: SqliteConnection) -> None:
    connection.run("INSERT INTO schema_contract VALUES('market_reference',1)")


def _validate_contract(connection: SqliteConnection) -> None:
    rows = connection.run("SELECT schema_id,schema_version FROM schema_contract").fetchall()
    if [tuple(row) for row in rows] != [("market_reference", 1)]:
        raise ValueError("reference_format_requires_rebuild")


def _insert_st(connection: SqliteConnection, evidence: tuple[HistoryStEvidence, ...]) -> None:
    connection.run_many(
        "INSERT OR IGNORE INTO security_st_evidence VALUES(?,?,?,?,?)",
        (
            (
                EncodeHash(CanonicalDigest(item)),
                item.code,
                EncodeDate(item.checked_on),
                item.status,
                EncodeHash(CanonicalDigest(item)),
            )
            for item in evidence
        ),
    )


def _evidence(row: sqlite3.Row) -> HistoryStEvidence:
    item = HistoryStEvidence(row[0], DecodeDate(row[1]), cast(Literal["ever_st", "clear", "unknown"], row[2]))
    if CanonicalDigest(item) != DecodeHash(row[3]) or row[3] != row[4]:
        raise ValueError("reference_st_hash_conflict")
    return item


def _current_st(connection: SqliteConnection) -> tuple[HistoryStEvidence, ...]:
    evidence = tuple(
        _evidence(row)
        for row in connection.run(
            "SELECT e.code,e.checked_date,e.st_status,e.content_hash,e.st_id "
            "FROM security_st_current c JOIN security_st_evidence e ON c.code=e.code AND c.st_id=e.st_id ORDER BY c.code"
        )
    )
    if len(evidence) != connection.run("SELECT COUNT(*) FROM security_st_current").require_one()[0]:
        raise ValueError("reference_st_member_missing")
    return evidence


def _incoming_wins(
    previous: HistoryStEvidence, incoming: HistoryStEvidence, observed: HistoryStEvidence | None
) -> bool:
    if previous.status == "ever_st":
        return False
    if incoming.status == "ever_st" or incoming.checked_on > previous.checked_on:
        return True
    return incoming.checked_on == previous.checked_on and (previous == observed or incoming.status == "unknown")


def _read(connection: SqliteConnection, publication_id: str | None) -> PreparedReference | None:
    if publication_id is None:
        current = connection.run("SELECT publication_id FROM reference_current WHERE singleton=1").fetchone()
        if current is None:
            return None
        publication_id = current[0]
    row = connection.run(
        "SELECT content_hash,checked_at,calendar_id,industry_version,security_count "
        "FROM reference_publication WHERE publication_id=?",
        (publication_id,),
    ).fetchone()
    if row is None:
        raise ValueError("reference_publication_missing")
    checked_at = DecodeTime(row[1])
    securities = tuple(
        BaoStockSecurity(
            item[0],
            item[1],
            cast(BaoStockBoard, item[2]),
            DecodeDate(item[3]),
            DecodeDate(item[4]) if item[4] is not None else None,
            item[5],
        )
        for item in connection.run(
            "SELECT s.code,s.name,s.board,s.listed_date,s.delisted_date,s.source_version "
            "FROM reference_member m JOIN security s ON m.code=s.code AND m.security_id=s.security_id "
            "WHERE m.publication_id=? ORDER BY m.code",
            (publication_id,),
        )
    )
    members = connection.run(
        "SELECT m.security_id,s.content_hash FROM reference_member m JOIN security s USING(security_id) "
        "WHERE m.publication_id=? ORDER BY m.code",
        (publication_id,),
    ).fetchall()
    if (
        len(securities) != row[4]
        or tuple(EncodeHash(item.content_hash) for item in securities) != tuple(item[0] for item in members)
        or any(item[0] != item[1] for item in members)
    ):
        raise ValueError("reference_security_hash_conflict")
    source_evidence = tuple(
        _evidence(item)
        for item in connection.run(
            "SELECT e.code,e.checked_date,e.st_status,e.content_hash,e.st_id "
            "FROM reference_member m JOIN security_st_evidence e ON m.code=e.code AND m.st_id=e.st_id "
            "WHERE m.publication_id=? ORDER BY m.code",
            (publication_id,),
        )
    )
    if any(
        item.checked_on > checked_at.date() or item.status != "ever_st" and item.checked_on != checked_at.date()
        for item in source_evidence
    ):
        raise ValueError("reference_st_publication_stale")
    evidence = tuple(replace(item, checked_on=checked_at.date()) for item in source_evidence)
    dates = tuple(
        DecodeDate(item[0])
        for item in connection.run(
            "SELECT trade_date FROM trading_calendar WHERE calendar_id=? ORDER BY trade_date",
            (row[2],),
        )
    )
    calendar = connection.run(
        "SELECT content_hash,first_trade_date,last_trade_date,session_count FROM calendar_version WHERE calendar_id=?",
        (row[2],),
    ).fetchone()
    if (
        not dates
        or calendar is None
        or calendar[0] != row[2]
        or EncodeHash(CanonicalDigest(dates)) != row[2]
        or (EncodeDate(dates[0]), EncodeDate(dates[-1]), len(dates)) != tuple(calendar)[1:]
    ):
        raise ValueError("reference_calendar_invalid")
    reference = PreparedReference(securities, evidence, checked_at, dates, row[3])
    if reference.identity != publication_id or reference.identity != DecodeHash(row[0]):
        raise ValueError("reference_publication_hash_conflict")
    return reference


__all__ = ["SQLiteReferenceIndex"]
