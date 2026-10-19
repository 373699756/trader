"""Explicit legacy-format decoding for isolated data migration; never an active read path."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from typing import Literal, cast

from trader.download.domain.history_reference import HistoryStEvidence
from trader.download.infra.history_reference_codec import WriteStEvidence
from trader.infra.sqlite_connection import (
    CheckSqliteIntegrity,
    OpenSqliteConnection,
    SqliteConnection,
    SqliteConnectionOptions,
    SqliteTransaction,
)
from trader.recommendation.application.ports.decision_records import DecisionCheckpoint
from trader.recommendation.domain.market.eligibility import (
    IssuerEligibilityFact,
    IssuerEligibilityFactHash,
    IssuerEligibilityReason,
)
from trader.recommendation.infra.persistence.decision_record_codec import CommittedRecordFromBytes
from trader.recommendation.infra.persistence.decision_records import SQLiteDecisionRecords
from trader.recommendation.infra.persistence.issuer_eligibility import SQLiteIssuerEligibilityIndex
from trader.training.infra.research.trace_storage import InitializeDatabase
from trader.training.infra.research.trace_codec import DEFAULT_DECODED_PAYLOAD_BYTES, DecodedPayloadBytes

_READ = SqliteConnectionOptions(read_only=True, immutable=True)


def CopyFile(source: Path, target: Path) -> None:
    if not source.is_file():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if FileDigest(source) != FileDigest(target):
            raise ValueError("migration_file_identity_conflict")
        return
    shutil.copyfile(source, target)
    if FileDigest(source) != FileDigest(target):
        raise ValueError("migration_copy_digest_mismatch")


def FileDigest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def ValidateDatabase(path: Path) -> None:
    for suffix in ("-wal", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise ValueError("migration_requires_stopped_checkpointed_source")
    with closing(OpenSqliteConnection(path, _READ)) as connection:
        CheckSqliteIntegrity(connection)
        if connection.run("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("migration_foreign_key_invalid")


def MergeDatabase(source: Path, target: Path) -> None:
    """Union unchanged table formats; duplicate keys must have identical entire rows."""
    if not source.is_file():
        return
    ValidateDatabase(source)
    if not target.exists():
        CopyFile(source, target)
        return
    with closing(OpenSqliteConnection(source, _READ)) as reader, closing(OpenSqliteConnection(target)) as writer:
        source_schema = tuple(
            tuple(row)
            for row in reader.run(
                "SELECT type,name,sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' ORDER BY type,name"
            )
        )
        target_schema = tuple(
            tuple(row)
            for row in writer.run(
                "SELECT type,name,sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' ORDER BY type,name"
            )
        )
        if source_schema != target_schema:
            raise ValueError("migration_database_schema_conflict")
        with SqliteTransaction(writer, immediate=True):
            for kind, table, _sql in source_schema:
                if kind == "table":
                    _merge_rows(reader, writer, str(table))
        CheckSqliteIntegrity(writer)


def _identifier(value: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) is None:
        raise ValueError("migration_identifier_invalid")
    return '"' + value + '"'


def _merge_rows(reader: SqliteConnection, writer: SqliteConnection, table: str) -> None:
    name = _identifier(table)
    columns = tuple(str(row[1]) for row in reader.run(f"PRAGMA table_info({name})"))
    for row in reader.run(f"SELECT * FROM {name}"):
        _insert_exact(writer, table, columns, tuple(row))


def _insert_exact(connection: SqliteConnection, table: str, columns: tuple[str, ...], row: tuple[object, ...]) -> None:
    name = _identifier(table)
    names = ",".join(_identifier(column) for column in columns)
    cursor = connection.run(f"INSERT OR IGNORE INTO {name} ({names}) VALUES ({','.join('?' for _ in row)})", row)
    if cursor.rowcount == 0:
        where = " AND ".join(f"{_identifier(column)} IS ?" for column in columns)
        if connection.run(f"SELECT 1 FROM {name} WHERE {where}", row).fetchone() is None:
            raise ValueError("migration_database_identity_conflict")


def ImportDecisions(database: Path, target: Path) -> None:
    if not database.is_file():
        return
    ValidateDatabase(database)
    records = SQLiteDecisionRecords(target)
    records.initialize()
    with closing(OpenSqliteConnection(database, _READ)) as connection:
        for row in connection.run("SELECT * FROM decision_records ORDER BY trade_date,strategy"):
            if row["status"] != "committed":
                raise ValueError("migration_requires_recovered_formal_decisions")
            payload = _verified_payload(database.parent, row)
            record = CommittedRecordFromBytes(payload)
            if (
                record.strategy.value,
                record.trade_date.isoformat(),
                record.version,
                record.payload_hash,
                record.committed_at.isoformat(),
                record.commit_kind,
            ) != tuple(
                row[key] for key in ("strategy", "trade_date", "version", "payload_hash", "committed_at", "commit_kind")
            ):
                raise ValueError("migration_decision_identity_invalid")
            records.import_record_bytes(payload)
        for row in connection.run("SELECT * FROM decision_checkpoints ORDER BY trade_date,strategy"):
            payload = _verified_payload(database.parent, row)
            envelope = CommittedRecordFromBytes(payload)
            checkpoint = DecisionCheckpoint(envelope.decision, envelope.committed_at)
            if (
                checkpoint.version,
                checkpoint.decision.version,
                checkpoint.boundary_at.isoformat(),
                checkpoint.decision.observed_at.isoformat(),
                checkpoint.decision.strategy.value,
                checkpoint.decision.trade_date.isoformat(),
            ) != tuple(
                row[key]
                for key in ("version", "decision_version", "boundary_at", "observed_at", "strategy", "trade_date")
            ):
                raise ValueError("migration_checkpoint_identity_invalid")
            records.import_checkpoint_bytes(payload)
            if row["consumed_at"] is not None:
                records.consume_checkpoint(checkpoint, consumed_at=datetime.fromisoformat(row["consumed_at"]))
        known = {
            str(row[0])
            for row in connection.run(
                "SELECT relative_path FROM decision_records UNION SELECT relative_path FROM decision_checkpoints"
            )
        }
        for path in database.parent.rglob("*.json"):
            if path.relative_to(database.parent).as_posix() not in known:
                raise ValueError("migration_unindexed_decision_evidence")
    # Verify exact persisted bytes, independently of the decoder's in-memory representation.
    with (
        closing(OpenSqliteConnection(database, _READ)) as reader,
        closing(OpenSqliteConnection(target, SqliteConnectionOptions(read_only=True))) as writer,
    ):
        for table in ("decision_records", "decision_checkpoints"):
            for row in reader.run(f"SELECT * FROM {table}"):
                actual = writer.run(
                    f"SELECT payload,payload_sha256 FROM {table} WHERE strategy=? AND trade_date=?",
                    (row["strategy"], row["trade_date"]),
                ).require_one()
                if tuple(actual) != (_verified_payload(database.parent, row), row["payload_sha256"]):
                    raise ValueError("migration_decision_bytes_changed")


def _verified_payload(root: Path, row: sqlite3.Row) -> bytes:
    relative = Path(row["relative_path"])
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("migration_manifest_path_invalid")
    payload = (root / relative).read_bytes()
    if hashlib.sha256(payload).hexdigest() != row["payload_sha256"]:
        raise ValueError("migration_payload_checksum_invalid")
    return payload


def ImportEligibility(reference: Path, legacy: Path, target: Path) -> None:
    manifest_path = reference / "active-manifest.json"
    manifest: dict[str, object] | None = None
    databases: tuple[Path, ...] = ()
    if manifest_path.exists():
        raw = json.loads(manifest_path.read_text())
        if not isinstance(raw, dict) or raw.get("schema_version") != "issuer_eligibility_snapshot":
            raise ValueError("migration_eligibility_manifest_invalid")
        manifest = raw
        snapshot = str(raw["snapshot_id"])
        if Path(snapshot).name != snapshot or snapshot in (".", ".."):
            raise ValueError("migration_eligibility_snapshot_invalid")
        databases = tuple(sorted((reference / "snapshots" / snapshot).glob("*.sqlite3")))
        if not databases and raw.get("fact_count") != 0:
            raise ValueError("migration_eligibility_snapshot_missing")
    if legacy.is_file():
        databases += (legacy,)
    facts: dict[tuple[str, IssuerEligibilityReason, str], IssuerEligibilityFact] = {}
    for database in databases:
        ValidateDatabase(database)
        with closing(OpenSqliteConnection(database, _READ)) as connection:
            for row in connection.run(
                "SELECT code,reason,effective_at,evidence_id,source,evidence_hash,content_hash FROM issuer_eligibility_facts"
            ):
                fact = IssuerEligibilityFact(
                    row[0], IssuerEligibilityReason(row[1]), datetime.fromisoformat(row[2]), row[3], row[4], row[5]
                )
                if IssuerEligibilityFactHash(fact) != row[6] or facts.setdefault(fact.identity, fact) != fact:
                    raise ValueError("migration_eligibility_fact_conflict")
    ordered = tuple(sorted(facts.values()))
    digest = hashlib.sha256("\n".join(IssuerEligibilityFactHash(fact) for fact in ordered).encode("ascii")).hexdigest()
    if manifest is not None and (manifest.get("fact_count"), manifest.get("manifest_hash")) != (len(ordered), digest):
        raise ValueError("migration_eligibility_publication_conflict")
    if not ordered and manifest is None:
        return
    index = SQLiteIssuerEligibilityIndex(target)
    index.initialize()
    index.record(ordered)
    if index.facts() != ordered or index.status().manifest_hash != digest:
        raise ValueError("migration_eligibility_verification_failed")
    if manifest is not None:
        with closing(OpenSqliteConnection(target)) as connection, SqliteTransaction(connection, immediate=True):
            connection.run(
                "UPDATE publication SET refresh_state=?,last_refresh_at=?,next_refresh_at=?,refresh_failure_count=?,last_source=? WHERE singleton=1",
                (
                    manifest.get("refresh_state", "not_ready"),
                    manifest.get("last_refresh_at"),
                    manifest.get("next_refresh_at"),
                    manifest.get("refresh_failure_count", 0),
                    manifest.get("last_source", "incremental"),
                ),
            )


def ImportSt(source: Path, target: Path) -> None:
    if not source.is_file():
        return
    raw = json.loads(source.read_text())
    if not isinstance(raw, dict):
        raise ValueError("migration_st_invalid")
    evidence: list[HistoryStEvidence] = []
    for code, value in raw.items():
        if not isinstance(value, list) or len(value) != 2 or value[1] not in ("clear", "unknown", "ever_st"):
            raise ValueError("migration_st_row_invalid")
        evidence.append(
            HistoryStEvidence(
                code, date.fromisoformat(value[0]), cast(Literal["clear", "unknown", "ever_st"], value[1])
            )
        )
    WriteStEvidence(target, tuple(evidence))


def ImportSequence(source: Path, target: Path) -> None:
    if not source.is_file():
        return
    ValidateDatabase(source)
    with closing(OpenSqliteConnection(source, _READ)) as reader:
        reserved = reader.run("SELECT reserved_until FROM publication_sequence WHERE singleton=1").require_one()[0]
    if type(reserved) is not int or not 0 <= reserved < 2**63:
        raise ValueError("migration_sequence_invalid")
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(OpenSqliteConnection(target)) as writer, SqliteTransaction(writer, immediate=True):
        writer.run(
            "CREATE TABLE IF NOT EXISTS publication_sequence (singleton INTEGER PRIMARY KEY CHECK(singleton=1), reserved_until INTEGER NOT NULL)"
        )
        writer.run(
            "INSERT INTO publication_sequence VALUES (1,?) ON CONFLICT(singleton) DO UPDATE SET reserved_until=MAX(reserved_until,excluded.reserved_until)",
            (reserved,),
        )


def ImportTraces(source: Path, target: Path) -> None:
    """Partition existing canonical rows by their original trade date, retaining quarantine bytes."""
    if not source.is_file():
        return
    ValidateDatabase(source)
    initialized: set[Path] = set()
    with closing(OpenSqliteConnection(source, _READ)) as reader:
        for table in ("committed_events", "committed_event_quarantine"):
            columns = tuple(str(row[1]) for row in reader.run(f"PRAGMA table_info({table})"))
            for row in reader.run(f"SELECT * FROM {table}"):
                if table == "committed_events":
                    day = date.fromisoformat(row["trade_date"])
                else:
                    # Quarantine has no date column. Its unchanged JSON envelope must supply the partition.
                    envelope = json.loads(
                        DecodedPayloadBytes(bytes(row["payload"]), maximum_bytes=DEFAULT_DECODED_PAYLOAD_BYTES)
                    )
                    day = date.fromisoformat(envelope["trade_date"])
                path = target / f"{day.isoformat()}.sqlite3"
                InitializeDatabase(path, initialized)
                with closing(OpenSqliteConnection(path)) as writer, SqliteTransaction(writer, immediate=True):
                    _insert_exact(writer, table, columns, tuple(row))


__all__ = [
    "CopyFile",
    "FileDigest",
    "ValidateDatabase",
    "MergeDatabase",
    "ImportDecisions",
    "ImportEligibility",
    "ImportSt",
    "ImportSequence",
    "ImportTraces",
]
