"""Replay local company field publication on new external SQLite and text copies."""

from __future__ import annotations

from trader.download.domain.company_field_results import CompanyFieldConclusion

import gzip
import hashlib
import math
import re
import sqlite3
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path

from scripts.download.company.company_research_baseline import ReadCompanyResearchBaseline, ReadCompanyResearchIdentity
from scripts.core.paths import PROJECT_ROOT
from scripts.download.company.output_paths import RequireIsolatedReplay
from scripts.download.company.company_risk_comparison import (
    CompareCompanyRisks, ProjectCompanyRiskComparison, WriteCompanyRiskBaseline,
)
from trader.download.domain.company_events import CompanyEventPolicy, CompanyPositiveEvent, CompanyRiskEvent
from trader.download.domain.company_facts import CompanyFacts
from trader.download.domain.company_field_results import CompanyFieldSnapshot
from trader.download.infra.company_document_files import CompanyDocumentFiles
from trader.download.infra.company_evidence_sql import COMPANY_MATERIAL_STATEMENTS
from trader.download.infra.company_source_schema import COMPANY_SOURCE_SCHEMA
from trader.download.infra.company_facts_index import CompanyPublicationIdentity, ReadCompanyPublicationIdentity, SQLiteCompanyFactsIndex, CompanyInputReceipt
from trader.download.infra.company_normalized_index import SQLiteCompanyNormalizedIndex, ReadCompanyNormalizationBaseline
from trader.download.infra.company_material_index import SQLiteCompanyMaterialIndex
from trader.download.infra.company_field_publication import CompanyFieldProofRulesHash, PrepareCompanyFieldResults
from trader.download.infra.company_field_recovery import RegisterCompanyMaterialDependencies
from trader.infra.sqlite_connection import SqliteTransaction
from trader.download.infra.company_facts_reader import SQLiteCompanyEvidenceReader
from trader.download.infra.download_settings import LoadCompanySyncConfiguration
from trader.infra.atomic_files import AtomicWriteJson
from trader.infra.sqlite_connection import EnsureSqliteSchema, SqliteConnectionOptions, SqliteSession
from trader.infra.sqlite_policy import SqliteIoPolicy
from trader.infra.settings.data_paths import DataPaths
from trader.download.infra.company_fact_rows import ReadCompanyFactRows
from trader.download.infra.company_facts_protocol import RequireCompanyTime
from trader.infra.sqlite_publication import PublishSqliteFile, VerifySqliteFile


_MAX_TEXT_BYTES = 256 * 1024 * 1024
_MAX_STOCKS = 10_000
_MATERIAL_TABLES = frozenset(
    match[1] for statement in COMPANY_MATERIAL_STATEMENTS
    if (match := re.match(r"\s*CREATE TABLE (?:IF NOT EXISTS )?(\w+)", statement, re.IGNORECASE))
)


class _CountingDocuments(CompanyDocumentFiles):
    """Count actual document-owner reads, including initialization validation."""

    def __init__(self, root: Path, *, max_text_characters: int, max_bytes: int, reserve_bytes: int) -> None:
        super().__init__(root, max_text_characters=max_text_characters, max_bytes=max_bytes, reserve_bytes=reserve_bytes)
        self.read_calls = 0
        self.read_counts: Counter[bytes] = Counter()

    def read(self, digest: bytes, *, check: Callable[[], None] | None = None) -> str:
        self.read_calls += 1
        self.read_counts[digest] += 1
        text = super().read(digest, check=check)
        if not isinstance(text, str):
            raise ValueError("company_field_replay_text_type_invalid")
        return text


@dataclass(frozen=True)
class _ReplayState:
    fields: CompanyFieldSnapshot
    publication: CompanyPublicationIdentity
    valid_receipts: tuple[tuple[object, ...], ...]
    attempted: int
    other_facts: tuple[CompanyFacts, ...]


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


def BackupCompanyCollection(
    source_path: Path, target_path: Path, policy: SqliteIoPolicy, deadline: float, *, material_only: bool = False,
) -> bool:
    """Copy a current source store, optionally keeping only material for a new isolated round."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with target_path.open("xb"):
        pass
    with SqliteSession(
        source_path, SqliteConnectionOptions(read_only=True), sqlite_policy=policy,
        cancel_requested=lambda: time.monotonic() >= deadline,
    ) as source, SqliteSession(
        target_path, SqliteConnectionOptions(foreign_keys=not material_only), sqlite_policy=policy,
        cancel_requested=lambda: time.monotonic() >= deadline,
    ) as target:
        version = source.run("PRAGMA data_version").require_one()[0]
        EnsureSqliteSchema(source, COMPANY_SOURCE_SCHEMA, exact=True)
        source.backup_to(target, deadline=deadline, monotonic=time.monotonic)
        stable = bool(version == source.run("PRAGMA data_version").require_one()[0])
        if material_only:
            with SqliteTransaction(target, immediate=True):
                tables = tuple(row[0] for row in target.run(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT GLOB 'sqlite_*' ORDER BY name"
                ))
                for table in tables:
                    if table not in _MATERIAL_TABLES:
                        target.run(f'DELETE FROM "{table}"')
        if tuple(row[0] for row in target.run("PRAGMA integrity_check")) != ("ok",):
            raise ValueError("company_field_replay_collection_integrity_failed")
        if target.run("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("company_field_replay_collection_foreign_keys_failed")
    return stable


def BackupCompanyFacts(
    source_path: Path, target_path: Path, policy: SqliteIoPolicy, event_policy: CompanyEventPolicy, deadline: float,
) -> bool:
    def cancelled() -> bool:
        return time.monotonic() >= deadline
    VerifySqliteFile(source_path, sqlite_policy=policy, check=lambda: _check_deadline(deadline), file_only=True)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with target_path.open("xb"):
        pass
    with SqliteSession(
        source_path, SqliteConnectionOptions(read_only=True), sqlite_policy=policy, cancel_requested=cancelled,
    ) as source, SqliteSession(target_path, sqlite_policy=policy, cancel_requested=cancelled) as target:
        identity = ReadCompanyResearchIdentity(source)
        generation = source.run("PRAGMA data_version").require_one()[0]
        source.backup_to(target, deadline=deadline, monotonic=time.monotonic)
        if ReadCompanyResearchIdentity(target) != identity:
            raise ValueError("company_field_replay_facts_identity_changed")
        if tuple(row[0] for row in target.run("PRAGMA integrity_check")) != ("ok",):
            raise ValueError("company_field_replay_facts_integrity_failed")
        if target.run("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("company_field_replay_facts_foreign_keys_failed")
        stable = bool(generation == source.run("PRAGMA data_version").require_one()[0])
    PublishSqliteFile(target_path, sqlite_policy=policy, check=lambda: _check_deadline(deadline))
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
        other_codes = tuple(str(row[0]) for row in connection.run(
            "SELECT code FROM company_facts ORDER BY code LIMIT ?", (_MAX_STOCKS + 1,),
        ))
    if len(other_codes) > _MAX_STOCKS:
        raise ValueError("company_field_replay_population_limit")
    other_facts: list[CompanyFacts] = []
    reader.visit(tuple(code for code in other_codes if code not in codes), other_facts.append,
                 cancel_requested=lambda: time.monotonic() >= deadline)
    return _ReplayState(fields, identity, valid, attempted, tuple(other_facts))


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
    recovered_text: Path | None = None,
) -> dict[str, object]:
    """Publish local evidence twice on a current-structure copy and report reuse.

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
    if recovered_text is not None:
        if len(source_keys) != 1 or not recovered_text.is_absolute():
            raise ValueError("company_field_replay_recovered_text_invalid")
        _reject_links(recovered_text)
    if isinstance(budget_seconds, bool) or not isinstance(budget_seconds, (int, float)) or not math.isfinite(budget_seconds) or not 0 < budget_seconds <= 10800:
        raise ValueError("company_field_replay_budget_invalid")
    deadline = time.monotonic() + budget_seconds
    collection_path, facts_path, directory = _prepare_paths(collection_path, facts_path, directory, protected_roots)
    report: dict[str, object] = {
         "status": "failed", "read_only_source": True,
        "observed_at": observed_at.isoformat(),
        "isolated_copy": True, "network_requests": 0, "source_data_writes": 0,
        "field_correctness_verified": False, "production_authorized": False,
        "full_market_verified": False, "performance_verified": False, "source_keys": source_keys,
    }
    try:
        before = (_source_files(collection_path, deadline), _source_files(facts_path, deadline))
        with SqliteSession(facts_path, SqliteConnectionOptions(read_only=True), sqlite_policy=sqlite_policy) as formal:
            if ReadCompanyPublicationIdentity(formal) is None:
                raise ValueError("company_field_replay_facts_publication_missing")
        target_facts = directory / "company_published.sqlite3"
        target_raw = directory / "company_sources.sqlite3"
        target_text = directory / "raw" / "text"
        stable_backup = BackupCompanyCollection(collection_path, target_raw, sqlite_policy, deadline, material_only=True)
        report["collection_backup_generation_stable"] = stable_backup
        configuration = LoadCompanySyncConfiguration(PROJECT_ROOT)
        hashes = _text_hashes(target_raw, sqlite_policy, deadline)
        copied, missing, compressed, expanded = _copy_texts(
            collection_path.parent / "raw" / "text", target_text, hashes,
            configuration.extended_document_limits.max_text_characters * 4, deadline,
        )
        report.update({"required_texts": len(hashes), "copied_texts": copied, "missing_texts": missing,
                       "compressed_text_bytes": compressed, "expanded_text_bytes": expanded})
        documents = _CountingDocuments(
            target_text, max_text_characters=configuration.extended_document_limits.max_text_characters,
            max_bytes=configuration.temporary_text_capacity_bytes, reserve_bytes=configuration.temporary_disk_reserve_bytes,
        )
        research = ReadCompanyResearchBaseline(target_raw, facts_path, documents,
            event_policy=event_policy, sqlite_policy=sqlite_policy, check=lambda: _check_deadline(deadline),
            cancel_requested=lambda: time.monotonic() >= deadline)
        writer = SQLiteCompanyFactsIndex(target_facts, event_policy=event_policy, sqlite_policy=sqlite_policy)
        index = SQLiteCompanyNormalizedIndex(target_raw, writer,
            event_policy=event_policy, sqlite_policy=sqlite_policy, clean_material=lambda: None)
        index.initialize()
        index.seed(research.baseline)
        report["facts_backup_generation_stable"] = True
        report["baseline_current_rule_reproof_reads"] = research.material_reads
        report["baseline_original_verified"] = sum(item.state == "verified" for item in research.original_fields)
        report["baseline_current_verified"] = sum(item.state == "verified" for item in research.baseline.fields)
        reader = SQLiteCompanyEvidenceReader(target_facts, event_policy=event_policy, sqlite_policy=sqlite_policy)
        codes = _selected_codes(target_raw, reader, source_keys, observed_at, sqlite_policy, deadline)
        report["codes"] = codes
        baseline = _state(target_facts, reader, codes, sqlite_policy, deadline)
        risk_baseline = directory / "risk-baseline.json"
        WriteCompanyRiskBaseline(target_facts, risk_baseline, deadline=deadline, sqlite_policy=sqlite_policy)
        states: list[_ReplayState] = []
        risks_preserved = True
        publications_complete = True
        material = SQLiteCompanyMaterialIndex(target_raw, facts=writer, documents=documents,
            event_policy=event_policy, sqlite_policy=sqlite_policy)
        material.initialize()
        for round_number in (1, 2):
            _check_deadline(deadline)
            startup_reads = documents.read_calls
            if round_number == 1 and recovered_text is not None:
                with SqliteSession(target_raw, sqlite_policy=sqlite_policy) as raw, SqliteTransaction(raw, immediate=True), SqliteSession(
                    target_facts, SqliteConnectionOptions(read_only=True), sqlite_policy=sqlite_policy,
                ) as formal:
                    facts = tuple(fact for fact in research.baseline.facts if fact.code in codes)
                    RegisterCompanyMaterialDependencies(raw, formal, facts, writer.extraction_hash)
                with recovered_text.open("rb") as stream:
                    payload = stream.read(configuration.extended_document_limits.max_text_characters * 4 + 1)
                if len(payload) > configuration.extended_document_limits.max_text_characters * 4:
                    raise ValueError("company_field_replay_recovered_text_oversized")
                report["recovered_required_text"] = material.recover_required_text(
                    source_keys[0], payload.decode("utf-8").strip(), observed_at)
            prepared_fields: list[CompanyFieldConclusion] = []
            prepared_receipts: list[CompanyInputReceipt] = []
            with SqliteSession(target_raw, SqliteConnectionOptions(read_only=True), sqlite_policy=sqlite_policy) as raw, SqliteSession(
                target_facts, SqliteConnectionOptions(read_only=True), sqlite_policy=sqlite_policy,
            ) as formal, SqliteTransaction(raw), SqliteTransaction(formal):
                for fact in research.baseline.facts:
                    if fact.code in codes:
                        prepared = PrepareCompanyFieldResults(raw, formal, fact, (), observed_at=fact.checked_at,
                            extraction_hash=writer.extraction_hash, event_policy=event_policy, read_document=documents.read)
                        prepared_fields.extend(prepared.results)
                        prepared_receipts.extend(prepared.receipts)
            index.publish_fields(codes, tuple(prepared_fields), tuple(prepared_receipts), observed_at,
                                 field_rules_hash=CompanyFieldProofRulesHash())
            report[f"round_{round_number}_publication_pending_codes"] = 0
            report[f"round_{round_number}_initialize_text_reads"] = 0
            report[f"round_{round_number}_publication_text_reads"] = documents.read_calls - startup_reads
            report[f"round_{round_number}_total_text_reads"] = documents.read_calls
            comparison = CompareCompanyRisks(target_facts, risk_baseline, deadline=deadline, sqlite_policy=sqlite_policy,
                cancel_requested=lambda: time.monotonic() >= deadline)
            report[f"round_{round_number}_risk_comparison"] = ProjectCompanyRiskComparison(comparison)
            risks_preserved = risks_preserved and comparison.all_old_risks_and_evidence_preserved
            states.append(_state(target_facts, reader, codes, sqlite_policy, deadline))
        first, second = states
        report["initial_missing_texts"] = missing
        required = _text_hashes(target_raw, sqlite_policy, deadline)
        missing = 0
        for digest in required:
            _check_deadline(deadline)
            key = digest.hex()
            missing += not (target_text / key[:2] / (key + ".txt.gz")).is_file()
        report["final_required_texts"] = len(required)
        report["missing_texts"] = missing
        original_valid = frozenset(baseline.valid_receipts)
        original_valid_unchanged = original_valid.issubset(first.valid_receipts) and original_valid.issubset(second.valid_receipts)
        fields_reused = first.fields == second.fields and first.publication == second.publication
        attempts_reused = first.attempted == second.attempted
        other_facts_preserved = baseline.other_facts == first.other_facts == second.other_facts
        report.update({
            "unselected_stocks": len(baseline.other_facts), "unselected_facts_unchanged": other_facts_preserved,
            "original_risks_and_evidence_preserved": risks_preserved,
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
        if not original_valid_unchanged or not fields_reused or not attempts_reused or not other_facts_preserved:
            raise ValueError("company_field_replay_second_state_changed")
        report["second_zero_publication_text_reads"] = report["round_2_publication_text_reads"] == 0
        report["status"] = "passed" if (
            before == after and stable_backup and report["facts_backup_generation_stable"] is True and missing == 0
            and report["second_zero_publication_text_reads"] and risks_preserved and publications_complete
        ) else "degraded"
    except (OSError, sqlite3.Error, ValueError, RuntimeError, TimeoutError, InterruptedError, EOFError) as error:
        report["failure_category"] = type(error).__name__
        reason = str(error)
        report["failure_code"] = reason if (
            reason.startswith(("company_field_replay_", "company_material_recovery_")) and len(reason) <= 110
            and all(character in "abcdefghijklmnopqrstuvwxyz0123456789_" for character in reason)
        ) else type(error).__name__
        report["deadline_exceeded"] = time.monotonic() >= deadline
    report_path = directory / "field-replay.json"
    if report_path.exists() or report_path.is_symlink():
        raise ValueError("company_field_replay_report_exists")
    AtomicWriteJson(report_path, report, indent=2)
    return report


def PrepareCompanyNormalNamespace(
    *, original_facts_path: Path, proven_facts_path: Path, collection_path: Path,
    documents: tuple[CompanyDocumentFiles, ...], directory: Path,
    event_policy: CompanyEventPolicy, sqlite_policy: SqliteIoPolicy, budget_seconds: float,
) -> dict[str, object]:
    """Explicit isolated typed input, preserving the original financial baseline.

    The original source codec is used only here and by the research replay.
    This does not migrate a public store, replay an old job, promote the frozen
    unadmitted financial cohort, or copy/reset an existing request allowance.
    """
    if directory.exists() or not 30 <= budget_seconds <= 900 or not documents:
        raise ValueError("company_normal_namespace_new_directory_required")
    RequireIsolatedReplay(directory, (PROJECT_ROOT, original_facts_path.parent, proven_facts_path.parent,
        collection_path.parent, *(owner.root for owner in documents)))
    deadline = time.monotonic() + budget_seconds
    def check() -> None:
        if time.monotonic() >= deadline:
            raise TimeoutError("company_normal_namespace_deadline_exceeded")
    for path in (original_facts_path, proven_facts_path):
        VerifySqliteFile(path, sqlite_policy=sqlite_policy, file_only=True, check=check)
    options = SqliteConnectionOptions(read_only=True)
    with (SqliteSession(proven_facts_path, options, sqlite_policy=sqlite_policy) as proven,
        SqliteSession(original_facts_path, options, sqlite_policy=sqlite_policy) as original,
        SqliteTransaction(proven), SqliteTransaction(original)):
        prepared = ReadCompanyNormalizationBaseline(proven)
        codes = tuple(fact.code for fact in prepared.facts)
        source_identity = ReadCompanyResearchIdentity(original)
        old = ReadCompanyFactRows(original, codes)
        if tuple(fact.code for fact in old) != codes:
            raise ValueError("company_normal_namespace_source_population_mismatch")
        # Current parser quality/corrections are retained. The known partial
        # financial research cohort remains outside normal formal adoption.
        facts = tuple(replace(current, financial=previous.financial, latest_financial=previous.latest_financial,
            financial_state=previous.financial_state) for current, previous in zip(prepared.facts, old, strict=True))
        original_receipts = tuple(CompanyInputReceipt(row[0], row[1], row[2], row[3], row[4], row[5], RequireCompanyTime(row[6]), row[7], row[8])
            for row in original.run("SELECT * FROM company_input_receipt ORDER BY source,source_key,code,record_hash,extraction_hash"))
        receipts = tuple(item for item in prepared.receipts if item.source != "eastmoney_financial" or item in original_receipts)
        baseline = replace(prepared, facts=facts, receipts=receipts, identity=source_identity, financial_observations=())
    paths = DataPaths(directory)
    stable = BackupCompanyCollection(collection_path, paths.company_sources, sqlite_policy, deadline, material_only=True)
    target_documents = CompanyDocumentFiles(paths.company_documents, max_text_characters=2_000_000,
        max_bytes=2 * 1024 * 1024 * 1024, reserve_bytes=1)
    copied = missing = 0
    with SqliteSession(paths.company_sources, options, sqlite_policy=sqlite_policy) as raw:
        hashes = tuple(row[0] for row in raw.run("SELECT DISTINCT body_hash FROM disclosures WHERE body_retained=1 AND body_hash IS NOT NULL ORDER BY body_hash"))
    for digest in hashes:
        check()
        text: str | None = None
        for owner in documents:
            try:
                text = owner.read(digest, check=check)
                break
            except FileNotFoundError:
                continue
        if text is None:
            missing += 1
            continue
        with target_documents.prepare(text) as actual:
            if actual != digest:
                raise ValueError("company_normal_namespace_original_body_hash_mismatch")
        copied += 1
    writer = SQLiteCompanyFactsIndex(paths.company, event_policy=event_policy, sqlite_policy=sqlite_policy)
    normalized = SQLiteCompanyNormalizedIndex(paths.company_sources, writer, event_policy=event_policy,
        sqlite_policy=sqlite_policy, clean_material=lambda: None)
    normalized.initialize()
    normalized.seed(baseline)
    material = SQLiteCompanyMaterialIndex(paths.company_sources, facts=writer, documents=target_documents,
        event_policy=event_policy, sqlite_policy=sqlite_policy)
    material.initialize()
    material.validate_material()
    reader = SQLiteCompanyEvidenceReader(paths.company, event_policy=event_policy, sqlite_policy=sqlite_policy)
    fields = reader.read_fields(codes)
    report: dict[str, object] = {
        "source_data_writes": 0, "network_requests": 0, "source_backup_stable": stable,
        "original_source_hash": source_identity.content_hash.hex(), "proven_source_hash": prepared.identity.content_hash.hex(),
        "publication_hash": reader.publication_identity(), "source_rule_hash": prepared.field_rules_hash.hex(),
        "stocks": len(codes), "fields": len(fields.results), "reader_equal": reader.read(codes) == facts,
        "original_financials_preserved": all((left.financial, left.latest_financial, left.financial_state) ==
            (right.financial, right.latest_financial, right.financial_state) for left, right in zip(facts, old, strict=True)),
        "original_risks_preserved": all(left.risk_events == right.risk_events for left, right in zip(facts, old, strict=True)),
        "original_valid_receipts_preserved": all(item in receipts for item in original_receipts if item.outcome == "valid"),
        "unadmitted_financial_metadata": 0, "copied_original_texts": copied, "unavailable_original_texts": missing,
        "production_adopted": False, "full_sync_verified": False,
    }
    AtomicWriteJson(directory.parent / "namespace.json", report)
    return report


__all__ = ["AuditCompanyFieldReplay", "BackupCompanyCollection", "BackupCompanyFacts", "PrepareCompanyNormalNamespace"]
