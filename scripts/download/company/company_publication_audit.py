"""Aggregate local company output and receipt evidence without reading document bodies."""

from __future__ import annotations

from scripts.download.company.company_field_audit import AuditCompanyFields

from trader.infra.sqlite_policy import SqliteIoPolicy

import time
import math
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from trader.download.infra.company_facts_index import ReadCompanyPublicationIdentity
from trader.download.infra.company_financial_input_use import ReadUnusedCompanyFinancialInputs
from trader.download.infra.company_fact_rows import ReadCompanyFactRows
from trader.download.infra.company_facts_reader import SQLiteCompanyEvidenceReader
from trader.download.infra.company_field_result_rows import ReadCompanyFieldResults
from trader.download.infra.company_document_link_rows import ReadCompanyDocumentLinks
from trader.download.domain.company_events import CompanyEventPolicy
from trader.download.domain.company_facts import CompanyFacts
from trader.download.infra.company_facts_protocol import (
    REVIEW_REASONS,
    REVIEW_SCOPES,
    DecodeCompanyDate,
    DecodeCompanyLabel,
    EncodeCompanyDate,
    EncodeCompanyTime,
    CompanyExtractionHash,
)
from trader.infra.sqlite_connection import SqliteConnection, SqliteConnectionOptions, SqliteSession, SqliteTransaction


class CompanyConsumerAuditBudgetExceeded(TimeoutError):
    """The production reader observed this diagnostic's deadline cancellation."""


def AuditCompanyConsumer(
    facts_path: Path,
    *,
    now: datetime,
    budget_seconds: float,
    sqlite_policy: SqliteIoPolicy,
    event_policy: CompanyEventPolicy,
    http_configuration: Path | None = None,
) -> dict[str, object]:
    """Verify only the formal read contract; never plan repairs or inspect collection data."""
    if (
        isinstance(budget_seconds, bool) or not math.isfinite(budget_seconds)
        or not 0 < budget_seconds <= 180 or now.utcoffset() is None
    ):
        raise ValueError("company_consumer_audit_budget_or_time_invalid")
    deadline = time.monotonic() + budget_seconds
    if http_configuration is not None:
        from trader.infra.settings import LoadRuntimeSettings

        if LoadRuntimeSettings(http_configuration).data_paths.company.resolve(strict=True) != facts_path.resolve(strict=True):
            raise ValueError("company_consumer_http_database_mismatch")
    expired = False

    def cancel() -> bool:
        nonlocal expired
        if time.monotonic() >= deadline:
            expired = True
        return expired

    def check() -> None:
        if cancel():
            raise CompanyConsumerAuditBudgetExceeded("company_consumer_audit_budget_exhausted")

    publication_hash: str | None = None
    extraction_hash: str | None = None
    published_at: str | None = None
    consumer: dict[str, object] = {"checked": False, "http_checked": False}
    stable = False
    findings: list[str] = []
    codes: list[str] = []
    http_audit: dict[str, object] | None = None
    try:
        check()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            cancel()
            raise CompanyConsumerAuditBudgetExceeded("company_consumer_audit_budget_exhausted")
        policy = replace(sqlite_policy, connection_timeout_seconds=min(sqlite_policy.connection_timeout_seconds, remaining))
        with SqliteSession(
            facts_path, SqliteConnectionOptions(read_only=True), cancel_requested=cancel, sqlite_policy=policy,
        ) as formal:
            version = formal.run("PRAGMA data_version").require_one()[0]
            with SqliteTransaction(formal):
                identity = ReadCompanyPublicationIdentity(formal)
                if identity is None:
                    findings.append("company_publication_missing")
                else:
                    publication_hash = identity.content_hash.hex()
                    extraction_hash = identity.extraction_hash.hex()
                    published_at = identity.published_at.isoformat()
                    for row in formal.run("SELECT code FROM company_facts ORDER BY code"):
                        check()
                        if len(codes) >= 12_000:
                            raise ValueError("company_consumer_audit_population_exceeded")
                        codes.append(row[0])
                    consumer = _consumer_read(formal, facts_path, tuple(codes), event_policy, policy, cancel)
            stable = version == formal.run("PRAGMA data_version").require_one()[0]
        check()
        if not stable:
            findings.append("company_audit_concurrent_change")
        if consumer.get("consistent") is not True:
            findings.append("company_consumer_read_not_verified")
    except (sqlite3.OperationalError, CompanyConsumerAuditBudgetExceeded) as exc:
        if not expired or isinstance(exc, sqlite3.OperationalError) and str(exc) != "interrupted":
            raise
        findings = ["company_consumer_audit_budget_exhausted"]
        if consumer.get("failure") is not None:
            findings.append("company_consumer_read_not_verified")
        else:
            consumer = {"checked": False, "http_checked": False}
        stable = False
    if http_configuration is not None and stable and consumer.get("consistent") is True and not findings:
        from scripts.download.company.company_http_audit import AuditCompanyHttp

        if cancel():
            findings.append("company_consumer_audit_budget_exhausted")
            stable = False
        else:
            if publication_hash is None:
                raise ValueError("company_consumer_publication_missing")
            try:
                http_audit = AuditCompanyHttp(http_configuration, expected_publication_hash=publication_hash,
                    codes=tuple(codes), budget_seconds=deadline - time.monotonic())
            except TimeoutError:
                if not cancel():
                    raise
                findings.append("company_consumer_audit_budget_exhausted")
            else:
                if http_audit.get("passed") is not True:
                    findings.append("company_consumer_http_not_verified")
            expired = cancel()
    http_checked = http_audit is not None and http_audit.get("passed") is True
    complete = stable and consumer.get("consistent") is True and not findings and not expired
    return {
        "schema_version": "company-resume",
        "status": "passed" if complete else "failed" if expired else "degraded",
        "summary": {"consumer_audit": {
            "read_only": True, "scope": "formal_reader_and_document_http" if http_configuration is not None else "formal_reader_consistency_only", "observed_at": now.isoformat(),
            "complete": complete, "budget_exhausted": expired, "stable_read": stable,
            "publication_hash": publication_hash, "extraction_hash": extraction_hash, "published_at": published_at,
            "consumer_read": consumer, "collection_checked": False, "recovery_planned": False,
            "field_correctness_verified": False, "full_market_verified": False, "http_checked": http_checked,
            **({"http_audit": http_audit} if http_configuration is not None else {}),
        }},
        "findings": findings,
    }


def _consumer_read(
    formal: SqliteConnection,
    facts_path: Path,
    codes: tuple[str, ...],
    event_policy: CompanyEventPolicy,
    sqlite_policy: SqliteIoPolicy,
    cancel_requested: Callable[[], bool],
) -> dict[str, object]:
    """Compare the real production reader with the caller's pinned SQL snapshot."""
    identity = ReadCompanyPublicationIdentity(formal)
    if identity is None or not codes or len(codes) > 12000:
        return {"checked": False, "http_checked": False, "failure": "company_consumer_population_or_publication_missing"}
    expected_facts = ReadCompanyFactRows(formal, codes)
    expected_fields = ReadCompanyFieldResults(formal, codes)
    expected_links = ReadCompanyDocumentLinks(formal, codes)
    reader = SQLiteCompanyEvidenceReader(facts_path, event_policy=event_policy, sqlite_policy=sqlite_policy)
    actual_facts: list[CompanyFacts] = []
    try:
        reader.visit(codes, actual_facts.append, cancel_requested=cancel_requested)
        actual_fields = reader.read_fields(codes, cancel_requested=cancel_requested)
        actual_links = reader.read_document_links(codes, cancel_requested=cancel_requested)
        actual_identity = reader.publication_identity(cancel_requested=cancel_requested)
        if cancel_requested():
            raise InterruptedError("company_consumer_audit_cancelled")
    except InterruptedError as exc:
        if str(exc) in {
            "company_preload_cancelled", "company_field_read_cancelled", "company_observation_cancelled",
            "company_consumer_audit_cancelled",
            "company_document_link_read_cancelled",
        } and cancel_requested():
            raise CompanyConsumerAuditBudgetExceeded("company_consumer_audit_budget_exhausted") from exc
        raise
    except (OSError, RuntimeError, ValueError):
        return {"checked": False, "http_checked": False, "failure": "company_consumer_unreadable"}
    same_facts = tuple(actual_facts) == expected_facts
    same_fields = actual_fields.results == expected_fields
    same_links = actual_links.links == expected_links
    same_identity = actual_fields.publication_hash == identity.content_hash.hex() == actual_identity == actual_links.publication_hash
    return {
        "checked": True, "http_checked": False, "stocks": len(actual_facts), "fields": len(actual_fields.results),
        "facts_equal": same_facts, "fields_equal": same_fields, "publication_equal": same_identity,
        "document_links": len(actual_links.links), "document_links_equal": same_links,
        "consistent": same_facts and same_fields and same_links and same_identity,
    }


def AuditCompanyPublication(
    path: Path, facts_path: Path | None, *, now: datetime, deadline: float, sqlite_policy: SqliteIoPolicy,
    cancel_requested: Callable[[], bool] | None = None,
    event_policy: CompanyEventPolicy | None = None,
) -> dict[str, object]:
    """Two read snapshots, with concurrent changes rejected; not a source completeness certificate."""
    if facts_path is None or not facts_path.exists():
        return {"available": False, "findings": ["company_formal_unavailable"]}
    options = SqliteConnectionOptions(read_only=True, timeout_seconds=5)
    findings: list[str] = []
    cancel = cancel_requested if cancel_requested is not None else lambda: time.monotonic() >= deadline
    with (
        SqliteSession(path, options, cancel_requested=cancel, sqlite_policy=sqlite_policy) as raw,
        SqliteSession(facts_path, options, cancel_requested=cancel, sqlite_policy=sqlite_policy) as formal,
    ):
        versions = (raw.run("PRAGMA data_version").require_one()[0], formal.run("PRAGMA data_version").require_one()[0])
        with SqliteTransaction(raw), SqliteTransaction(formal):
            identity = ReadCompanyPublicationIdentity(formal)
            population = {row[0] for row in raw.run("SELECT code FROM company_population")}
            published = {row[0] for row in formal.run("SELECT code FROM company_facts")}
            dirty_codes = {row[0] for row in raw.run("SELECT code FROM company_dirty_codes")}
            dirty = len(dirty_codes.intersection(population))
            states = [
                {"component": component, "state": label, "stocks": row[1]}
                for component, column in (
                    ("financial", "finance_state"), ("pledge", "pledge_state"),
                    ("unlock", "unlock_state"), ("announcement", "announcement_state"),
                )
                for row in formal.run(f"SELECT {column},COUNT(*) FROM company_facts GROUP BY {column}")
                for label in (("unavailable", "partial", "complete")[row[0]],)
            ]
            coverage = formal.run(
                "SELECT MIN(announcement_start_date),MAX(announcement_start_date),"
                "MIN(announcement_end_date),MAX(announcement_end_date),"
                "MIN(unlock_end_date),MAX(unlock_end_date),"
                "COALESCE(SUM(unlock_state<>2 OR unlock_end_date IS NULL OR unlock_end_date<?),0),"
                "COALESCE(SUM(pending_docs),0) FROM company_facts",
                (EncodeCompanyDate(now.date() + timedelta(days=180)),),
            ).require_one()
            reviews = [
                {"scope": DecodeCompanyLabel(REVIEW_SCOPES, row[0]),
                 "reason": DecodeCompanyLabel(REVIEW_REASONS, row[1]), "records": row[2], "stocks": row[3]}
                for row in formal.run(
                    "SELECT scope,reason,COUNT(*),COUNT(DISTINCT code) FROM company_review GROUP BY scope,reason"
                )
            ]
            risks = formal.run(
                "SELECT COUNT(*),COUNT(DISTINCT code),"
                "COALESCE(SUM(first_disclosed_at<?),0) FROM company_risk_event WHERE status=1",
                (EncodeCompanyTime(now - timedelta(days=365)),),
            ).require_one()
            missing_risk_evidence = formal.run(
                "SELECT COUNT(*) FROM company_risk_event e WHERE NOT EXISTS("
                "SELECT 1 FROM company_risk_evidence r WHERE r.risk_id=e.id)"
            ).require_one()[0]
            receipts: dict[str, object] = {"checked": False}
            if identity is not None:
                unused = ReadUnusedCompanyFinancialInputs(
                    raw,
                    formal,
                    tuple(sorted(population)),
                    now,
                    CompanyExtractionHash(event_policy),
                    cancel_requested=cancel,
                ) if event_policy is not None else frozenset()
                missing: set[int] = set()
                released: set[int] = set()
                for row in raw.run(
                    "SELECT d.id,d.source,d.source_key,d.record_hash,d.body_retained,c.code,d.body_hash FROM disclosures d "
                    "JOIN disclosure_codes c ON c.disclosure_id=d.id JOIN company_population u ON u.code=c.code "
                    "WHERE d.kind='announcement' AND d.body_checked=1"
                ):
                    if row[6] is None or formal.run(
                        "SELECT 1 FROM company_input_receipt WHERE source=? AND source_key=? AND code=? AND record_hash=? "
                        "AND body_hash IS ? AND extraction_hash=? AND outcome='valid'",
                        (row[1], row[2], row[5], row[3], row[6], identity.extraction_hash),
                    ).fetchone() is None:
                        if row[4] and row[0] in unused:
                            continue
                        missing.add(row[0])
                        if not row[4]:
                            released.add(row[0])
                receipt_rows = len(missing), len(released)
                receipts = {"checked": True, "awaiting_publication_documents": receipt_rows[0],
                            "released_without_receipts": receipt_rows[1]}
                if receipt_rows[1]:
                    findings.append("company_released_body_receipt_missing")
            else:
                findings.append("company_publication_missing")
            if not population:
                findings.append("company_population_empty")
            if population - published:
                findings.append("company_population_unpublished")
            if dirty:
                findings.append("company_publication_dirty")
            if any(row["state"] != "complete" and row["stocks"] for row in states):
                findings.append("company_components_incomplete")
            if coverage[6]:
                findings.append("company_unlock_180_days_incomplete")
            if reviews:
                findings.append("company_reviews_pending")
            if missing_risk_evidence:
                findings.append("company_risk_evidence_missing")
            field_audit = AuditCompanyFields(formal, path, raw=raw, observed_at=now)
            field_findings = field_audit["findings"]
            if not isinstance(field_findings, list):
                raise ValueError("company_field_findings_invalid")
            findings.extend(field_findings)
            consumer = _consumer_read(formal, facts_path, tuple(sorted(published)), event_policy, sqlite_policy, cancel) if (
                event_policy is not None
            ) else {"checked": False, "http_checked": False, "failure": "company_consumer_policy_not_supplied"}
            if event_policy is not None and consumer.get("consistent") is not True:
                findings.append("company_consumer_read_not_verified")
            result: dict[str, object] = {
                "available": True,
                "observed_at": now.isoformat(),
                "scope": "local_records_not_source_completeness",
                "publication_hash": identity.content_hash.hex() if identity else None,
                "extraction_hash": identity.extraction_hash.hex() if identity else None,
                "published_at": identity.published_at.isoformat() if identity else None,
                "population_stocks": len(population), "published_stocks": len(published),
                "unpublished_stocks": len(population - published), "dirty_stocks": dirty,
                "outside_population_dirty_stocks": len(dirty_codes - population),
                "component_states": states,
                "coverage": {
                    **{name: str(DecodeCompanyDate(value)) if value is not None else None
                       for name, value in zip(("announcement_start_min", "announcement_start_max",
                                               "announcement_end_min", "announcement_end_max",
                                               "unlock_end_min", "unlock_end_max"), coverage[:6], strict=True)},
                    "unlock_required_end": (now.date() + timedelta(days=180)).isoformat(),
                    "unlock_incomplete_stocks": coverage[6], "pending_document_references": coverage[7],
                },
                "reviews": reviews,
                "ongoing_risks": {"events": risks[0], "stocks": risks[1], "older_than_year": risks[2],
                                  "missing_evidence_events": missing_risk_evidence},
                "receipts": receipts,
                "findings": findings,
                "field_audit": field_audit,
                "consumer_read": consumer,
            }
        stable = versions == (
            raw.run("PRAGMA data_version").require_one()[0], formal.run("PRAGMA data_version").require_one()[0]
        )
        result["stable_read"] = stable
        if not stable:
            findings.append("company_audit_concurrent_change")
        return result
