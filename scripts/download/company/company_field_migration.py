"""Materialize field conclusions on a new external company database copy."""

from __future__ import annotations

import hashlib
import math
import sqlite3
import time
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from scripts.core.paths import PROJECT_ROOT
from scripts.download.company.output_paths import RequireIsolatedReplay
from trader.download.domain.company_events import CompanyEventPolicy
from trader.download.domain.company_facts import CompanyFacts
from trader.download.domain.company_field_plan import BuildCompanyFieldResults
from trader.download.domain.company_field_results import CompanyFieldConclusion, CompanyFieldConclusionState, CompanyFieldName
from trader.download.infra.company_fact_rows import ReadCompanyFactRows
from trader.download.infra.company_facts_protocol import CompanyExtractionHash, CompanyInteger, RequireCompanyTime
from trader.download.infra.company_facts_reader import SQLiteCompanyEvidenceReader
from trader.download.infra.company_facts_schema import COMPANY_FACTS_SCHEMA
from trader.download.infra.company_field_result_rows import ReadCompanyFieldResults, WriteCompanyFieldResults
from trader.infra.atomic_files import AtomicWriteJson
from trader.infra.serialization.canonical_codec import CanonicalDigest, CanonicalText
from trader.infra.sqlite_connection import (
    EnsureSqliteSchema, SqliteConnection, SqliteConnectionOptions, SqliteSession, SqliteTransaction,
)
from trader.infra.sqlite_policy import SqliteIoPolicy
from trader.infra.sqlite_publication import PublishSqliteFile, VerifySqliteFile


_BUSINESS_TABLES = (
    "company_facts", "company_input_receipt", "company_evidence_ref", "company_risk_event", "company_reduction_plan",
    "company_positive_event", "company_positive_milestone", "company_unlock", "company_review",
    "company_risk_evidence", "company_positive_evidence", "company_exclusion",
)


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise TimeoutError("company_field_migration_deadline_exceeded")


def _reject_links(path: Path) -> None:
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("company_field_migration_symlink_rejected")


def _prepare_paths(source_path: Path, directory: Path, protected_roots: Sequence[Path]) -> tuple[Path, Path]:
    source = source_path.expanduser()
    destination = directory.expanduser()
    if not source.is_absolute() or not destination.is_absolute():
        raise ValueError("company_field_migration_absolute_paths_required")
    _reject_links(source)
    _reject_links(destination)
    source = source.resolve(strict=True)
    if not source.is_file():
        raise ValueError("company_field_migration_source_file_required")
    destination = destination.resolve()
    RequireIsolatedReplay(destination, (PROJECT_ROOT, source, *protected_roots))
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError("company_field_migration_new_directory_required")
    destination.mkdir(parents=True, exist_ok=True)
    return source, destination


def _file_digest(path: Path, deadline: float) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            _check_deadline(deadline)
            digest.update(block)
    _check_deadline(deadline)
    return digest.hexdigest()


def _source_files(path: Path, deadline: float) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for name, candidate in (("database", path), ("wal", Path(f"{path}-wal")), ("receipt", Path(f"{path}.sha256"))):
        if candidate.is_symlink():
            raise ValueError("company_field_migration_source_symlink_rejected")
        result[name] = _file_digest(candidate, deadline) if candidate.exists() else None
    return result


def _integrity(connection: SqliteConnection, deadline: float) -> None:
    _check_deadline(deadline)
    if tuple(row[0] for row in connection.run("PRAGMA integrity_check")) != ("ok",):
        raise ValueError("company_field_migration_integrity_failed")
    if connection.run("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("company_field_migration_foreign_keys_failed")


def _source_identity(connection: SqliteConnection) -> tuple[bytes, bytes, int]:
    tables = {row[0] for row in connection.run("SELECT name FROM sqlite_master WHERE type='table' AND name NOT GLOB 'sqlite_*'")}
    if tables != {*_BUSINESS_TABLES, "company_publication"}:
        raise ValueError("company_field_migration_source_tables_unsupported")
    rows = connection.run(
        "SELECT singleton,extraction_hash,content_hash,published_at FROM company_publication"
    ).fetchall()
    if len(rows) != 1 or rows[0][0] != 1:
        raise ValueError("company_field_migration_source_publication_invalid")
    row = rows[0]
    if any(not isinstance(value, bytes) or len(value) != 32 for value in row[1:3]):
        raise ValueError("company_field_migration_source_publication_invalid")
    published_at = CompanyInteger(row[3])
    RequireCompanyTime(published_at)
    return cast(bytes, row[1]), cast(bytes, row[2]), published_at


def _columns(connection: SqliteConnection, table: str) -> tuple[tuple[str, str, int, int], ...]:
    return tuple((row[1], row[2], row[3], row[5]) for row in connection.run(f'PRAGMA table_info("{table}")'))


def _ordered_table(connection: SqliteConnection, table: str) -> str:
    columns = _columns(connection, table)
    keys = tuple(column[0] for column in sorted(columns, key=lambda column: column[3]) if column[3])
    if not keys:
        raise ValueError("company_field_migration_table_key_missing")
    return f'SELECT * FROM "{table}" ORDER BY ' + ",".join(f'"{key}"' for key in keys)


def _table_fingerprint(connection: SqliteConnection, table: str, deadline: float) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    rows = connection.run(_ordered_table(connection, table))
    while batch := rows.fetch_values(256):
        _check_deadline(deadline)
        for row in batch:
            # Both sides are decoded in the same process. Typed SQLite tuple
            # repr preserves blobs, NULLs, integers and reals without publishing values.
            digest.update(repr(row).encode("utf-8"))
            digest.update(b"\n")
        count += len(batch)
    return count, digest.hexdigest()


def _copy_tables(source: SqliteConnection, target: SqliteConnection, deadline: float) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in _BUSINESS_TABLES:
        _check_deadline(deadline)
        columns = _columns(source, table)
        if not columns or columns != _columns(target, table):
            raise ValueError("company_field_migration_source_columns_unsupported")
        rows = source.run(_ordered_table(source, table))
        count = 0
        statement = f'INSERT INTO "{table}" VALUES({",".join("?" for _ in columns)})'
        while batch := rows.fetch_values(256):
            _check_deadline(deadline)
            target.run_many(statement, batch)
            count += len(batch)
        counts[table] = count
    return counts


def _baseline_fields(
    connection: SqliteConnection, codes: tuple[str, ...], deadline: float,
) -> tuple[dict[str, int], str]:
    counts: Counter[str] = Counter()
    digest = hashlib.sha256()
    for offset in range(0, len(codes), 128):
        _check_deadline(deadline)
        for fact in ReadCompanyFactRows(connection, codes[offset:offset + 128]):
            conclusions = tuple(CompanyFieldConclusion(
                item.code, item.event_id, cast(CompanyFieldName, item.field), item.value,
                cast(CompanyFieldConclusionState, item.state), item.applicable, item.reason, (),
            ) for item in BuildCompanyFieldResults(fact))
            if any(item.state in {"verified", "not_disclosed"} for item in conclusions):
                raise ValueError("company_field_migration_baseline_overclaims")
            WriteCompanyFieldResults(connection, fact.code, conclusions)
            digest.update(CanonicalText(conclusions).encode("utf-8"))
            counts.update(item.state for item in conclusions)
    return dict(sorted(counts.items())), digest.hexdigest()


def _write_report(path: Path, report: dict[str, object]) -> None:
    if path.exists() or path.is_symlink():
        raise ValueError("company_field_migration_report_exists")
    AtomicWriteJson(path, report, indent=2)


def AuditCompanyFieldMigration(
    source_path: Path, directory: Path, *, budget_seconds: float, sqlite_policy: SqliteIoPolicy,
    event_policy: CompanyEventPolicy, protected_roots: Sequence[Path],
) -> dict[str, object]:
    """Preserve the source, materialize field conclusions and never authorize production.

    Invalid output paths fail before creating artifacts. Operational failures
    keep the backup/target and a bounded failure report for the next review.
    """
    if isinstance(budget_seconds, bool) or not isinstance(budget_seconds, (int, float)) or not math.isfinite(budget_seconds) or not 0 < budget_seconds <= 10800:
        raise ValueError("company_field_migration_budget_invalid")
    deadline = time.monotonic() + budget_seconds
    source_path, directory = _prepare_paths(source_path, directory, protected_roots)
    backup_path = directory / "source.sqlite3"
    target_path = directory / "company_facts.sqlite3"
    report_path = directory / "field-migration.json"
    report: dict[str, object] = {
        "schema_version": "company-field-migration", "status": "failed", "read_only_source": True,
        "isolated_copy": True, "production_authorized": False,
        "source_data_writes": 0, "field_correctness_verified": False,
        "baseline_is_not_field_verification": True, "target_path": str(target_path),
    }
    cancelled = lambda: time.monotonic() >= deadline
    try:
        verified_source = VerifySqliteFile(
            source_path, sqlite_policy=sqlite_policy, check=lambda: _check_deadline(deadline),
        )
        report["source_file_publication_verified"] = True
        before = _source_files(source_path, deadline)
        for path in (backup_path, target_path):
            with path.open("xb"):
                pass
        with SqliteSession(
            source_path, SqliteConnectionOptions(read_only=True), sqlite_policy=sqlite_policy, cancel_requested=cancelled,
        ) as source, SqliteSession(backup_path, sqlite_policy=sqlite_policy, cancel_requested=cancelled) as backup:
            data_version = CompanyInteger(source.run("PRAGMA data_version").require_one()[0])
            source.backup_to(backup, deadline=deadline, monotonic=time.monotonic)
            if CompanyInteger(source.run("PRAGMA data_version").require_one()[0]) != data_version:
                report["source_changed_during_backup"] = True
                raise ValueError("company_field_migration_source_changed_during_backup")
            current_source = VerifySqliteFile(
                source_path, sqlite_policy=sqlite_policy, previous=verified_source,
                check=lambda: _check_deadline(deadline),
            )
            if current_source != verified_source or before != _source_files(source_path, deadline):
                report["source_changed_during_backup"] = True
                raise ValueError("company_field_migration_source_changed_during_backup")
            report["source_backup_generation_verified"] = True
        snapshot_hash = _file_digest(backup_path, deadline)
        with SqliteSession(
            backup_path, SqliteConnectionOptions(read_only=True), sqlite_policy=sqlite_policy, cancel_requested=cancelled,
        ) as source, SqliteSession(
            target_path, SqliteConnectionOptions(foreign_keys=True), sqlite_policy=sqlite_policy, cancel_requested=cancelled,
        ) as target:
            _integrity(source, deadline)
            extraction_hash, content_hash, published_at = _source_identity(source)
            report.update({"source_publication_hash": content_hash.hex(), "source_extraction_hash": extraction_hash.hex(),
                           "source_published_at": RequireCompanyTime(published_at).isoformat(), "source_snapshot_sha256": snapshot_hash})
            if extraction_hash != CompanyExtractionHash(event_policy):
                raise ValueError("company_field_migration_reader_policy_mismatch")
            with SqliteTransaction(target, immediate=True):
                EnsureSqliteSchema(target, COMPANY_FACTS_SCHEMA, allow_create=True)
                counts = _copy_tables(source, target, deadline)
                codes = tuple(row[0] for row in target.run("SELECT code FROM company_facts ORDER BY code"))
                states, fields_hash = _baseline_fields(target, codes, deadline)
                migrated_hash = CanonicalDigest((
                    "company-field-conclusions", content_hash.hex(), extraction_hash.hex(), published_at, fields_hash,
                ))
                target.run("INSERT INTO company_publication(singleton,extraction_hash,content_hash,published_at) VALUES(1,?,?,?)",
                           (extraction_hash, bytes.fromhex(migrated_hash), published_at))
                _integrity(target, deadline)
                for table in _BUSINESS_TABLES:
                    if _table_fingerprint(source, table, deadline) != _table_fingerprint(target, table, deadline):
                        raise ValueError("company_field_migration_business_rows_changed")
                for offset in range(0, len(codes), 128):
                    _check_deadline(deadline)
                    ReadCompanyFieldResults(target, codes[offset:offset + 128])
        PublishSqliteFile(target_path, sqlite_policy=sqlite_policy, check=lambda: _check_deadline(deadline))
        reader = SQLiteCompanyEvidenceReader(target_path, event_policy=event_policy, sqlite_policy=sqlite_policy)
        if reader.read_fields((), cancel_requested=cancelled).publication_hash != migrated_hash:
            raise ValueError("company_field_migration_reader_inconsistent")
        field_count = 0
        for offset in range(0, len(codes), 128):
            _check_deadline(deadline)
            batch = codes[offset:offset + 128]
            captured: list[CompanyFacts] = []
            reader.visit(batch, captured.append, cancel_requested=cancelled)
            snapshot = reader.read_fields(batch, cancel_requested=cancelled)
            if len(captured) != len(batch) or snapshot.publication_hash != migrated_hash:
                raise ValueError("company_field_migration_reader_inconsistent")
            field_count += len(snapshot.results)
        _check_deadline(deadline)
        after = _source_files(source_path, deadline)
        unchanged = before == after
        report.update({
            "status": "passed" if unchanged else "degraded", "source_input_unchanged": unchanged,
            "source_files_sha256_before": before, "source_files_sha256_after": after,
            "business_data_unchanged": True, "original_receipts_unchanged": True,
            "preserved_business_tables": counts, "field_states": states, "fields": field_count,
            "target_publication_hash": migrated_hash, "target_reader_verified": True,
            "source_snapshot_path": str(backup_path), "target_integrity_verified": True, "target_foreign_keys_verified": True,
        })
    except (OSError, sqlite3.Error, ValueError, RuntimeError, TimeoutError, InterruptedError) as error:
        report["failure_category"] = type(error).__name__
        reason = str(error)
        report["failure_code"] = reason if (
            reason.startswith(("company_field_migration_", "sqlite_publication_")) and len(reason) <= 100
            and all(character in "abcdefghijklmnopqrstuvwxyz0123456789_" for character in reason)
        ) else type(error).__name__
        report["deadline_exceeded"] = time.monotonic() >= deadline
    _write_report(report_path, report)
    return report


__all__ = ["AuditCompanyFieldMigration"]
