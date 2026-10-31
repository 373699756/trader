"""Aggregate local company output and receipt evidence without reading document bodies."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy

import time
from datetime import datetime, timedelta
from pathlib import Path

from trader.download.infra.company_facts_index import ReadCompanyPublicationIdentity
from trader.download.infra.company_facts_protocol import (
    REVIEW_REASONS,
    REVIEW_SCOPES,
    DecodeCompanyDate,
    DecodeCompanyLabel,
    EncodeCompanyDate,
    EncodeCompanyTime,
)
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession, SqliteTransaction


def AuditCompanyPublication(
    path: Path, facts_path: Path | None, *, now: datetime, deadline: float, sqlite_policy: SqliteIoPolicy
) -> dict[str, object]:
    """Two read snapshots, with concurrent changes rejected; not a source completeness certificate."""
    if facts_path is None or not facts_path.exists():
        return {"available": False, "findings": ["company_formal_unavailable"]}
    options = SqliteConnectionOptions(read_only=True, timeout_seconds=5)
    findings: list[str] = []
    with (
        SqliteSession(path, options, cancel_requested=lambda: time.monotonic() >= deadline, sqlite_policy=sqlite_policy) as raw,
        SqliteSession(facts_path, options, cancel_requested=lambda: time.monotonic() >= deadline, sqlite_policy=sqlite_policy) as formal,
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
                receipt_rows = raw.run(
                    "SELECT COUNT(*),COALESCE(SUM(d.body_retained=0),0) FROM disclosures d "
                    "WHERE d.kind='announcement' AND d.body_checked=1 AND EXISTS("
                    "SELECT 1 FROM disclosure_codes c JOIN company_population u ON u.code=c.code "
                    "WHERE c.disclosure_id=d.id AND NOT EXISTS(SELECT 1 FROM document_publications p "
                    "WHERE p.disclosure_id=d.id AND p.code=c.code AND p.record_hash=d.record_hash "
                    "AND p.extraction_hash=?))",
                    (identity.extraction_hash,),
                ).require_one()
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
            }
        stable = versions == (
            raw.run("PRAGMA data_version").require_one()[0], formal.run("PRAGMA data_version").require_one()[0]
        )
        result["stable_read"] = stable
        if not stable:
            findings.append("company_audit_concurrent_change")
        return result
