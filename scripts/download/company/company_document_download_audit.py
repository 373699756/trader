"""Compare failed-PDF requests or recover bounded documents in explicit isolation."""

from __future__ import annotations

from scripts.core.paths import PROJECT_ROOT as TOOL_ROOT

import hashlib
import subprocess
import tempfile
import time
import json
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import cast

from trader.bootstrap import OpenExternalHttp
from scripts.download.company.output_paths import RequireIsolatedReplay
from scripts.download.company.company_failure_audit import ParseCompanyFailureIdentity
from scripts.download.company.company_field_replay import BackupCompanyCollection, BackupCompanyFacts
from trader.download.application.company_failures import CompanyFailureCode
from trader.download.domain.company_evidence import BuildCompanyDocumentPolicies, CompanyDocumentFailure, Disclosure
from trader.download.domain.company_events import CompanyEventPolicy
from trader.download.domain.company_events import RepurchaseMeasurements, IncreaseMeasurements
from trader.download.domain.company_event_extraction import ExtractCompanyFieldEvidence, ExtractCompanyLinkCandidate
from trader.download.domain.company_evidence import CompanyDocument
from trader.download.domain.company_field_results import CompanyFieldConclusion
from trader.download.domain.company_facts import CompanyFacts
from trader.download.infra.company_disclosure_source import CninfoMarketDisclosureSource
from trader.download.infra.company_document_ocr import CompanyOcrEngine, DetectCompanyOcr
from trader.download.infra.company_document_parser import DetectCompanyText
from trader.download.infra.company_fact_inputs import DecodeCompanyDisclosure
from trader.download.infra.company_facts_index import CompanyInputReceipt, ReadCompanyPublicationIdentity, SQLiteCompanyFactsIndex
from trader.download.infra.company_fact_rows import ReadCompanyFactRows
from trader.download.infra.company_field_result_rows import ReadCompanyFieldResults
from trader.download.infra.company_field_publication import CompanyFieldProofRulesHash, PrepareCompanyFieldResults
from trader.download.infra.company_field_recovery import RegisterCompanyMaterialDependencies, RecoverRequiredCompanyText
from trader.download.infra.company_document_files import CompanyDocumentFiles
from trader.download.infra.company_raw_files import CompanyRawFiles
from trader.download.infra.download_settings import LoadCompanySyncConfiguration
from trader.infra.atomic_files import AtomicWriteJson
from trader.infra.http_contracts import HttpAttempt, HttpError, HttpRequest, HttpTransport
from trader.infra.sqlite_connection import SqliteConnection, SqliteConnectionOptions, SqliteSession, SqliteTransaction
from trader.infra.sqlite_policy import SqliteIoPolicy
from trader.infra.sqlite_publication import VerifySqliteFile
from trader.infra.regular_file import OpenRegularFile
from trader.infra.process_lock import ProcessLock


def _field_value(field: CompanyFieldConclusion) -> str | float | None:
    return field.value.isoformat() if isinstance(field.value, date) else field.value


def FreezeCompanyFieldProofRecovery(
    collection_path: Path,
    facts_path: Path,
    targets: tuple[tuple[str, str, str], ...],
    *,
    observed_at: datetime,
    sqlite_policy: SqliteIoPolicy,
    check: Callable[[], None] | None = None,
) -> dict[str, object]:
    """Freeze exact old-proof dependencies before any explicit recovery request."""
    if observed_at.utcoffset() is None or not 1 <= len(targets) <= 68 or len(set(targets)) != len(targets):
        raise ValueError("company_field_recovery_targets_invalid")
    def cancelled() -> bool:
        if check is not None:
            check()
        return False
    VerifySqliteFile(facts_path, sqlite_policy=sqlite_policy, file_only=True, check=check)
    options = SqliteConnectionOptions(read_only=True)
    materials: dict[tuple[str, str, str, str], dict[str, object]] = {}
    selected: list[dict[str, object]] = []
    with (
        SqliteSession(collection_path, options, sqlite_policy=sqlite_policy, cancel_requested=cancelled) as raw,
        SqliteSession(facts_path, options, sqlite_policy=sqlite_policy, cancel_requested=cancelled) as formal,
    ):
        before = raw.run("PRAGMA data_version").require_one()[0], formal.run("PRAGMA data_version").require_one()[0]
        with SqliteTransaction(raw), SqliteTransaction(formal):
            identity = ReadCompanyPublicationIdentity(formal)
            if identity is None:
                raise ValueError("company_field_recovery_publication_missing")
            previous = ReadCompanyFieldResults(formal, tuple(sorted({target[0] for target in targets})))
            for old in previous:
                cancelled()
                if (old.code, old.event_id, old.field) not in targets:
                    continue
                if old.state != "verified" or not old.evidence:
                    raise ValueError("company_field_recovery_original_proof_missing")
                selected.append({"code": old.code, "event_id": old.event_id, "field": old.field, "value": _field_value(old)})
                for proof in old.evidence:
                    rows = raw.run(
                        "SELECT d.*,c.code FROM disclosures d JOIN disclosure_codes c ON c.disclosure_id=d.id "
                        "WHERE d.source=? AND d.source_key=? AND c.code=? "
                        "AND (SELECT COUNT(*) FROM disclosure_codes m WHERE m.disclosure_id=d.id)=1",
                        (proof.source, proof.source_key, old.code),
                    ).fetchall()
                    if len(rows) != 1:
                        raise ValueError("company_field_recovery_source_ambiguous")
                    row = rows[0]
                    item = DecodeCompanyDisclosure(row)
                    if (item.source != "cninfo" or item.kind != "announcement"
                        or not item.url.startswith("https://static.cninfo.com.cn/finalpage/")
                        or item.digest != proof.record_hash or row["body_hash"] != proof.body_hash
                        or item.published_at != proof.published_at or item.published_at > observed_at
                        or not observed_at.date() - timedelta(days=365) <= item.published_at.date() <= observed_at.date()):
                        raise ValueError("company_field_recovery_source_identity_changed")
                    if formal.run(
                        "SELECT 1 FROM company_input_receipt WHERE source=? AND source_key=? AND code=? AND record_hash=? "
                        "AND body_hash=? AND extraction_hash=? AND outcome='valid'",
                        (item.source, item.identity, old.code, item.digest, proof.body_hash, identity.extraction_hash),
                    ).fetchone() is None:
                        raise ValueError("company_field_recovery_valid_receipt_missing")
                    payloads = tuple(row[0] for row in raw.run(
                        "SELECT DISTINCT payload_hash FROM document_tasks WHERE source=? AND source_key=? "
                        "AND record_hash=? AND payload_hash IS NOT NULL",
                        (item.source, item.identity, item.digest),
                    ))
                    if len(payloads) > 1 or any(not isinstance(value, bytes) or len(value) != 32 for value in payloads):
                        raise ValueError("company_field_recovery_pdf_identity_ambiguous")
                    key = old.code, item.source, item.identity, item.digest
                    material = {
                        "code": old.code, "source": item.source, "source_key": item.identity, "record_hash": item.digest,
                        "body_hash": proof.body_hash.hex(), "published_at": item.published_at.isoformat(),
                        "title": item.title, "url": item.url,
                        "pdf_hash": payloads[0].hex() if payloads else None,
                    }
                    if key in materials and materials[key] != material:
                        raise ValueError("company_field_recovery_material_conflict")
                    materials[key] = material
        stable = before == (raw.run("PRAGMA data_version").require_one()[0], formal.run("PRAGMA data_version").require_one()[0])
    if not stable or len(selected) != len(targets) or not 1 <= len(materials) <= 52:
        raise ValueError("company_field_recovery_scope_changed_or_exceeded")
    configuration = LoadCompanySyncConfiguration(TOOL_ROOT)
    return {
        "scope": "exact_published_field_proof_materials",
        "observed_at": observed_at.isoformat(), "publication_hash": identity.content_hash.hex(),
        "extraction_hash": identity.extraction_hash.hex(), "field_rule_hash": CompanyFieldProofRulesHash().hex(),
        "request_limit": len(materials), "attempt_limit_per_object": 1, "request_timeout_seconds": 15,
        "max_document_bytes": configuration.extended_document_limits.max_bytes,
        "targets": sorted(selected, key=lambda target: (str(target["code"]), str(target["event_id"]), str(target["field"]))),
        "materials": [materials[key] for key in sorted(materials)],
        "source_data_writes": 0, "network_requests": 0, "production_adopted": False,
    }


def InspectCompanyUnreprovedFields(
    collection_path: Path,
    facts_path: Path,
    targets: tuple[tuple[str, str, str], ...],
    *,
    observed_at: datetime,
    sqlite_policy: SqliteIoPolicy,
    event_policy: CompanyEventPolicy,
) -> dict[str, object]:
    """Annotate exact old proofs and current parsing without granting or publishing fields."""
    if not 1 <= len(targets) <= 17 or len(set(targets)) != len(targets):
        raise ValueError("company_field_annotation_targets_invalid")
    configuration = LoadCompanySyncConfiguration(TOOL_ROOT)
    documents = CompanyDocumentFiles(
        collection_path.parent / "text", max_text_characters=configuration.extended_document_limits.max_text_characters,
        max_bytes=configuration.temporary_text_capacity_bytes, reserve_bytes=configuration.temporary_disk_reserve_bytes,
    )
    observations: list[dict[str, object]] = []
    options = SqliteConnectionOptions(read_only=True)
    before = _recovery_identity(collection_path), _recovery_identity(facts_path)
    VerifySqliteFile(facts_path, sqlite_policy=sqlite_policy, file_only=True)
    with (
        SqliteSession(collection_path, options, sqlite_policy=sqlite_policy) as raw,
        SqliteSession(facts_path, options, sqlite_policy=sqlite_policy) as formal,
        SqliteTransaction(raw), SqliteTransaction(formal),
    ):
        publication = ReadCompanyPublicationIdentity(formal)
        fields = ReadCompanyFieldResults(formal, tuple(sorted({key[0] for key in targets})))
        for old in fields:
            if (old.code, old.event_id, old.field) not in targets:
                continue
            sources: list[dict[str, object]] = []
            for proof in old.evidence:
                rows = raw.run(
                    "SELECT d.*,c.code FROM disclosures d JOIN disclosure_codes c ON c.disclosure_id=d.id "
                    "WHERE d.source=? AND d.source_key=? AND c.code=? AND d.record_hash=? AND d.body_hash=?",
                    (proof.source, proof.source_key, old.code, proof.record_hash, proof.body_hash),
                ).fetchall()
                if len(rows) != 1:
                    raise ValueError("company_field_annotation_source_changed")
                text = documents.read(proof.body_hash)
                document = CompanyDocument(DecodeCompanyDisclosure(rows[0]), text, True)
                candidates = ExtractCompanyFieldEvidence(document, observed_at, event_policy)
                linked = ExtractCompanyLinkCandidate(document, observed_at, event_policy)
                lines = text.splitlines()
                first, last = max(0, proof.start_line - 3), min(len(lines), proof.end_line + 2)
                summary: dict[str, object] = {
                    "source": proof.source, "source_key": proof.source_key, "record_hash": proof.record_hash,
                    "body_hash": proof.body_hash.hex(), "published_at": proof.published_at.isoformat(),
                    "title": document.disclosure.title, "old_subject": proof.subject.value, "old_quote": proof.quote,
                    "start_line": proof.start_line, "end_line": proof.end_line,
                    "context": [{"line": index + 1, "text": lines[index][:400]} for index in range(first, min(last, first + 12))],
                    "current_field_candidates": [{"field": item.field, "value": _field_value(item),
                                                  "event_id": item.event_id,
                                                  "subject": item.evidence[0].subject.value if item.evidence else None}
                                                 for item in candidates if item.field == old.field],
                    "current_link_candidate": None,
                }
                if linked is not None and linked.positive is not None:
                    event = linked.positive
                    measurement = event.measurements
                    summary["current_link_candidate"] = {
                        "event_type": event.event_type.value, "subject": event.subject_type.value,
                        "status": event.status.value,
                        "actual_pct": measurement.actual_ratio_pct if isinstance(measurement, (RepurchaseMeasurements, IncreaseMeasurements)) else None,
                        "purpose": measurement.purpose.value if isinstance(measurement, RepurchaseMeasurements) else None,
                    }
                sources.append(summary)
            observations.append({"code": old.code, "event_id": old.event_id, "field": old.field,
                                 "old_value": _field_value(old), "sources": sources, "published": False})
    if len(observations) != len(targets):
        raise ValueError("company_field_annotation_target_missing")
    return {"publication_hash": publication.content_hash.hex() if publication else None,
            "source_unchanged": before == (_recovery_identity(collection_path), _recovery_identity(facts_path)),
            "read_only": True, "network_requests": 0, "source_data_writes": 0,
            "field_correctness_verified": False, "items": observations}


@dataclass(frozen=True)
class _RecoveryObservation:
    source_key: str
    code: str
    status: str
    requests: int | None
    same_pdf: bool = False
    same_body: bool = False
    recovered: bool = False
    failure: str | None = None

    def projection(self) -> dict[str, object]:
        return {"source_key": self.source_key, "code": self.code, "status": self.status,
                "requests": self.requests, "same_pdf": self.same_pdf, "same_body": self.same_body,
                "recovered": self.recovered, "failure": self.failure}


def _recovery_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("company_field_recovery_object_invalid")
    return cast(dict[str, object], value)


def _recovery_entries(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list):
        raise ValueError("company_field_recovery_entries_invalid")
    return tuple(_recovery_object(item) for item in value)


def _recovery_targets(manifest: dict[str, object]) -> tuple[tuple[str, str, str], ...]:
    targets: list[tuple[str, str, str]] = []
    for item in _recovery_entries(manifest.get("targets")):
        parts = item.get("code"), item.get("event_id"), item.get("field")
        if any(not isinstance(part, str) or not part for part in parts):
            raise ValueError("company_field_recovery_target_invalid")
        targets.append(cast(tuple[str, str, str], parts))
    return tuple(targets)


def _recovery_identity(path: Path) -> tuple[tuple[int, int, int] | None, ...]:
    identities: list[tuple[int, int, int] | None] = []
    for file in (path, Path(f"{path}-wal"), Path(f"{path}.sha256")):
        if any(parent.is_symlink() for parent in (file, *file.parents)):
            raise ValueError("company_field_recovery_symlink_rejected")
        try:
            metadata = file.stat()
        except FileNotFoundError:
            identities.append(None)
        else:
            identities.append((metadata.st_ino, metadata.st_size, metadata.st_mtime_ns))
    return tuple(identities)


def _recovery_output_path(path: Path, *, database: bool = False) -> None:
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("company_field_recovery_output_symlink_rejected")
    if database and path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
        raise ValueError("company_field_recovery_output_independent_file_required")


def _recovery_checkpoint(
    path: Path, manifest_hash: str, prepared: bool, observations: Sequence[_RecoveryObservation],
) -> None:
    AtomicWriteJson(path, {"manifest_hash": manifest_hash, "prepared": prepared,
                           "observations": [item.projection() for item in observations]})


def _recovery_read_checkpoint(
    path: Path, manifest_hash: str, check: Callable[[], None],
) -> tuple[_RecoveryObservation, ...]:
    with OpenRegularFile(path, maximum_bytes=64 * 1024, check=check) as stream:
        checkpoint = _recovery_object(json.loads(stream.read()))
    if checkpoint.get("manifest_hash") != manifest_hash or checkpoint.get("prepared") is not True:
        raise ValueError("company_field_recovery_checkpoint_unrelated_or_incomplete")
    observations: list[_RecoveryObservation] = []
    for item in _recovery_entries(checkpoint.get("observations")):
        key, code, status, requests = item.get("source_key"), item.get("code"), item.get("status"), item.get("requests")
        failure = item.get("failure")
        if (not isinstance(key, str) or not isinstance(code, str) or not isinstance(status, str)
            or requests is not None and (type(requests) is not int or not 0 <= requests <= 1)
            or failure is not None and not isinstance(failure, str)
            or any(type(item.get(flag)) is not bool for flag in ("same_pdf", "same_body", "recovered"))):
            raise ValueError("company_field_recovery_checkpoint_invalid")
        observations.append(_RecoveryObservation(key, code, status, requests,
                                                cast(bool, item["same_pdf"]), cast(bool, item["same_body"]),
                                                cast(bool, item["recovered"]), failure))
    if len({item.source_key for item in observations}) != len(observations):
        raise ValueError("company_field_recovery_checkpoint_duplicate")
    return tuple(observations)


def _recovery_reprove(
    collection: Path, formal_path: Path, source_text: CompanyDocumentFiles, recovered_text: CompanyDocumentFiles,
    recovered_hashes: frozenset[bytes], codes: tuple[str, ...], sqlite_policy: SqliteIoPolicy,
    event_policy: CompanyEventPolicy, observed_at: datetime, deadline: float,
) -> tuple[CompanyFacts, ...]:
    def cancelled() -> bool:
        return time.monotonic() >= deadline

    def check() -> None:
        if cancelled():
            raise TimeoutError("company_field_recovery_deadline_exceeded")

    check()
    def read_material(digest: bytes) -> str:
        owner = recovered_text if digest in recovered_hashes else source_text
        return owner.read(digest, check=check)

    fields: list[CompanyFieldConclusion] = []
    index = SQLiteCompanyFactsIndex(formal_path, event_policy=event_policy, sqlite_policy=sqlite_policy,
                                    cancel_requested=cancelled)
    with (
        SqliteSession(collection, SqliteConnectionOptions(read_only=True), sqlite_policy=sqlite_policy,
                      cancel_requested=cancelled) as raw,
        SqliteSession(formal_path, SqliteConnectionOptions(read_only=True), sqlite_policy=sqlite_policy,
                      cancel_requested=cancelled) as formal,
        SqliteTransaction(raw), SqliteTransaction(formal),
    ):
        parent = ReadCompanyPublicationIdentity(formal)
        if parent is None or parent.extraction_hash != index.extraction_hash:
            raise ValueError("company_field_recovery_extraction_changed")
        facts = ReadCompanyFactRows(formal, codes)
        receipts: list[CompanyInputReceipt] = []
        for fact in facts:
            check()
            prepared = PrepareCompanyFieldResults(
                raw, formal, fact, (), observed_at=fact.checked_at, extraction_hash=parent.extraction_hash,
                event_policy=event_policy, read_document=read_material, revalidate=True,
            )
            fields.extend(prepared.results)
            receipts.extend(prepared.receipts)
    check()
    index.publish((), {}, parent, observed_at, receipts=receipts, field_results=fields)
    check()
    return facts


def AuditCompanyFieldProofRecovery(
    collection_path: Path,
    facts_path: Path,
    manifest_path: Path,
    directory: Path,
    *,
    observed_at: datetime,
    budget_seconds: float,
    sqlite_policy: SqliteIoPolicy,
    event_policy: CompanyEventPolicy,
    session: HttpTransport,
    http_attempts: Sequence[HttpAttempt],
    object_limit: int | None = None,
) -> dict[str, object]:
    """Hold the existing process-lock owner throughout one exact recovery/resume."""
    if (observed_at.utcoffset() is None or not math.isfinite(budget_seconds) or budget_seconds <= 0
        or object_limit is not None and (type(object_limit) is not int or not 1 <= object_limit <= 52)):
        raise ValueError("company_field_recovery_limits_invalid")
    RequireIsolatedReplay(directory, (TOOL_ROOT, collection_path.parent, facts_path.parent))
    if not directory.is_absolute() or any(path.is_symlink() for path in (directory, *directory.parents)):
        raise ValueError("company_field_recovery_path_invalid")
    lock_path = directory.with_name(f".{directory.name}-proof-recovery.lock")
    if lock_path.is_symlink():
        raise ValueError("company_field_recovery_symlink_rejected")
    with ProcessLock(lock_path, label="company-field-proof-recovery"):
        return _audit_company_field_proof_recovery(
            collection_path, facts_path, manifest_path, directory, observed_at=observed_at,
            budget_seconds=budget_seconds, sqlite_policy=sqlite_policy, event_policy=event_policy,
            session=session, http_attempts=http_attempts, object_limit=object_limit,
        )


def _audit_company_field_proof_recovery(
    collection_path: Path,
    facts_path: Path,
    manifest_path: Path,
    directory: Path,
    *,
    observed_at: datetime,
    budget_seconds: float,
    sqlite_policy: SqliteIoPolicy,
    event_policy: CompanyEventPolicy,
    session: HttpTransport,
    http_attempts: Sequence[HttpAttempt],
    object_limit: int | None,
) -> dict[str, object]:
    """Recover a frozen exact scope on isolated copies; consumed request intents never retry."""
    deadline = time.monotonic() + budget_seconds

    def cancelled() -> bool:
        return time.monotonic() >= deadline

    def check() -> None:
        if cancelled():
            raise TimeoutError("company_field_recovery_deadline_exceeded")

    RequireIsolatedReplay(directory, (TOOL_ROOT, collection_path.parent, facts_path.parent))
    if not directory.is_absolute() or any(path.is_symlink() for path in (
        directory, *directory.parents, manifest_path, *manifest_path.parents,
    )):
        raise ValueError("company_field_recovery_path_invalid")
    before = _recovery_identity(collection_path), _recovery_identity(facts_path)
    with OpenRegularFile(manifest_path, maximum_bytes=256 * 1024, check=check) as stream:
        manifest_bytes = stream.read()
    manifest = _recovery_object(json.loads(manifest_bytes))
    manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    targets = _recovery_targets(manifest)
    fixed_at_value = manifest.get("observed_at")
    if not isinstance(fixed_at_value, str):
        raise ValueError("company_field_recovery_manifest_time_invalid")
    fixed_at = datetime.fromisoformat(fixed_at_value)
    if fixed_at > observed_at or observed_at.date() - fixed_at.date() > timedelta(days=1):
        raise ValueError("company_field_recovery_manifest_expired")
    current = FreezeCompanyFieldProofRecovery(
        collection_path, facts_path, targets, observed_at=fixed_at, sqlite_policy=sqlite_policy, check=check,
    )
    if manifest != current:
        raise ValueError("company_field_recovery_manifest_identity_changed")
    materials = _recovery_entries(manifest["materials"])
    codes = tuple(sorted({key[0] for key in targets}))
    configuration = LoadCompanySyncConfiguration(TOOL_ROOT)
    raw_files = CompanyRawFiles(
        directory / "raw", capacity=configuration.raw_capacity_bytes,
        analysis_reserve=configuration.raw_analysis_reserve_bytes,
        free_reserve=configuration.temporary_disk_reserve_bytes,
        maximum_document_bytes=configuration.extended_document_limits.max_bytes,
    )
    text_options = {"max_text_characters": configuration.extended_document_limits.max_text_characters,
                    "max_bytes": configuration.temporary_text_capacity_bytes,
                    "reserve_bytes": configuration.temporary_disk_reserve_bytes}
    source_text = CompanyDocumentFiles(collection_path.parent / "text", **text_options)
    recovered_text = CompanyDocumentFiles(directory / "raw" / "text", **text_options)
    collection, formal_path = directory / "raw" / "company_collection.sqlite3", directory / "formal" / "company_facts.sqlite3"
    for path in (collection, formal_path):
        for suffix in ("", "-wal", "-shm", ".sha256"):
            _recovery_output_path(Path(f"{path}{suffix}"), database=True)
    for material in materials:
        digest = cast(str, material["body_hash"])
        _recovery_output_path(recovered_text.root / digest[:2] / f"{digest}.txt.gz")
    checkpoint_path = directory / "proof-recovery-checkpoint.json"
    if directory.exists() and any(directory.iterdir()):
        observations = list(_recovery_read_checkpoint(checkpoint_path, manifest_hash, check))
    else:
        directory.mkdir(parents=True, exist_ok=True)
        _recovery_checkpoint(checkpoint_path, manifest_hash, False, ())
        if (not BackupCompanyCollection(collection_path, collection, sqlite_policy, deadline)
            or not BackupCompanyFacts(facts_path, formal_path, sqlite_policy, event_policy, deadline)):
            raise ValueError("company_field_recovery_source_changed_during_copy")
        facts = _recovery_reprove(collection, formal_path, source_text, recovered_text, frozenset(), codes,
                                  sqlite_policy, event_policy, observed_at, deadline)
        with (
            SqliteSession(collection, sqlite_policy=sqlite_policy, cancel_requested=cancelled) as raw,
            SqliteSession(formal_path, SqliteConnectionOptions(read_only=True), sqlite_policy=sqlite_policy,
                          cancel_requested=cancelled) as formal,
            SqliteTransaction(raw, immediate=True), SqliteTransaction(formal),
        ):
            RegisterCompanyMaterialDependencies(raw, formal, facts, bytes.fromhex(cast(str, manifest["extraction_hash"])))
            for material in materials:
                if raw.run(
                    "SELECT 1 FROM company_material_dependencies m JOIN disclosures d ON d.id=m.disclosure_id "
                    "WHERE d.source=? AND d.source_key=? AND m.code=? AND m.record_hash=? AND m.body_hash=? "
                    "AND m.extraction_hash=?",
                    (material["source"], material["source_key"], material["code"], material["record_hash"],
                     bytes.fromhex(cast(str, material["body_hash"])), bytes.fromhex(cast(str, manifest["extraction_hash"]))),
                ).fetchone() is None:
                    raise ValueError("company_field_recovery_exact_dependency_missing")
        observations = []
        _recovery_checkpoint(checkpoint_path, manifest_hash, True, observations)
    material_keys = {cast(str, item["source_key"]) for item in materials}
    if any(item.source_key not in material_keys for item in observations):
        raise ValueError("company_field_recovery_checkpoint_scope_changed")
    capability = DetectCompanyOcr(configuration.document_ocr_languages)
    text_capability = DetectCompanyText()
    source = CninfoMarketDisclosureSource(
        session, monotonic=time.monotonic, sleeper=time.sleep, timeout_seconds=min(15, configuration.source_timeout_seconds),
        document_download_timeout_seconds=min(15, configuration.document_download_timeout_seconds),
        document_parse_timeout_seconds=configuration.document_parse_timeout_seconds,
        request_interval_seconds=configuration.request_interval_seconds,
        document_limits=configuration.document_limits, extended_document_limits=configuration.extended_document_limits,
        document_policies=BuildCompanyDocumentPolicies(configuration, capability.identity, text_identity=text_capability.identity),
        text_capability=text_capability,
        ocr_engine=CompanyOcrEngine(
            capability, timeout_seconds=configuration.document_ocr_timeout_seconds,
            page_timeout_seconds=configuration.document_ocr_page_timeout_seconds, max_pages=configuration.document_ocr_max_pages,
            dpi=configuration.document_ocr_dpi, cancel_requested=cancelled, retry_each_page=True,
        ),
        cancel_requested=cancelled, raw_files=raw_files, text_reserve_bytes=configuration.text_task_reserve_bytes,
        ocr_reserve_bytes=configuration.ocr_task_reserve_bytes,
    )
    attempted_now = 0
    for material in materials:
        key, code = cast(str, material["source_key"]), cast(str, material["code"])
        if key in {item.source_key for item in observations}:
            continue
        if cancelled() or object_limit is not None and attempted_now >= object_limit:
            break
        with SqliteSession(collection, SqliteConnectionOptions(read_only=True), sqlite_policy=sqlite_policy,
                           cancel_requested=cancelled) as raw:
            row = raw.run("SELECT d.*,c.code FROM disclosures d JOIN disclosure_codes c ON c.disclosure_id=d.id "
                          "WHERE d.source=? AND d.source_key=? AND c.code=? AND d.record_hash=?",
                          (material["source"], key, code, material["record_hash"])).require_one()
            item = DecodeCompanyDisclosure(row)
            if (item.url != material["url"] or item.title != material["title"]
                or item.published_at.isoformat() != material["published_at"] or item.codes != (code,)
                or row["body_hash"] != bytes.fromhex(cast(str, material["body_hash"]))):
                raise ValueError("company_field_recovery_isolated_material_changed")
        _recovery_output_path(raw_files.directory(item))
        # Durable intent consumes this object's allowance even if the process dies before observation.
        observations.append(_RecoveryObservation(key, code, "request_intent", None, failure="request_outcome_unknown"))
        _recovery_checkpoint(checkpoint_path, manifest_hash, True, observations)
        start_attempt = len(http_attempts)
        same_pdf = same_body = recovered = False
        failure = None
        try:
            source.download_document(item, attempt_limit=1)
            local = raw_files.local(item, check=check)
            if local is None or material["pdf_hash"] is None or local.payload_hash.hex() != material["pdf_hash"]:
                raise ValueError("company_field_recovery_pdf_hash_changed")
            same_pdf = True
            try:
                extraction = source.extract_document(item)
            except CompanyDocumentFailure as exc:
                if exc.reason != "disclosure_document_ocr_required":
                    raise
                extraction = source.extract_document(item, route="ocr")
            if hashlib.sha256(extraction.text.encode()).hexdigest() != material["body_hash"]:
                raise ValueError("company_field_recovery_body_hash_changed")
            same_body = True
            with (
                SqliteSession(collection, sqlite_policy=sqlite_policy, cancel_requested=cancelled) as raw,
                SqliteSession(formal_path, SqliteConnectionOptions(read_only=True), sqlite_policy=sqlite_policy,
                              cancel_requested=cancelled) as formal,
                SqliteTransaction(raw, immediate=True), SqliteTransaction(formal),
            ):
                recovered = RecoverRequiredCompanyText(
                    raw, formal, key, extraction.text, observed_at, bytes.fromhex(cast(str, manifest["extraction_hash"])),
                    recovered_text, check, partial(raw_files.reserve, local.directory, analysis=True),
                )
        except InterruptedError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            failure = CompanyFailureCode(exc)
        requests = len(http_attempts) - start_attempt
        if requests > 1:
            raise RuntimeError("company_field_recovery_transport_attempt_limit_exceeded")
        observations[-1] = _RecoveryObservation(key, code, "recovered" if recovered else "unrecovered", requests,
                                               same_pdf, same_body, recovered, failure)
        _recovery_checkpoint(checkpoint_path, manifest_hash, True, observations)
        attempted_now += 1
    recovered_hashes = frozenset(bytes.fromhex(cast(str, material["body_hash"])) for material in materials
                                 if any(item.source_key == material["source_key"] and item.recovered for item in observations))
    if not cancelled():
        _recovery_reprove(collection, formal_path, source_text, recovered_text, recovered_hashes, codes,
                          sqlite_policy, event_policy, observed_at, deadline)
    final_fields: tuple[CompanyFieldConclusion, ...] = ()
    if not cancelled():
        with SqliteSession(formal_path, SqliteConnectionOptions(read_only=True), sqlite_policy=sqlite_policy,
                           cancel_requested=cancelled) as formal:
            final_fields = ReadCompanyFieldResults(formal, codes)
    field_results: list[dict[str, object]] = []
    for target in _recovery_entries(manifest["targets"]):
        field = next((field for field in final_fields if (field.code, field.event_id, field.field)
                      == (target["code"], target["event_id"], target["field"])), None)
        reproved = field is not None and field.state == "verified" and _field_value(field) == target["value"]
        field_results.append({"code": target["code"], "event_id": target["event_id"], "field": target["field"],
                              "state": field.state if field else "missing", "reason": field.reason if field else "current_field_missing",
                              "reproved": reproved})
    source_unchanged = before == (_recovery_identity(collection_path), _recovery_identity(facts_path))
    complete = len(observations) == len(materials) and all(item.requests is not None for item in observations)
    reproved_count = sum(item["reproved"] is True for item in field_results)
    summary: dict[str, object] = {
        "scope": "exact_published_field_proof_materials", "complete": complete,
        "read_only_source": True, "isolated_copy": True, "source_data_writes": 0,
        "source_publication_hash": manifest["publication_hash"], "field_rule_hash": manifest["field_rule_hash"],
        "network_requests": sum(item.requests or 0 for item in observations),
        "network_request_count_complete": all(item.requests is not None for item in observations),
        "planned_objects": len(materials), "attempted_objects": len(observations),
        "exact_text_objects": sum(item.same_body for item in observations),
        "recovered_objects": sum(item.recovered for item in observations),
        "target_fields": len(targets), "reproved_fields": reproved_count, "unreproved_fields": len(targets) - reproved_count,
        "source_unchanged": source_unchanged, "budget_exhausted": cancelled(),
        "material_observations": [item.projection() for item in observations], "field_results": field_results,
        "production_adopted": False, "field_correctness_verified": False,
        "full_market_verified": False, "service_http_checked": False,
    }
    passed = (complete and source_unchanged and not cancelled() and reproved_count == len(targets)
              and all(item.recovered for item in observations))
    report: dict[str, object] = {"schema_version": "company-resume", "status": "passed" if passed else "degraded",
                                "summary": {"field_proof_recovery": summary},
                                "findings": [] if passed else ["company_field_proof_recovery_incomplete"]}
    AtomicWriteJson(directory / "proof-recovery.json", report)
    return report


@dataclass(frozen=True)
class _DownloadVariant:
    name: str
    timeout: float
    browser_headers: bool
    io_timeout: float | None = None
    expanded: bool = False


def AuditCompanyDocumentDownloads(
    path: Path, *, budget_seconds: float, sqlite_policy: SqliteIoPolicy, directory: Path | None = None,
) -> dict[str, object]:
    """An explicit directory selects capacity-failure recovery without active database writes."""
    if directory is not None:
        return _recover_capacity_documents(path, directory, budget_seconds, sqlite_policy)
    configuration = LoadCompanySyncConfiguration(TOOL_ROOT)
    deadline = time.monotonic() + budget_seconds
    with SqliteSession(path, SqliteConnectionOptions(read_only=True, timeout_seconds=3), sqlite_policy=sqlite_policy) as connection:
        samples = tuple(connection.run(
            "SELECT d.id,d.url FROM document_retries r JOIN disclosures d "
            "ON d.id=r.disclosure_id AND d.record_hash=r.record_hash "
            "WHERE r.failure IN ('timeout','HttpConnectionError','HttpError','http_transport_failed') "
            "AND EXISTS(SELECT 1 FROM disclosure_codes c JOIN company_population p ON p.code=c.code "
            "WHERE c.disclosure_id=d.id) ORDER BY r.attempted_at DESC,d.id LIMIT 2"
        ))
        encrypted = tuple(connection.run(
            "SELECT d.id,d.url,r.payload_hash FROM document_retries r JOIN disclosures d "
            "ON d.id=r.disclosure_id AND d.record_hash=r.record_hash "
            "WHERE r.failure='disclosure_document_encrypted' ORDER BY d.id LIMIT 3"
        ))
    variants = (
        _DownloadVariant("legacy_8_seconds", configuration.source_timeout_seconds, False),
        _DownloadVariant("browser_headers", configuration.source_timeout_seconds, True),
        _DownloadVariant("browser_headers_longer", 30.0, True),
        _DownloadVariant("legacy_repeat", configuration.source_timeout_seconds, False),
        _DownloadVariant(
            "production_fixed", configuration.document_download_timeout_seconds, False,
            configuration.source_timeout_seconds, True,
        ),
    )
    observations: list[dict[str, object]] = []
    encrypted_observations: list[dict[str, object]] = []
    for sample in samples:
        if not sample[1].startswith("https://static.cninfo.com.cn/finalpage/"):
            raise ValueError("company_document_audit_url_invalid")
        for variant in variants:
            if time.monotonic() >= deadline:
                break
            if observations:
                next_request = min(deadline, time.monotonic() + configuration.request_interval_seconds)
                while (delay := next_request - time.monotonic()) > 0:
                    time.sleep(min(0.25, delay))
                if time.monotonic() >= deadline:
                    break
            attempts: list[HttpAttempt] = []
            observation: dict[str, object] = {
                "disclosure_id": sample[0], "variant": variant.name,
                "timeout_seconds": variant.timeout, "passed": False,
                "io_timeout_seconds": variant.io_timeout,
            }
            headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.cninfo.com.cn/"} if variant.browser_headers else None
            with OpenExternalHttp(observe=attempts.append) as session:
                try:
                    response = session.execute(
                        HttpRequest(
                            "GET", sample[1], variant.timeout, tuple((headers or {}).items()),
                            deadline=min(deadline, time.monotonic() + variant.timeout),
                            max_response_bytes=(configuration.extended_document_limits if variant.expanded else configuration.document_limits).max_bytes,
                            cancel_requested=lambda: time.monotonic() >= deadline,
                            io_timeout_seconds=variant.io_timeout,
                        )
                    )
                    is_pdf = response.content.startswith(b"%PDF-")
                    observation.update({
                        "passed": response.status_code == 200 and is_pdf,
                        "is_pdf": is_pdf, "payload_hash": hashlib.sha256(response.content).hexdigest(),
                    })
                except HttpError as exc:
                    observation.update({
                        "failure": type(exc).__name__,
                        "cause": type(exc.__cause__).__name__ if exc.__cause__ is not None else None,
                    })
            observation["attempts"] = [{
                "seconds": round(attempt.elapsed_seconds, 3), "response_bytes": attempt.response_bytes,
                "status_code": attempt.status_code, "failure": attempt.failure,
                "phase": "before_response_headers" if attempt.status_code is None else "response_body",
            } for attempt in attempts]
            observations.append(observation)
    for sample in encrypted:
        if time.monotonic() >= deadline:
            break
        if not sample[1].startswith("https://static.cninfo.com.cn/finalpage/"):
            raise ValueError("company_document_audit_url_invalid")
        observation = {"disclosure_id": sample[0], "read_only": True}
        try:
            with OpenExternalHttp() as session:
                response = session.execute(HttpRequest(
                    "GET", sample[1], configuration.document_download_timeout_seconds,
                    deadline=min(deadline, time.monotonic() + configuration.document_download_timeout_seconds),
                    max_response_bytes=configuration.extended_document_limits.max_bytes,
                    io_timeout_seconds=configuration.source_timeout_seconds,
                ))
            observation["status_code"] = response.status_code
            observation["same_pdf"] = hashlib.sha256(response.content).digest() == sample[2]
            if response.status_code != 200 or not response.content.startswith(b"%PDF-"):
                observation["failure"] = "pdf_unavailable"
            else:
                with tempfile.TemporaryDirectory(prefix="trader-encrypted-pdf-audit-") as temporary:
                    pdf = Path(temporary) / "input.pdf"
                    output = Path(temporary) / "text.txt"
                    pdf.write_bytes(response.content)
                    metadata = subprocess.run(
                        ("pdfinfo", str(pdf)), capture_output=True, timeout=10, check=False,
                    )
                    lines = metadata.stdout.decode("utf-8", errors="replace").splitlines()
                    observation["metadata_exit_code"] = metadata.returncode
                    observation["encrypted"] = next((line.split(":", 1)[1].strip() for line in lines if line.startswith("Encrypted:")), None)
                    observation["pages"] = next((line.split(":", 1)[1].strip() for line in lines if line.startswith("Pages:")), None)
                    observation["password_required"] = b"Incorrect password" in metadata.stderr
                    extracted = subprocess.run(
                        ("pdftotext", "-enc", "UTF-8", str(pdf), str(output)),
                        capture_output=True, timeout=10, check=False,
                    )
                    observation["text_exit_code"] = extracted.returncode
                    observation["text_bytes"] = output.stat().st_size if output.exists() else 0
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            observation["failure"] = type(exc).__name__
        encrypted_observations.append(observation)
    complete = bool(samples) and len(observations) == len(samples) * len(variants)
    corrected = [row for row in observations if row["variant"] == "production_fixed"]
    passed = complete and len(corrected) == len(samples) and all(row["passed"] for row in corrected)
    return {
        "schema_version": "company-resume", "status": "passed" if passed else "degraded",
        "summary": {"document_download_audit": {
            "read_only": True, "sample_size": len(samples), "complete": complete,
            "production_passed": passed,
            "observations": observations,
            "encrypted_observations": encrypted_observations,
        }},
        "findings": [] if passed else ["company_document_download_incomplete"],
    }


def _capacity_samples(connection: SqliteConnection) -> tuple[Disclosure, ...]:
    if connection.run("SELECT 1 FROM sqlite_master WHERE name='download_failure'").fetchone() is None:
        return ()
    selected: dict[str, Disclosure] = {}
    for row in connection.run(
        "SELECT diagnostic FROM download_failure WHERE reason='company_raw_capacity_exhausted' ORDER BY id DESC LIMIT 50"
    ):
        identity = ParseCompanyFailureIdentity(row[0])
        if identity is None or identity.source_key in selected:
            continue
        document = connection.run(
            "SELECT d.*,MIN(c.code) AS code FROM disclosures d JOIN disclosure_codes c ON c.disclosure_id=d.id "
            "JOIN company_population p ON p.code=c.code WHERE d.source='cninfo' AND d.source_key=? "
            "AND d.record_hash=? AND d.body_checked=0 GROUP BY d.id",
            (identity.source_key, identity.record_hash),
        ).fetchone()
        if document is not None:
            selected[identity.source_key] = DecodeCompanyDisclosure(document)
    return tuple(selected.values())


def _recover_capacity_documents(
    path: Path, directory: Path, budget_seconds: float, sqlite_policy: SqliteIoPolicy,
) -> dict[str, object]:
    RequireIsolatedReplay(directory, (TOOL_ROOT, path.parent))
    if not directory.is_absolute() or any(item.is_symlink() for item in (directory, *directory.parents)):
        raise ValueError("company_document_recovery_path_invalid")
    if directory.exists() and (not directory.is_dir() or any(directory.iterdir())):
        raise ValueError("company_document_recovery_new_directory_required")
    deadline = time.monotonic() + budget_seconds
    def cancelled() -> bool:
        return time.monotonic() >= deadline
    options = SqliteConnectionOptions(read_only=True, timeout_seconds=3)
    with SqliteSession(path, options, sqlite_policy=sqlite_policy, cancel_requested=cancelled) as connection:
        pending = _capacity_samples(connection)
    samples = pending[:2]
    directory.mkdir(parents=True, exist_ok=True)
    configuration = LoadCompanySyncConfiguration(TOOL_ROOT)
    capability = DetectCompanyOcr(configuration.document_ocr_languages)
    text = DetectCompanyText()
    policies = BuildCompanyDocumentPolicies(configuration, capability.identity, text_identity=text.identity)
    raw = CompanyRawFiles(
        directory / "inputs",
        capacity=configuration.raw_capacity_bytes,
        analysis_reserve=configuration.raw_analysis_reserve_bytes,
        free_reserve=configuration.temporary_disk_reserve_bytes,
        maximum_document_bytes=configuration.extended_document_limits.max_bytes,
    )
    engine = CompanyOcrEngine(
        capability,
        timeout_seconds=configuration.document_ocr_timeout_seconds,
        page_timeout_seconds=configuration.document_ocr_page_timeout_seconds,
        max_pages=configuration.document_ocr_max_pages,
        dpi=configuration.document_ocr_dpi,
        cancel_requested=cancelled,
        retry_each_page=True,
    )
    observations: list[dict[str, object]] = []
    if samples:
        with OpenExternalHttp() as session:
            source = CninfoMarketDisclosureSource(
                session,
                monotonic=time.monotonic,
                sleeper=time.sleep,
                timeout_seconds=configuration.source_timeout_seconds,
                document_download_timeout_seconds=configuration.document_download_timeout_seconds,
                document_parse_timeout_seconds=configuration.document_parse_timeout_seconds,
                request_interval_seconds=configuration.request_interval_seconds,
                document_limits=configuration.document_limits,
                extended_document_limits=configuration.extended_document_limits,
                document_policies=policies,
                text_capability=text,
                ocr_engine=engine,
                cancel_requested=cancelled,
                raw_files=raw,
                text_reserve_bytes=configuration.text_task_reserve_bytes,
                ocr_reserve_bytes=configuration.ocr_task_reserve_bytes,
            )
            for item in samples:
                observation: dict[str, object] = {"record": item.identity, "variant": "isolated_recovery", "passed": False}
                try:
                    source.download_document(item)
                    try:
                        result = source.extract_document(item)
                    except CompanyDocumentFailure as exc:
                        if exc.reason != "disclosure_document_ocr_required":
                            raise
                        result = source.extract_document(item, route="ocr")
                    observation.update(passed=True, text_extracted=True, characters=len(result.text),
                                       payload_hash=result.payload_hash.hex())
                except (OSError, RuntimeError, ValueError) as exc:
                    observation["failure"] = CompanyFailureCode(exc)
                observations.append(observation)
                if cancelled():
                    break
    complete = bool(samples) and len(observations) == len(samples) and len(pending) <= len(samples)
    passed = complete and all(row["passed"] for row in observations)
    report: dict[str, object] = {
        "schema_version": "company-resume", "status": "passed" if passed else "degraded",
        "summary": {"document_download_audit": {
            "read_only": True, "active_writes": False, "pending_records": len(pending), "sample_size": len(samples),
            "complete": complete, "production_passed": passed, "observations": observations,
        }},
        "findings": [] if passed else ["company_document_recovery_incomplete"],
    }
    AtomicWriteJson(directory / "document-recovery.json", report)
    return report
