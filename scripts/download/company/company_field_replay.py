"""Replay local company field publication on new external SQLite and text copies."""

from __future__ import annotations

import gzip
import hashlib
import math
import sqlite3
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from scripts.core.paths import PROJECT_ROOT
from scripts.download.company.output_paths import RequireIsolatedReplay
from trader.download.domain.company_events import CompanyEventPolicy, CompanyPositiveEvent, CompanyRiskEvent
from trader.download.domain.company_facts import CompanyFacts
from trader.download.domain.company_field_results import CompanyFieldSnapshot
from trader.download.infra.company_document_files import CompanyDocumentFiles
from trader.download.infra.company_evidence_index import SQLiteCompanyEvidenceIndex
from trader.download.infra.company_evidence_sql import SCHEMA
from trader.download.infra.company_facts_index import CompanyPublicationIdentity, ReadCompanyPublicationIdentity
from trader.download.infra.company_facts_reader import SQLiteCompanyEvidenceReader
from trader.download.infra.download_settings import LoadCompanySyncConfiguration
from trader.infra.atomic_files import AtomicWriteJson
from trader.infra.sqlite_connection import EnsureSqliteSchema, SqliteConnectionOptions, SqliteSession
from trader.infra.sqlite_policy import SqliteIoPolicy
from trader.infra.sqlite_publication import PublishSqliteFile


_MAX_TEXT_BYTES = 256 * 1024 * 1024


class _CountingDocuments(CompanyDocumentFiles):
    """Count actual document-owner reads, including initialization validation."""

    read_calls = 0

    def read(self, digest: bytes) -> str:
        self.read_calls += 1
        text = super().read(digest)
        if not isinstance(text, str):
            raise ValueError("company_field_replay_text_type_invalid")
        return text


@dataclass(frozen=True)
class _ReplayState:
    fields: CompanyFieldSnapshot
    publication: CompanyPublicationIdentity
    valid_receipts: tuple[tuple[object, ...], ...]
    attempted: int


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise TimeoutError("company_field_replay_deadline_exceeded")


def _reject_links(path: Path) -> None:
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("company_field_replay_symlink_rejected")


def _prepare_paths(
    collection_path: Path, facts_path: Path, directory: Path, protected_roots: Sequence[Path],
) -> tuple[Path, Path, Path]:
    paths = tuple(path.expanduser() for path in (collection_path, facts_path, directory))
    if any(not path.is_absolute() for path in paths):
        raise ValueError("company_field_replay_absolute_paths_required")
    for path in paths:
        _reject_links(path)
    collection, facts = (path.resolve(strict=True) for path in paths[:2])
    destination = paths[2].resolve()
    if not collection.is_file() or not facts.is_file() or collection == facts:
        raise ValueError("company_field_replay_source_pair_invalid")
    RequireIsolatedReplay(destination, (PROJECT_ROOT, collection.parent, facts.parent, *protected_roots))
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError("company_field_replay_new_directory_required")
    destination.mkdir(parents=True, exist_ok=True)
    return collection, facts, destination


def _file_digest(path: Path, deadline: float) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            _check_deadline(deadline)
            digest.update(block)
    _check_deadline(deadline)
    return digest.hexdigest()


def _source_files(path: Path, deadline: float) -> tuple[str | None, ...]:
    files = (path, Path(f"{path}-wal"), Path(f"{path}.sha256"))
    for item in files:
        _reject_links(item)
    return tuple(_file_digest(item, deadline) if item.exists() else None for item in files)


def _backup_collection(source_path: Path, target_path: Path, policy: SqliteIoPolicy, deadline: float) -> bool:
    target_path.parent.mkdir(parents=True)
    with target_path.open("xb"):
        pass
    with SqliteSession(
        source_path, SqliteConnectionOptions(read_only=True), sqlite_policy=policy,
        cancel_requested=lambda: time.monotonic() >= deadline,
    ) as source, SqliteSession(
        target_path, sqlite_policy=policy, cancel_requested=lambda: time.monotonic() >= deadline,
    ) as target:
        version = source.run("PRAGMA data_version").require_one()[0]
        EnsureSqliteSchema(source, SCHEMA)
        if tuple(row[0] for row in source.run("SELECT schema_id FROM contract")) != ("company_collection",):
            raise ValueError("company_field_replay_collection_contract_invalid")
        source.backup_to(target, deadline=deadline, monotonic=time.monotonic)
        stable = bool(version == source.run("PRAGMA data_version").require_one()[0])
        if tuple(row[0] for row in target.run("PRAGMA integrity_check")) != ("ok",):
            raise ValueError("company_field_replay_collection_integrity_failed")
        if target.run("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("company_field_replay_collection_foreign_keys_failed")
    return stable


def _backup_facts(
    source_path: Path, target_path: Path, policy: SqliteIoPolicy, event_policy: CompanyEventPolicy, deadline: float,
) -> bool:
    reader = SQLiteCompanyEvidenceReader(source_path, event_policy=event_policy, sqlite_policy=policy)
    cancelled = lambda: time.monotonic() >= deadline
    publication = reader.read_fields((), cancel_requested=cancelled).publication_hash
    if not publication:
        raise ValueError("company_field_replay_facts_publication_missing")
    target_path.parent.mkdir(parents=True)
    with target_path.open("xb"):
        pass
    with SqliteSession(
        source_path, SqliteConnectionOptions(read_only=True), sqlite_policy=policy, cancel_requested=cancelled,
    ) as source, SqliteSession(target_path, sqlite_policy=policy, cancel_requested=cancelled) as target:
        identity = ReadCompanyPublicationIdentity(source)
        generation = source.run("PRAGMA data_version").require_one()[0]
        source.backup_to(target, deadline=deadline, monotonic=time.monotonic)
        if ReadCompanyPublicationIdentity(target) != identity:
            raise ValueError("company_field_replay_facts_identity_changed")
        if tuple(row[0] for row in target.run("PRAGMA integrity_check")) != ("ok",):
            raise ValueError("company_field_replay_facts_integrity_failed")
        if target.run("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("company_field_replay_facts_foreign_keys_failed")
        stable = bool(generation == source.run("PRAGMA data_version").require_one()[0])
    PublishSqliteFile(target_path, sqlite_policy=policy, check=lambda: _check_deadline(deadline))
    copied = SQLiteCompanyEvidenceReader(target_path, event_policy=event_policy, sqlite_policy=policy)
    if (
        copied.read_fields((), cancel_requested=cancelled).publication_hash != publication
        or reader.read_fields((), cancel_requested=cancelled).publication_hash != publication
    ):
        raise ValueError("company_field_replay_facts_identity_changed")
    return stable


def _text_hashes(path: Path, policy: SqliteIoPolicy, deadline: float) -> tuple[bytes, ...]:
    with SqliteSession(
        path, SqliteConnectionOptions(read_only=True), sqlite_policy=policy,
        cancel_requested=lambda: time.monotonic() >= deadline,
    ) as connection:
        hashes = {row[0] for row in connection.run("SELECT body_hash FROM disclosures WHERE body_retained=1")}
        if connection.run(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='company_material_dependencies'"
        ).fetchone() is not None:
            hashes.update(row[0] for row in connection.run("SELECT DISTINCT body_hash FROM company_material_dependencies"))
    _check_deadline(deadline)
    if any(not isinstance(digest, bytes) or len(digest) != 32 for digest in hashes):
        raise ValueError("company_field_replay_body_hash_invalid")
    return tuple(sorted(hashes))


def _copy_texts(
    source_root: Path, target_root: Path, hashes: tuple[bytes, ...], maximum_text_bytes: int, deadline: float,
) -> tuple[int, int, int, int]:
    copied = missing = compressed = expanded = 0
    _reject_links(source_root)
    for digest in hashes:
        _check_deadline(deadline)
        key = digest.hex()
        source = source_root / key[:2] / f"{key}.txt.gz"
        _reject_links(source)
        if not source.exists():
            missing += 1
            continue
        if not source.is_file() or source.stat().st_size > maximum_text_bytes + 4096:
            raise ValueError("company_field_replay_text_file_invalid")
        target = target_root / key[:2] / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as incoming, target.open("xb") as outgoing:
            while block := incoming.read(1024 * 1024):
                _check_deadline(deadline)
                compressed += len(block)
                if compressed > _MAX_TEXT_BYTES:
                    raise ValueError("company_field_replay_text_capacity_exceeded")
                outgoing.write(block)
        body_hash = hashlib.sha256()
        body_bytes = 0
        with gzip.open(target, "rb") as stream:
            while block := stream.read(1024 * 1024):
                _check_deadline(deadline)
                body_bytes += len(block)
                expanded += len(block)
                if body_bytes > maximum_text_bytes or expanded > _MAX_TEXT_BYTES:
                    raise ValueError("company_field_replay_text_capacity_exceeded")
                body_hash.update(block)
        if body_hash.digest() != digest:
            raise ValueError("company_field_replay_text_hash_mismatch")
        copied += 1
    return copied, missing, compressed, expanded


def _selected_codes(
    raw_path: Path, reader: SQLiteCompanyEvidenceReader, keys: tuple[str, ...], observed_at: datetime,
    policy: SqliteIoPolicy, deadline: float,
) -> tuple[str, ...]:
    codes: set[str] = set()
    with SqliteSession(
        raw_path, SqliteConnectionOptions(read_only=True), sqlite_policy=policy,
        cancel_requested=lambda: time.monotonic() >= deadline,
    ) as connection:
        for key in keys:
            _check_deadline(deadline)
            rows = connection.run(
                "SELECT d.source,d.kind,d.published_at,c.code FROM disclosures d "
                "JOIN disclosure_codes c ON c.disclosure_id=d.id WHERE d.source_key=? ORDER BY d.source,c.code LIMIT 3",
                (key,),
            ).fetchall()
            if len(rows) != 1 or rows[0][0] != "cninfo" or rows[0][1] != "announcement":
                raise ValueError("company_field_replay_selected_source_ambiguous")
            published_at = datetime.fromisoformat(rows[0][2])
            if published_at.utcoffset() is None or published_at > observed_at:
                raise ValueError("company_field_replay_selected_source_time_invalid")
            code = rows[0][3]
            facts: list[CompanyFacts] = []
            reader.visit((code,), facts.append, cancel_requested=lambda: time.monotonic() >= deadline)
            if len(facts) != 1:
                raise ValueError("company_field_replay_selected_event_context_missing")
            events: tuple[CompanyRiskEvent | CompanyPositiveEvent, ...] = (
                *facts[0].risk_events, *facts[0].positive_events,
            )
            if not any(key in event.evidence_ids for event in events):
                raise ValueError("company_field_replay_selected_event_context_missing")
            if facts[0].announcement_coverage_start != observed_at.date() - timedelta(days=365):
                raise ValueError("company_field_replay_listing_context_missing")
            codes.add(code)
    if not 1 <= len(codes) <= 6:
        raise ValueError("company_field_replay_population_invalid")
    return tuple(sorted(codes))


def _state(
    facts_path: Path, reader: SQLiteCompanyEvidenceReader, codes: tuple[str, ...], policy: SqliteIoPolicy,
    deadline: float,
) -> _ReplayState:
    _check_deadline(deadline)
    fields = reader.read_fields(codes, cancel_requested=lambda: time.monotonic() >= deadline)
    with SqliteSession(
        facts_path, SqliteConnectionOptions(read_only=True), sqlite_policy=policy,
        cancel_requested=lambda: time.monotonic() >= deadline,
    ) as connection:
        identity = ReadCompanyPublicationIdentity(connection)
        if identity is None or fields.publication_hash != identity.content_hash.hex():
            raise ValueError("company_field_replay_reader_identity_mismatch")
        valid = tuple(tuple(row) for row in connection.run(
            "SELECT * FROM company_input_receipt WHERE outcome='valid' "
            "ORDER BY source,source_key,code,record_hash,extraction_hash"
        ))
        attempted = connection.run("SELECT COUNT(*) FROM company_input_receipt WHERE outcome='field_attempted'").require_one()[0]
    return _ReplayState(fields, identity, valid, attempted)


def _field_items(snapshot: CompanyFieldSnapshot) -> list[dict[str, object]]:
    return [{
        "code": item.code, "event_id": item.event_id, "field": item.field, "state": item.state,
        "value": item.value.isoformat() if isinstance(item.value, date) else item.value,
        "applicable": item.applicable, "reason": item.reason,
        "evidence": [{
            "source": proof.source, "source_key": proof.source_key, "record_hash": proof.record_hash,
            "body_hash": proof.body_hash.hex(), "rule_id": proof.rule_id, "subject": proof.subject.value,
            "published_at": proof.published_at.isoformat(), "start_line": proof.start_line,
            "end_line": proof.end_line, "unit": proof.unit,
        } for proof in item.evidence[:4]],
    } for item in snapshot.results[:100]]


def AuditCompanyFieldReplay(
    collection_path: Path,
    facts_path: Path,
    source_keys: tuple[str, ...],
    directory: Path,
    *,
    observed_at: datetime,
    budget_seconds: float,
    sqlite_policy: SqliteIoPolicy,
    event_policy: CompanyEventPolicy,
    protected_roots: Sequence[Path],
) -> dict[str, object]:
    """Migrate a copy, publish local evidence twice and report observed reuse.

    This diagnostic constructs no supplier or HTTP client and never writes the
    source databases. Its result authorizes neither production migration nor
    field correctness, full-market coverage or performance acceptance.
    """
    if not isinstance(source_keys, tuple) or not 1 <= len(source_keys) <= 6 or any(
        not isinstance(key, str) or not key or len(key) > 240 or any(character.isspace() for character in key)
        for key in source_keys
    ) or len(set(source_keys)) != len(source_keys):
        raise ValueError("company_field_replay_keys_invalid")
    if observed_at.utcoffset() is None:
        raise ValueError("company_field_replay_observation_requires_timezone")
    if isinstance(budget_seconds, bool) or not isinstance(budget_seconds, (int, float)) or not math.isfinite(budget_seconds) or not 0 < budget_seconds <= 10800:
        raise ValueError("company_field_replay_budget_invalid")
    deadline = time.monotonic() + budget_seconds
    collection_path, facts_path, directory = _prepare_paths(collection_path, facts_path, directory, protected_roots)
    report: dict[str, object] = {
        "schema_version": "company-field-replay", "status": "failed", "read_only_source": True,
        "isolated_copy": True, "network_requests": 0, "source_data_writes": 0,
        "field_correctness_verified": False, "production_authorized": False,
        "full_market_verified": False, "performance_verified": False, "source_keys": source_keys,
    }
    try:
        before = (_source_files(collection_path, deadline), _source_files(facts_path, deadline))
        target_facts = directory / "facts" / "company_facts.sqlite3"
        report["facts_backup_generation_stable"] = _backup_facts(
            facts_path, target_facts, sqlite_policy, event_policy, deadline,
        )
        target_raw = directory / "raw" / "company_collection.sqlite3"
        stable_backup = _backup_collection(collection_path, target_raw, sqlite_policy, deadline)
        report["collection_backup_generation_stable"] = stable_backup
        configuration = LoadCompanySyncConfiguration(PROJECT_ROOT)
        hashes = _text_hashes(target_raw, sqlite_policy, deadline)
        copied, missing, compressed, expanded = _copy_texts(
            collection_path.parent / "text", target_raw.parent / "text", hashes,
            configuration.extended_document_limits.max_text_characters * 4, deadline,
        )
        report.update({"required_texts": len(hashes), "copied_texts": copied, "missing_texts": missing,
                       "compressed_text_bytes": compressed, "expanded_text_bytes": expanded})
        reader = SQLiteCompanyEvidenceReader(target_facts, event_policy=event_policy, sqlite_policy=sqlite_policy)
        codes = _selected_codes(target_raw, reader, source_keys, observed_at, sqlite_policy, deadline)
        report["codes"] = codes
        baseline = _state(target_facts, reader, codes, sqlite_policy, deadline)
        states: list[_ReplayState] = []
        for round_number in (1, 2):
            _check_deadline(deadline)
            documents = _CountingDocuments(
                target_raw.parent / "text",
                max_text_characters=configuration.extended_document_limits.max_text_characters,
                max_bytes=configuration.temporary_text_capacity_bytes,
                reserve_bytes=configuration.temporary_disk_reserve_bytes,
            )
            index = SQLiteCompanyEvidenceIndex(
                target_raw, facts_path=target_facts, documents=documents, raw_files=None,
                event_policy=event_policy, sqlite_policy=sqlite_policy,
                cancel_requested=lambda: time.monotonic() >= deadline,
            )
            index.initialize()
            startup_reads = documents.read_calls
            index.prepare_publication(codes, observed_at)
            index.publish(codes, observed_at)
            index.publish_file()
            report[f"round_{round_number}_initialize_text_reads"] = startup_reads
            report[f"round_{round_number}_publication_text_reads"] = documents.read_calls - startup_reads
            report[f"round_{round_number}_total_text_reads"] = documents.read_calls
            states.append(_state(target_facts, reader, codes, sqlite_policy, deadline))
        first, second = states
        original_valid = frozenset(baseline.valid_receipts)
        original_valid_unchanged = original_valid.issubset(first.valid_receipts) and original_valid.issubset(second.valid_receipts)
        fields_reused = first.fields == second.fields and first.publication == second.publication
        attempts_reused = first.attempted == second.attempted
        report.update({
            "first_publication_hash": first.fields.publication_hash,
            "second_publication_hash": second.fields.publication_hash,
            "original_valid_receipts_unchanged": original_valid_unchanged,
            "second_fields_and_publication_unchanged": fields_reused,
            "initial_field_attempted": baseline.attempted, "first_field_attempted": first.attempted,
            "second_field_attempted": second.attempted, "second_new_field_attempted": second.attempted - first.attempted,
            "fields": len(second.fields.results), "field_states": dict(Counter(item.state for item in second.fields.results)),
            "field_items": _field_items(second.fields), "fields_truncated": len(second.fields.results) > 100,
        })
        after = (_source_files(collection_path, deadline), _source_files(facts_path, deadline))
        report["source_input_unchanged"] = before == after
        report["collection_source_files_unchanged"] = before[0] == after[0]
        report["facts_source_files_unchanged"] = before[1] == after[1]
        report["collection_source_files_sha256_before"] = before[0]
        report["collection_source_files_sha256_after"] = after[0]
        report["source_empty_wal_created"] = before[0][1] is None and after[0][1] == hashlib.sha256(b"").hexdigest()
        report["source_input_comparison"] = "database_wal_receipt_sha256_exact"
        _check_deadline(deadline)
        if not original_valid_unchanged or not fields_reused or not attempts_reused:
            raise ValueError("company_field_replay_second_state_changed")
        report["second_zero_publication_text_reads"] = report["round_2_publication_text_reads"] == 0
        report["status"] = "passed" if (
            before == after and stable_backup and report["facts_backup_generation_stable"] is True and missing == 0
            and report["second_zero_publication_text_reads"]
        ) else "degraded"
    except (OSError, sqlite3.Error, ValueError, RuntimeError, TimeoutError, InterruptedError, EOFError) as error:
        report["failure_category"] = type(error).__name__
        reason = str(error)
        report["failure_code"] = reason if (
            reason.startswith("company_field_replay_") and len(reason) <= 110
            and all(character in "abcdefghijklmnopqrstuvwxyz0123456789_" for character in reason)
        ) else type(error).__name__
        report["deadline_exceeded"] = time.monotonic() >= deadline
    report_path = directory / "field-replay.json"
    if report_path.exists() or report_path.is_symlink():
        raise ValueError("company_field_replay_report_exists")
    AtomicWriteJson(report_path, report, indent=2)
    return report


__all__ = ["AuditCompanyFieldReplay"]
