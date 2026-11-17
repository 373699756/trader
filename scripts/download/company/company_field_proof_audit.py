"""Reprove published field values with current rules and exact local evidence, without writes."""

from __future__ import annotations

import math
import sqlite3
import time
from collections import Counter
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from scripts.core.paths import PROJECT_ROOT
from trader.download.domain.company_events import CompanyEventPolicy
from trader.download.domain.company_field_results import CompanyFieldConclusion
from trader.download.infra.company_document_files import CompanyDocumentFiles
from trader.download.infra.company_fact_rows import ReadCompanyFactRows
from trader.download.infra.company_facts_index import ReadCompanyPublicationIdentity
from trader.download.infra.company_facts_protocol import CompanyExtractionHash
from trader.download.infra.company_facts_reader import SQLiteCompanyEvidenceReader
from trader.download.infra.company_field_publication import CompanyFieldProofRulesHash, PrepareCompanyFieldResults
from trader.download.infra.company_field_result_rows import ReadCompanyFieldResults
from trader.download.infra.download_settings import LoadCompanySyncConfiguration
from trader.infra.sqlite_connection import SqliteConnection, SqliteConnectionOptions, SqliteSession, SqliteTransaction
from trader.infra.sqlite_policy import SqliteIoPolicy


class CompanyFieldProofAuditBudgetExceeded(InterruptedError):
    """The explicit reproof diagnostic exhausted its own deadline."""


def _unreproved_basis(
    raw: SqliteConnection,
    formal: SqliteConnection,
    previous: CompanyFieldConclusion,
    extraction_hash: bytes,
    text_root: Path,
) -> str:
    """Explain missing old-proof prerequisites; this projection never grants a proof."""
    for proof in previous.evidence:
        rows = raw.run(
            "SELECT d.record_hash,d.body_hash,d.published_at,d.body_retained FROM disclosures d "
            "JOIN disclosure_codes c ON c.disclosure_id=d.id WHERE d.source=? AND d.source_key=? AND c.code=? "
            "AND (SELECT COUNT(*) FROM disclosure_codes m WHERE m.disclosure_id=d.id)=1",
            (proof.source, proof.source_key, previous.code),
        ).fetchall()
        if len(rows) != 1:
            return "source_missing_or_ambiguous"
        if tuple(rows[0])[:3] != (proof.record_hash, proof.body_hash, proof.published_at.isoformat()):
            return "material_identity_changed"
        if formal.run(
            "SELECT 1 FROM company_input_receipt WHERE source=? AND source_key=? AND code=? AND record_hash=? "
            "AND body_hash=? AND extraction_hash=? AND outcome='valid'",
            (proof.source, proof.source_key, previous.code, proof.record_hash, proof.body_hash, extraction_hash),
        ).fetchone() is None:
            return "current_valid_receipt_missing"
        key = proof.body_hash.hex()
        available = (text_root / key[:2] / (key + ".txt.gz")).is_file()
        if not rows[0][3]:
            return "released_text_present" if available else "original_body_released"
        if not available:
            return "original_body_missing"
    return "current_rule_not_reproduced"


def AuditCompanyFieldProofs(
    collection_path: Path,
    facts_path: Path,
    *,
    now: datetime,
    budget_seconds: float,
    sqlite_policy: SqliteIoPolicy,
    event_policy: CompanyEventPolicy,
) -> dict[str, object]:
    """Ignore cached proofs and attempts; never submit the prepared conclusions or receipts.

    Only previously verified fields are acceptance targets. The original fact's
    observation time remains the visibility cutoff; this audit cannot renew
    source age or certify independent field correctness.
    """
    if (isinstance(budget_seconds, bool) or not math.isfinite(budget_seconds)
        or not 0 < budget_seconds <= 10800 or now.utcoffset() is None):
        raise ValueError("company_field_proof_audit_budget_or_time_invalid")
    deadline = time.monotonic() + budget_seconds
    expired = False

    def cancel() -> bool:
        nonlocal expired
        expired = expired or time.monotonic() >= deadline
        return expired

    def check() -> None:
        if cancel():
            raise CompanyFieldProofAuditBudgetExceeded("company_field_proof_audit_budget_exhausted")

    configuration = LoadCompanySyncConfiguration(PROJECT_ROOT)
    documents = CompanyDocumentFiles(
        collection_path.parent / "text",
        max_text_characters=configuration.extended_document_limits.max_text_characters,
        max_bytes=configuration.temporary_text_capacity_bytes,
        reserve_bytes=configuration.temporary_disk_reserve_bytes,
    )
    material_failures: Counter[str] = Counter()
    material_reads = 0

    def read_document(digest: bytes) -> str:
        nonlocal material_reads
        check()
        material_reads += 1
        try:
            return documents.read(digest, check=check)
        except InterruptedError:
            raise
        except (OSError, ValueError, EOFError) as exc:
            material_failures[type(exc).__name__] += 1
            raise

    summary: dict[str, object] = {
        "read_only": True, "scope": "current_rules_published_verified_fields", "observed_at": now.isoformat(),
        "complete": False, "stable_read": False, "budget_exhausted": False,
        "publication_hash": None, "extraction_hash": None,
        "current_extraction_hash": CompanyExtractionHash(event_policy).hex(),
        "field_rule_hash": CompanyFieldProofRulesHash().hex(),
        "target_fields": 0, "checked_fields": 0, "reproved_fields": 0, "unreproved_fields": 0,
        "checked_stocks": 0, "material_reads": 0, "material_failures": {}, "unreproved_basis": {}, "items": [], "items_truncated": False,
        "network_requests": 0, "source_data_writes": 0, "production_adopted": False,
        "field_correctness_verified": False, "full_market_verified": False, "http_checked": False,
    }
    findings: list[str] = []
    checked_fields = reproved = checked_stocks = 0
    items: list[dict[str, object]] = []
    unreproved_basis: Counter[str] = Counter()
    stable = False
    try:
        check()
        policy = replace(sqlite_policy, connection_timeout_seconds=min(sqlite_policy.connection_timeout_seconds, budget_seconds))
        reader = SQLiteCompanyEvidenceReader(facts_path, event_policy=event_policy, sqlite_policy=policy)
        publication_hash = reader.read_fields((), cancel_requested=cancel).publication_hash
        options = SqliteConnectionOptions(read_only=True)
        with (
            SqliteSession(collection_path, options, sqlite_policy=policy, cancel_requested=cancel) as raw,
            SqliteSession(facts_path, options, sqlite_policy=policy, cancel_requested=cancel) as formal,
        ):
            before = (raw.run("PRAGMA data_version").require_one()[0], formal.run("PRAGMA data_version").require_one()[0])
            with SqliteTransaction(raw), SqliteTransaction(formal):
                identity = ReadCompanyPublicationIdentity(formal)
                if identity is None or publication_hash != identity.content_hash.hex():
                    raise ValueError("company_field_proof_audit_publication_changed_or_missing")
                summary.update(publication_hash=publication_hash, extraction_hash=identity.extraction_hash.hex())
                codes = tuple(row[0] for row in formal.run(
                    "SELECT DISTINCT code FROM company_field_result WHERE state='verified' ORDER BY code LIMIT 12001",
                ))
                if len(codes) > 12000:
                    raise ValueError("company_field_proof_audit_population_exceeded")
                target = formal.run("SELECT COUNT(*) FROM company_field_result WHERE state='verified'").require_one()[0]
                summary["target_fields"] = target
                if not target:
                    findings.append("company_field_proof_targets_empty")
                for code in codes:
                    check()
                    facts, = ReadCompanyFactRows(formal, (code,))
                    previous = ReadCompanyFieldResults(formal, (code,))
                    current = PrepareCompanyFieldResults(
                        raw, formal, facts, (), observed_at=facts.checked_at, extraction_hash=identity.extraction_hash,
                        event_policy=event_policy, read_document=read_document, revalidate=True,
                    )
                    for old in previous:
                        if old.state != "verified":
                            continue
                        result = next((item for item in current.results if (item.event_id, item.field) == (old.event_id, old.field)), None)
                        verified = (result is not None and result.state == "verified"
                                    and type(result.value) is type(old.value) and result.value == old.value)
                        checked_fields += 1
                        reproved += verified
                        if not verified:
                            basis = _unreproved_basis(raw, formal, old, identity.extraction_hash, documents.root)
                            unreproved_basis[basis] += 1
                            if len(items) < 100:
                                items.append({"code": code, "event_id": old.event_id, "field": old.field,
                                              "state": result.state if result is not None else "missing", "basis": basis,
                                              "reason": result.reason if result is not None else "field_absent"})
                    checked_stocks += 1
            stable = before == (raw.run("PRAGMA data_version").require_one()[0], formal.run("PRAGMA data_version").require_one()[0])
        check()
        if not stable:
            findings.append("company_audit_concurrent_change")
        if checked_fields != reproved:
            findings.append("company_published_fields_not_reproved")
        if checked_fields != summary["target_fields"]:
            findings.append("company_field_proof_targets_incomplete")
    except (sqlite3.OperationalError, InterruptedError, CompanyFieldProofAuditBudgetExceeded) as exc:
        if (not expired or isinstance(exc, sqlite3.OperationalError) and str(exc) != "interrupted"
            or isinstance(exc, InterruptedError) and not isinstance(exc, CompanyFieldProofAuditBudgetExceeded)
            and str(exc) != "company_field_read_cancelled"):
            raise
        findings.append("company_field_proof_audit_budget_exhausted")
    summary.update(
        complete=stable and not expired and not findings, stable_read=stable and not expired, budget_exhausted=expired,
        checked_fields=checked_fields, reproved_fields=reproved, unreproved_fields=checked_fields - reproved,
        checked_stocks=checked_stocks, material_reads=material_reads, material_failures=dict(material_failures),
        unreproved_basis=dict(unreproved_basis),
        items=items, items_truncated=checked_fields - reproved > len(items),
    )
    return {"schema_version": "company-resume", "status": "failed" if expired else "passed" if summary["complete"] else "degraded",
            "summary": {"field_proof_audit": summary}, "findings": findings}


__all__ = ["AuditCompanyFieldProofs", "CompanyFieldProofAuditBudgetExceeded"]
