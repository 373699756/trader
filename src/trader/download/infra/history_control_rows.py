"""Typed relational rows for history source, reference, checkpoint and snapshot identities."""

from __future__ import annotations

from typing import Literal, cast

from trader.download.domain.history_control import (
    HistoryCalendarIdentity,
    HistorySecurityIdentity,
    HistorySnapshot,
    HistorySnapshotPartition,
    HistorySourceIdentity,
    HistorySyncCheckpoint,
    HistorySyncState,
    HistoryUniverseIdentity,
)
from trader.download.domain.prepared_reference import PreparedReference
from trader.download.infra.reference_index import SQLiteReferenceIndex
from trader.infra.sqlite_codec import DecodeDate, DecodeHash, DecodeTime, EncodeDate, EncodeHash, EncodeTime
from trader.infra.sqlite_connection import SqliteConnection, SqliteSchema

ControlKind = Literal["source", "calendar", "universe", "checkpoint", "snapshot"]
ControlRecord = (
    HistorySourceIdentity | HistoryCalendarIdentity | HistoryUniverseIdentity | HistorySyncCheckpoint | HistorySnapshot
)
_TABLES = {
    "source": "source_version",
    "calendar": "calendar_binding",
    "universe": "universe_version",
    "checkpoint": "sync_checkpoint",
    "snapshot": "history_snapshot",
}

HISTORY_CONTROL_SCHEMA = SqliteSchema(
    2,
    (
        """CREATE TABLE schema_contract(
            singleton INTEGER PRIMARY KEY CHECK(singleton=1), schema_id TEXT NOT NULL,
            schema_version INTEGER NOT NULL, reference_file TEXT
        )""",
        """CREATE TABLE reference_binding(
            reference_hash BLOB PRIMARY KEY CHECK(length(reference_hash)=32), publication_id TEXT NOT NULL
        ) WITHOUT ROWID""",
        """CREATE TABLE history_industry(
            reference_hash BLOB NOT NULL REFERENCES reference_binding(reference_hash),
            code TEXT NOT NULL, start_date INTEGER NOT NULL, end_date INTEGER,
            industry TEXT NOT NULL, classification TEXT NOT NULL,
            PRIMARY KEY(reference_hash,code,start_date), CHECK(end_date IS NULL OR end_date>start_date)
        ) WITHOUT ROWID""",
        """CREATE TABLE source_version(
            id BLOB PRIMARY KEY CHECK(length(id)=32), source TEXT NOT NULL, dataset TEXT NOT NULL,
            supplier_contract TEXT NOT NULL, observed_at INTEGER NOT NULL
        ) WITHOUT ROWID""",
        """CREATE TABLE calendar_binding(
            id BLOB PRIMARY KEY CHECK(length(id)=32), source_hash BLOB NOT NULL REFERENCES source_version(id),
            start_date INTEGER NOT NULL, end_date INTEGER NOT NULL, session_count INTEGER NOT NULL CHECK(session_count>0), publication_id TEXT NOT NULL
        ) WITHOUT ROWID""",
        """CREATE TABLE universe_version(
            id BLOB PRIMARY KEY CHECK(length(id)=32), source_hash BLOB NOT NULL REFERENCES source_version(id),
            security_count INTEGER NOT NULL CHECK(security_count>0), publication_id TEXT NOT NULL
        ) WITHOUT ROWID""",
        """CREATE TABLE sync_checkpoint(
            id TEXT PRIMARY KEY, content_hash BLOB NOT NULL CHECK(length(content_hash)=32),
            sync_id TEXT NOT NULL, ordinal INTEGER NOT NULL CHECK(ordinal>0),
            state TEXT NOT NULL CHECK(state IN ('pending','running','completed','failed','cancelled')),
            observed_at INTEGER NOT NULL, completed_units INTEGER NOT NULL,
            total_units INTEGER NOT NULL, failure_code TEXT,
            CHECK(completed_units>=0 AND total_units>=completed_units), UNIQUE(sync_id,ordinal)
        ) WITHOUT ROWID""",
        """CREATE TABLE history_snapshot(
            id INTEGER PRIMARY KEY CHECK(id>0), content_hash BLOB NOT NULL UNIQUE CHECK(length(content_hash)=32),
            data_cutoff_date INTEGER NOT NULL, label_cutoff_date INTEGER NOT NULL,
            calendar_hash BLOB NOT NULL REFERENCES calendar_binding(id),
            universe_hash BLOB NOT NULL REFERENCES universe_version(id),
            source_hash BLOB NOT NULL REFERENCES source_version(id),
            CHECK(label_cutoff_date<=data_cutoff_date)
        )""",
        """CREATE TABLE snapshot_partition(
            snapshot_id INTEGER NOT NULL REFERENCES history_snapshot(id), calendar_year INTEGER NOT NULL,
            calendar_month INTEGER NOT NULL CHECK(calendar_month BETWEEN 1 AND 12), relative_path TEXT NOT NULL,
            logical_hash BLOB NOT NULL CHECK(length(logical_hash)=32), row_count INTEGER NOT NULL CHECK(row_count>=0),
            sync_sequence INTEGER NOT NULL CHECK(sync_sequence>0), PRIMARY KEY(snapshot_id,calendar_year,calendar_month)
        ) WITHOUT ROWID""",
        """CREATE TABLE published_snapshot(
            singleton INTEGER PRIMARY KEY CHECK(singleton=1), snapshot_hash BLOB NOT NULL,
            sync_sequence INTEGER NOT NULL REFERENCES history_snapshot(id),
            FOREIGN KEY(snapshot_hash) REFERENCES history_snapshot(content_hash)
        )""",
    ),
)


def ControlKey(value: ControlRecord) -> tuple[ControlKind, str]:
    if isinstance(value, HistorySourceIdentity):
        return "source", value.content_hash
    if isinstance(value, HistoryCalendarIdentity):
        return "calendar", value.content_hash
    if isinstance(value, HistoryUniverseIdentity):
        return "universe", value.content_hash
    if isinstance(value, HistorySyncCheckpoint):
        return "checkpoint", f"{value.sync_identity}:{value.ordinal}"
    return "snapshot", str(value.sequence)


def ControlKeys(connection: SqliteConnection, kind: ControlKind) -> tuple[str, ...]:
    rows = connection.run(f"SELECT id FROM {_TABLES[kind]} ORDER BY id")  # noqa: S608 - fixed table names
    return tuple(DecodeHash(row[0]) if kind in {"source", "calendar", "universe"} else str(row[0]) for row in rows)


def _reference(
    index: SQLiteReferenceIndex | None,
    publication_id: str | None,
    references: dict[str, PreparedReference] | None = None,
) -> PreparedReference:
    if index is None or publication_id is None:
        raise ValueError("history_reference_publication_missing")
    if references is not None and publication_id in references:
        return references[publication_id]
    reference = index.read(publication_id)
    if reference is None:
        raise ValueError("history_reference_missing")
    if references is not None:
        references[publication_id] = reference
    return reference


def ReadControlRecord(
    connection: SqliteConnection,
    kind: ControlKind,
    key: str,
    index: SQLiteReferenceIndex | None,
    references: dict[str, PreparedReference] | None = None,
) -> ControlRecord | None:
    record_key = (
        EncodeHash(key) if kind in {"source", "calendar", "universe"} else int(key) if kind == "snapshot" else key
    )
    row = connection.run(f"SELECT * FROM {_TABLES[kind]} WHERE id=?", (record_key,)).fetchone()  # noqa: S608
    if row is None:
        return None
    value: ControlRecord
    if kind == "source":
        value = HistorySourceIdentity(row[1], row[2], row[3], DecodeTime(row[4]))
        digest = DecodeHash(row[0])
    elif kind == "calendar":
        source_hash = DecodeHash(row[1])
        reference = _reference(index, row[5], references)
        start, end = DecodeDate(row[2]), DecodeDate(row[3])
        dates = tuple(day for day in reference.open_dates if start <= day <= end)
        if len(dates) != row[4]:
            raise ValueError("history_calendar_binding_invalid")
        value = HistoryCalendarIdentity(dates, source_hash)
        digest = DecodeHash(row[0])
    elif kind == "universe":
        source_hash = DecodeHash(row[1])
        reference = _reference(index, row[3], references)
        securities = tuple(
            HistorySecurityIdentity(item.code, item.name, item.board, item.listed_on, item.delisted_on)
            for item in reference.eligible_universe
        )
        if len(securities) != row[2]:
            raise ValueError("history_universe_binding_invalid")
        value = HistoryUniverseIdentity(securities, source_hash)
        digest = DecodeHash(row[0])
    elif kind == "checkpoint":
        value = HistorySyncCheckpoint(
            row[2],
            row[3],
            cast(HistorySyncState, row[4]),
            DecodeTime(row[5]),
            row[6],
            row[7],
            row[8],
        )
        digest = DecodeHash(row[1])
    else:
        partitions = tuple(
            HistorySnapshotPartition(item[0], DecodeHash(item[1]), item[2], item[3], item[4])
            for item in connection.run(
                "SELECT relative_path,logical_hash,row_count,calendar_month,sync_sequence FROM snapshot_partition "
                "WHERE snapshot_id=? ORDER BY calendar_year,calendar_month",
                (row[0],),
            )
        )
        value = HistorySnapshot(
            row[0],
            DecodeDate(row[2]),
            DecodeDate(row[3]),
            DecodeHash(row[4]),
            DecodeHash(row[5]),
            DecodeHash(row[6]),
            partitions,
        )
        digest = DecodeHash(row[1])
    if value.content_hash != digest or ControlKey(value) != (kind, key):
        raise ValueError("history_control_hash_conflict")
    return value


def WriteControlRecord(
    connection: SqliteConnection,
    value: ControlRecord,
    index: SQLiteReferenceIndex | None,
    publication_id: str | None = None,
    *,
    references: dict[str, PreparedReference] | None = None,
) -> None:
    kind, key = ControlKey(value)
    previous = ReadControlRecord(connection, kind, key, index, references)
    if previous is not None:
        if previous != value:
            raise ValueError("history_control_identity_conflict")
        return
    digest = EncodeHash(value.content_hash)
    if isinstance(value, HistorySourceIdentity):
        connection.run(
            "INSERT INTO source_version VALUES(?,?,?,?,?)",
            (digest, value.source, value.dataset, value.supplier_contract, EncodeTime(value.observed_at)),
        )
    elif isinstance(value, HistoryCalendarIdentity):
        reference = _reference(index, publication_id, references)
        selected = tuple(day for day in reference.open_dates if value.open_dates[0] <= day <= value.open_dates[-1])
        if selected != value.open_dates:
            raise ValueError("history_calendar_binding_invalid")
        connection.run(
            "INSERT INTO calendar_binding VALUES(?,?,?,?,?,?)",
            (
                digest,
                EncodeHash(value.source_identity_hash),
                EncodeDate(selected[0]),
                EncodeDate(selected[-1]),
                len(selected),
                publication_id,
            ),
        )
    elif isinstance(value, HistoryUniverseIdentity):
        reference = _reference(index, publication_id, references)
        expected = tuple(
            HistorySecurityIdentity(item.code, item.name, item.board, item.listed_on, item.delisted_on)
            for item in reference.eligible_universe
        )
        if value.securities != expected:
            raise ValueError("history_universe_binding_invalid")
        connection.run(
            "INSERT INTO universe_version VALUES(?,?,?,?)",
            (digest, EncodeHash(value.source_identity_hash), len(expected), publication_id),
        )
    elif isinstance(value, HistorySyncCheckpoint):
        connection.run(
            "INSERT INTO sync_checkpoint VALUES(?,?,?,?,?,?,?,?,?)",
            (
                key,
                digest,
                value.sync_identity,
                value.ordinal,
                value.state,
                EncodeTime(value.observed_at),
                value.completed_units,
                value.total_units,
                value.error_code,
            ),
        )
    else:
        connection.run(
            "INSERT INTO history_snapshot VALUES(?,?,?,?,?,?,?)",
            (
                value.sequence,
                digest,
                EncodeDate(value.data_cutoff),
                EncodeDate(value.label_cutoff),
                EncodeHash(value.calendar_hash),
                EncodeHash(value.universe_hash),
                EncodeHash(value.source_identity_hash),
            ),
        )
        connection.run_many(
            "INSERT INTO snapshot_partition VALUES(?,?,?,?,?,?,?)",
            (
                (
                    value.sequence,
                    item.calendar_year,
                    item.calendar_month,
                    item.relative_path,
                    EncodeHash(item.logical_hash),
                    item.row_count,
                    item.sequence,
                )
                for item in value.partitions
            ),
        )


__all__ = [
    "ControlKind",
    "ControlRecord",
    "ControlKey",
    "ControlKeys",
    "HISTORY_CONTROL_SCHEMA",
    "ReadControlRecord",
    "WriteControlRecord",
]
