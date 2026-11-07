"""Field repair tasks belong to the collection ledger, never to a second facts source."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from itertools import groupby
from pathlib import Path
from dataclasses import dataclass, replace
import hashlib

from trader.download.domain.company_announcement_numbers import ExtractCompanyAnnouncementNumbers
from trader.download.domain.company_events import (
    CompanyEventPolicy, CompanyEventStatus, CompanyPositiveEvent, CompanyPositiveStatus, CompanyPositiveType, CompanyReviewReason, CompanyRiskEvent, CompanyRiskType, CompanySubject, IncreaseMeasurements,
    RepurchaseMeasurements, RepurchasePurpose,
)
from trader.download.domain.company_evidence import CompanyDocument, CompanyDocumentFailureRules, Disclosure
from trader.download.domain.company_event_extraction import (
    ExtractCompanyEvents, ExtractCompanyLinkCandidate, IsCompanyRepurchasePlaceholder, IsCompanyRepurchaseShareholderTable,
)
from trader.download.domain.company_facts import CompanyFacts
from trader.download.domain.company_field_plan import (
    BuildCompanyFieldGaps,
    CompanyFieldGap,
    CompanyFieldMaterial,
    CompanyFieldRecovery,
    CompanyFieldIsAvailable,
    PlanCompanyFieldRecovery,
)
from trader.download.infra.company_fact_rows import ReadCompanyFactRows
from trader.download.infra.company_link_codec import DecodeCompanyLink, EncodeCompanyLink
from trader.download.infra.company_facts_index import CompanyInputReceipt, ReadCompanyPublicationIdentity
from trader.download.infra.company_field_result_rows import ReadCompanyFieldResults
from trader.download.infra.company_facts_protocol import (
    DecodeCompanyLabel,
    EVIDENCE_SOURCES,
    REVIEW_REASONS,
    REVIEW_SCOPES,
    RequireCompanyTime,
)
from trader.infra.sqlite_connection import SqliteConnection

# These rules change measurements only; old document receipts keep their identity.
# Unknown dependencies/subjects still require verification and are not download jobs.
COMPANY_FIELD_RULES = (
    ("start_date", "explicit_reduction_start_date:2"),
    ("end_date", "explicit_reduction_end_date:2"),
    ("planned_pct", "reduction_total_capital_ratio:2"),
    ("actual_pct", "implemented_total_capital_ratio:2"),
    ("repurchase_purpose", "explicit_repurchase_purpose:2"),
    ("contract_amount", "explicit_cny_contract_amount:3"),
    ("revenue_report_date", "explicit_contract_revenue_year:1"),
)
_REQUIRED_FIELD_EVIDENCE_RULE = "required_field_evidence:1"
_REPURCHASE_RECHECK_RULE = "same_body_repurchase_fields:1"
_REPURCHASE_PLAN_RULE = "issuer_repurchase_plan:1"
_FIELD_SOURCE_PROOF_REQUIRED = "field_source_proof_required"
_MATERIAL_REVIEW_REASONS = frozenset({
    "subject_unverified", "conclusion_unverified", "case_conclusion_ambiguous",
    "progress_unlinked", "resolution_unlinked", "cancellation_unlinked",
    "implementation_or_measurement_unverified", "revision_pending",
})


@dataclass(frozen=True, slots=True)
class CompanyMaterialDependency:
    """A still-unresolved review's exact input, never a field value or retry job."""

    code: str
    source: str
    evidence_id: str
    record_hash: str
    body_hash: bytes
    extraction_hash: bytes
    scope: str
    reason_code: str
    current_record_hash: str
    current_body_hash: bytes | None
    current_extraction_hash: bytes

    @property
    def is_current_version(self) -> bool:
        return (
            self.record_hash == self.current_record_hash
            and self.body_hash == self.current_body_hash
            and self.extraction_hash == self.current_extraction_hash
        )


def EnsureCompanyFieldRepairs(connection: SqliteConnection) -> None:
    connection.run(
        "CREATE TABLE IF NOT EXISTS company_field_repairs("
        "disclosure_id INTEGER NOT NULL REFERENCES disclosures(id) ON DELETE CASCADE,"
        "code TEXT NOT NULL,record_hash TEXT NOT NULL,event_id TEXT NOT NULL,field TEXT NOT NULL,rule_id TEXT NOT NULL,"
        "state TEXT NOT NULL CHECK(state IN ('pending','attempted','complete')),"
        "PRIMARY KEY(disclosure_id,code,record_hash,event_id,field,rule_id)) WITHOUT ROWID"
    )
    connection.run(
        "CREATE TABLE IF NOT EXISTS company_material_dependencies("
        "disclosure_id INTEGER NOT NULL REFERENCES disclosures(id) ON DELETE CASCADE,"
        "code TEXT NOT NULL,record_hash TEXT NOT NULL,body_hash BLOB NOT NULL CHECK(length(body_hash)=32),"
        "extraction_hash BLOB NOT NULL CHECK(length(extraction_hash)=32),scope TEXT NOT NULL,reason_code TEXT NOT NULL,"
        "PRIMARY KEY(disclosure_id,code,record_hash,body_hash,extraction_hash,scope,reason_code)) WITHOUT ROWID"
    )
    connection.run(
        "CREATE INDEX IF NOT EXISTS company_material_dependency_codes ON company_material_dependencies(code,disclosure_id)"
    )


def RegisterCompanyMaterialDependencies(
    collected: SqliteConnection,
    formal: SqliteConnection,
    facts: Sequence[CompanyFacts],
    extraction_hash: bytes,
) -> None:
    """Pin reviewed and unproved field material after its matching formal receipt exists.

    A pending/unread input is not a resolution. Keep its existing dependency
    until the same current body has actually been processed and published.
    This projection can be rebuilt after a formal commit/collection rollback.
    """
    field_required, field_settled = _field_material_requirements(formal, tuple(fact.code for fact in facts), extraction_hash)
    for fact in facts:
        required = frozenset(
            (review.evidence_id, review.scope, review.reason_code)
            for review in fact.review_reasons
            if review.evidence_id is not None and review.reason_code in _MATERIAL_REVIEW_REASONS
        ) | field_required.get(fact.code, frozenset())
        for evidence_id, scope, reason in required:
            rows = collected.run(
                "SELECT d.id,d.source,d.record_hash,d.body_hash FROM disclosures d "
                "JOIN disclosure_codes c ON c.disclosure_id=d.id "
                "WHERE d.source_key=? AND c.code=? AND d.kind='announcement'",
                (evidence_id, fact.code),
            ).fetchall()
            if len(rows) != 1:
                continue
            row = rows[0]
            if reason == _FIELD_SOURCE_PROOF_REQUIRED and not _field_material_source(formal, fact.code, scope, evidence_id, row[1]):
                continue
            body_hash = row[3]
            if body_hash is None:
                receipt = formal.run(
                    "SELECT body_hash FROM company_input_receipt WHERE source=? AND source_key=? AND code=? "
                    "AND record_hash=? AND extraction_hash=? AND outcome='valid' AND body_hash IS NOT NULL",
                    (row[1], evidence_id, fact.code, row[2], extraction_hash),
                ).fetchone()
                if receipt is None:
                    continue
                # Recover the original provenance, never the current material's
                # availability: initialization may have rejected a missing text.
                body_hash = receipt[0]
            if not _has_valid_material_receipt(
                formal, row[1], evidence_id, fact.code, row[2], body_hash, extraction_hash,
            ):
                continue
            collected.run(
                "INSERT OR REPLACE INTO company_material_dependencies VALUES(?,?,?,?,?,?,?)",
                (row[0], fact.code, row[2], body_hash, extraction_hash, scope, reason),
            )
        for row in collected.run(
            "SELECT m.disclosure_id,m.record_hash,m.body_hash,m.extraction_hash,m.scope,m.reason_code,"
            "d.source,d.source_key,d.record_hash,d.body_hash,d.body_checked "
            "FROM company_material_dependencies m JOIN disclosures d ON d.id=m.disclosure_id WHERE m.code=?",
            (fact.code,),
        ).fetchall():
            if (row[7], row[4], row[5]) in required:
                continue
            if row[5] == _FIELD_SOURCE_PROOF_REQUIRED and (
                (row[7], row[4], row[5]) not in field_settled.get(fact.code, frozenset())
                or row[1] != row[8] or row[2] != row[9] or row[3] != extraction_hash
                or not _field_material_source(formal, fact.code, row[4], row[7], row[6])
            ):
                # A different record/rule or an unrelated current event cannot
                # discharge the original field evidence obligation.
                continue
            unread = any(
                review.evidence_id == row[7] and review.reason_code in {"document_pending", "extraction_unsupported"}
                for review in fact.review_reasons
            )
            if unread or not row[10]:
                continue
            if row[9] is None or not _has_valid_material_receipt(
                formal, row[6], row[7], fact.code, row[8], row[9], extraction_hash,
            ):
                continue
            collected.run(
                "DELETE FROM company_material_dependencies WHERE disclosure_id=? AND code=? "
                "AND record_hash=? AND body_hash=? AND extraction_hash=? AND scope=? AND reason_code=?",
                (row[0], fact.code, row[1], row[2], row[3], row[4], row[5]),
            )


def _field_material_requirements(
    formal: SqliteConnection,
    codes: Sequence[str],
    extraction_hash: bytes,
) -> tuple[dict[str, frozenset[tuple[str, str, str]]], dict[str, frozenset[tuple[str, str, str]]]]:
    publication = ReadCompanyPublicationIdentity(formal)
    if publication is None or publication.extraction_hash != extraction_hash:
        return {}, {}
    required: dict[str, set[tuple[str, str, str]]] = {}
    settled: dict[str, set[tuple[str, str, str]]] = {}
    for offset in range(0, len(codes), 128):
        batch = codes[offset:offset + 128]
        fields = ReadCompanyFieldResults(formal, batch)
        events = {(item.code, item.event_id) for item in fields}
        unverified = {
            (item.code, item.event_id) for item in fields
            if item.applicable and item.state not in {"verified", "not_disclosed", "not_applicable"}
        }
        for fact in ReadCompanyFactRows(formal, batch):
            scoped_events: tuple[tuple[str, tuple[CompanyRiskEvent | CompanyPositiveEvent, ...]], ...] = (
                ("risk", fact.risk_events), ("positive", fact.positive_events),
            )
            for scope, members in scoped_events:
                for event in members:
                    key = (fact.code, event.event_id)
                    if key not in events:
                        continue
                    destination = required if key in unverified else settled
                    destination.setdefault(fact.code, set()).update(
                        (evidence_id, scope, _FIELD_SOURCE_PROOF_REQUIRED) for evidence_id in event.evidence_ids
                    )
    return (
        {code: frozenset(items) for code, items in required.items()},
        {code: frozenset(items) for code, items in settled.items()},
    )


def _field_material_source(formal: SqliteConnection, code: str, scope: str, evidence_id: str, source: str) -> bool:
    if scope not in {"risk", "positive"} or source not in EVIDENCE_SOURCES:
        return False
    return formal.run(
        f"SELECT 1 FROM company_{scope}_event e JOIN company_{scope}_evidence l ON l.{scope}_id=e.id "
        "JOIN company_evidence_ref r ON r.id=l.evidence_id "
        "WHERE e.code=? AND r.source_key=? AND r.source=? LIMIT 1",
        (code, evidence_id, EVIDENCE_SOURCES[source]),
    ).fetchone() is not None


def RestoreCompanyMaterialDependencies(collected: SqliteConnection, formal: SqliteConnection) -> None:
    """Pin exact review and field inputs before startup can discard an older version.

    Reviews do not name their original record/body version. Preserve the exact
    existing valid receipts for that source/key/code rather than guessing one;
    none establishes availability or success for the current directory version.
    """
    reasons = tuple(REVIEW_REASONS[reason] for reason in sorted(_MATERIAL_REVIEW_REASONS))
    placeholders = ",".join("?" for _reason in reasons)
    reviews = formal.run(
        "SELECT v.code,v.scope,v.reason,r.source,r.source_key FROM company_review v "
        "JOIN company_evidence_ref r ON r.id=v.evidence_id "
        f"WHERE v.reason IN ({placeholders}) ORDER BY v.code,r.source,r.source_key,v.scope,v.reason",
        reasons,
    )
    while batch := reviews.fetchmany(128):
        for review in batch:
            source = DecodeCompanyLabel(EVIDENCE_SOURCES, review[3])
            scope = DecodeCompanyLabel(REVIEW_SCOPES, review[1])
            reason = DecodeCompanyLabel(REVIEW_REASONS, review[2])
            receipts = formal.run(
                "SELECT record_hash,body_hash,extraction_hash FROM company_input_receipt "
                "WHERE source=? AND source_key=? AND code=? AND outcome='valid' AND body_hash IS NOT NULL",
                (source, review[4], review[0]),
            )
            while inputs := receipts.fetchmany(128):
                collected.run_many(
                    "INSERT OR IGNORE INTO company_material_dependencies "
                    "SELECT id,?,?,?,?,?,? FROM disclosures WHERE source=? AND source_key=?",
                    ((review[0], receipt[0], receipt[1], receipt[2], scope, reason, source, review[4]) for receipt in inputs),
                )
    publication = ReadCompanyPublicationIdentity(formal)
    if publication is None:
        return
    codes = tuple(row[0] for row in formal.run("SELECT code FROM company_facts ORDER BY code"))
    for offset in range(0, len(codes), 128):
        required, _settled = _field_material_requirements(formal, codes[offset:offset + 128], publication.extraction_hash)
        for code, obligations in required.items():
            for evidence_id, scope, reason in obligations:
                # Event evidence links retain the exact source owner which the
                # compact domain evidence key alone cannot distinguish.
                references = formal.run(
                    f"SELECT DISTINCT r.source FROM company_{scope}_event e JOIN company_{scope}_evidence l "
                    f"ON l.{scope}_id=e.id JOIN company_evidence_ref r ON r.id=l.evidence_id "
                    "WHERE e.code=? AND r.source_key=?", (code, evidence_id),
                ).fetchall()
                for reference in references:
                    source = DecodeCompanyLabel(EVIDENCE_SOURCES, reference[0])
                    receipts = formal.run(
                        "SELECT record_hash,body_hash,extraction_hash FROM company_input_receipt "
                        "WHERE source=? AND source_key=? AND code=? AND extraction_hash=? "
                        "AND outcome='valid' AND body_hash IS NOT NULL",
                        (source, evidence_id, code, publication.extraction_hash),
                    )
                    while inputs := receipts.fetchmany(128):
                        collected.run_many(
                            "INSERT OR IGNORE INTO company_material_dependencies "
                            "SELECT d.id,?,?,?,?,?,? FROM disclosures d JOIN disclosure_codes c ON c.disclosure_id=d.id "
                            "WHERE d.source=? AND d.source_key=? AND c.code=?",
                            ((code, receipt[0], receipt[1], receipt[2], scope, reason, source, evidence_id, code)
                             for receipt in inputs),
                        )


def ReadRequiredCompanyMaterialDependencies(
    collected: SqliteConnection,
    extraction_hash: bytes,
) -> tuple[CompanyMaterialDependency, ...]:
    """Read still-required original inputs alongside their current source/rule versions."""
    return tuple(CompanyMaterialDependency(*row) for row in collected.run(
        "SELECT m.code,d.source,d.source_key,m.record_hash,m.body_hash,m.extraction_hash,m.scope,m.reason_code,"
        "d.record_hash,d.body_hash,? "
        "FROM company_material_dependencies m JOIN disclosures d ON d.id=m.disclosure_id "
        "ORDER BY m.code,d.source,d.source_key,m.scope,m.reason_code,m.record_hash,m.body_hash,m.extraction_hash",
        (extraction_hash,),
    ))


def _has_valid_material_receipt(
    formal: SqliteConnection,
    source: str,
    evidence_id: str,
    code: str,
    record_hash: str,
    body_hash: bytes,
    extraction_hash: bytes,
) -> bool:
    return formal.run(
        "SELECT 1 FROM company_input_receipt WHERE source=? AND source_key=? AND code=? "
        "AND record_hash=? AND body_hash=? AND extraction_hash=? AND outcome='valid'",
        (source, evidence_id, code, record_hash, body_hash, extraction_hash),
    ).fetchone() is not None


def ReadCompanyFieldRecovery(
    formal: SqliteConnection,
    collected: SqliteConnection,
    codes: Sequence[str],
    observed_at: datetime,
    raw_root: Path,
) -> tuple[CompanyFieldRecovery, ...]:
    """Read a plan from existing facts and current source versions without writing either DB."""
    recoveries: list[CompanyFieldRecovery] = []
    for offset in range(0, len(codes), 128):
        facts = tuple(replace(fact, risk_events=tuple(
            risk for risk in fact.risk_events if risk.expires_at is None or risk.expires_at > observed_at
        )) for fact in ReadCompanyFactRows(formal, codes[offset:offset + 128]))
        has_repairs = _has_repairs(collected)
        for fact in facts:
            events: tuple[CompanyRiskEvent | CompanyPositiveEvent, ...] = (*fact.risk_events, *fact.positive_events)
            gaps = _field_gaps(fact, collected)
            materials: list[CompanyFieldMaterial] = []
            for key in sorted({gap.evidence_id for gap in gaps}):
                rows = collected.run(
                    "SELECT d.published_at,d.body_retained,d.body_hash,t.path,r.failure "
                    "FROM disclosures d JOIN disclosure_codes c ON c.disclosure_id=d.id "
                    "LEFT JOIN document_tasks t ON t.source=d.source "
                    "AND t.source_key=d.source_key AND t.record_hash=d.record_hash "
                    "LEFT JOIN document_retries r ON r.disclosure_id=d.id AND r.record_hash=d.record_hash "
                    "WHERE c.code=? AND d.source_key=? AND d.kind='announcement'", (fact.code, key),
                ).fetchall()
                # Evidence keys shared by different sources are not interchangeable.
                if len(rows) != 1:
                    continue
                row = rows[0]
                digest = row[2]
                text = bool(row[1]) and isinstance(digest, bytes) and len(digest) == 32 and (
                    raw_root / "text" / digest.hex()[:2] / (digest.hex() + ".txt.gz")
                ).is_file()
                pdf = _local_file(raw_root, row[3])
                materials.append(CompanyFieldMaterial(
                    key, datetime.fromisoformat(row[0]), text, pdf,
                    any(failure == row[4] and route == "ocr" for failure, route, _transient in CompanyDocumentFailureRules()),
                ))
            for item in PlanCompanyFieldRecovery(gaps, tuple(materials), observed_at):
                # A processed attempt is not proof of non-disclosure, and is not a
                # reason to request unchanged material again with the same rule.
                rule = _field_attempt_rule(item.gap)
                attempted = collected.run(
                    "SELECT 1 FROM company_field_repairs f JOIN disclosures d ON d.id=f.disclosure_id "
                    "AND d.record_hash=f.record_hash JOIN disclosure_codes c ON c.disclosure_id=d.id AND c.code=f.code "
                    "WHERE d.source_key=? AND f.code=? AND d.kind='announcement' "
                    "AND f.event_id=? AND f.field=? AND f.rule_id=? AND f.state='attempted'",
                    (item.gap.evidence_id, item.gap.code, item.gap.event_id, item.gap.field, rule),
                ).fetchone() if has_repairs else None
                material = next((m for m in materials if m.evidence_id == item.gap.evidence_id), None)
                event_time = next((event.last_disclosed_at for event in events if event.event_id == item.gap.event_id), None)
                if material is not None and event_time is not None and material.published_at < event_time:
                    item = replace(item, action="verify", reason="earlier_evidence_requires_revision_check")
                if material is not None and material.published_at.date() < observed_at.date() - timedelta(days=365) and not material.text_available:
                    item = replace(item, action="verify", reason="old_local_material_requires_verification") if material.pdf_available else item
                if (
                    item.action in {"extract", "ocr", "download"}
                    and not any(field == item.gap.field for field, _rule in COMPANY_FIELD_RULES)
                ):
                    item = replace(item, action="unsupported", reason="automatic_field_rule_unavailable")
                if attempted and item.action in {"extract", "ocr", "download"}:
                    item = replace(item, action="verify", reason="same_rule_already_attempted")
                recoveries.append(item)
    return tuple(recoveries)


def QueueCompanyFieldRepairs(
    collected: SqliteConnection,
    formal: SqliteConnection,
    codes: Sequence[str],
    observed_at: datetime,
    raw_root: Path,
) -> None:
    """Reopen only affected, supported field inputs, once per source version and rule."""
    for item in ReadCompanyFieldRecovery(formal, collected, codes, observed_at, raw_root):
        rule = next((rule for field, rule in COMPANY_FIELD_RULES if field == item.gap.field), None)
        if rule is None or item.action not in {"extract", "ocr", "download"}:
            continue
        row = collected.run(
            "SELECT d.id,d.record_hash,d.body_retained FROM disclosures d "
            "JOIN disclosure_codes c ON c.disclosure_id=d.id "
            "WHERE d.source_key=? AND c.code=? AND d.kind='announcement' "
            "AND (d.published_at>=? OR d.body_retained=1) AND d.published_at<=?",
            (item.gap.evidence_id, item.gap.code,
             (observed_at.date() - timedelta(days=365)).isoformat(), observed_at.isoformat()),
        ).fetchone()
        if row is None:
            continue
        inserted = collected.run(
            "INSERT OR IGNORE INTO company_field_repairs VALUES(?,?,?,?,?,?,'pending')",
            (row[0], item.gap.code, row[1], item.gap.event_id, item.gap.field, rule),
        ).rowcount
        if not inserted:
            continue
        collected.run("DELETE FROM document_publications WHERE disclosure_id=? AND code=?", (row[0], item.gap.code))
        if not row[2]:
            collected.run("UPDATE disclosures SET body_checked=0 WHERE id=?", (row[0],))
        collected.run("INSERT OR IGNORE INTO company_dirty_codes VALUES(?)", (item.gap.code,))


def RegisterCompanyFieldAttempts(
    connection: SqliteConnection,
    facts: Sequence[CompanyFacts],
    accepted: Sequence[CompanyInputReceipt],
    extraction_hash: bytes,
) -> None:
    """Bind first-pass gaps only to newly accepted, matching source and body versions.

    An evidence-only rule records an unresolved requirement, not a successful
    extraction or non-disclosure. It never authorizes an automatic retry.
    The caller keeps these rows in the collection transaction enclosing formal
    facts and field receipts, so a failed formal commit cannot retain a new task.
    """
    inputs = frozenset(
        (receipt.source, receipt.source_key, receipt.code, receipt.record_hash, receipt.body_hash)
        for receipt in accepted
        if receipt.outcome == "valid" and receipt.body_hash is not None and receipt.extraction_hash == extraction_hash
    )
    if not inputs:
        return
    for fact in facts:
        for gap in _field_gaps(fact, connection):
            for row in connection.run(
                "SELECT d.source,d.source_key,c.code,d.record_hash,d.body_hash,d.id FROM disclosures d "
                "JOIN disclosure_codes c ON c.disclosure_id=d.id "
                "WHERE d.source_key=? AND c.code=? AND d.kind='announcement' "
                "AND d.body_checked=1 AND d.body_retained=1",
                (gap.evidence_id, gap.code),
            ).fetchall():
                if tuple(row[:5]) not in inputs:
                    continue
                connection.run(
                    "INSERT OR IGNORE INTO company_field_repairs VALUES(?,?,?,?,?,?,'pending')",
                    (row[5], gap.code, row[3], gap.event_id, gap.field, _field_attempt_rule(gap)),
                )


def QueueCompanyRepurchaseRechecks(
    collected: SqliteConnection,
    formal: SqliteConnection,
    codes: Sequence[str],
    observed_at: datetime,
    extraction_hash: bytes,
    event_policy: CompanyEventPolicy,
    read_document: Callable[[bytes], str],
) -> None:
    """Correct retained same-body derivations without resetting document identity.

    Existing fields replay only their latest, exactly receipted input. Verified
    issuer plans and empty holder-list placeholders use that same material
    boundary. Missing material never creates a download task; field receipts
    prevent repeated field replay after publication or a collection rollback.
    """
    _queue_unverified_repurchase_inputs(collected, formal, codes, observed_at, extraction_hash, event_policy, read_document)
    _refresh_repurchase_link_candidates(collected, formal, codes, observed_at, extraction_hash, event_policy, read_document)
    for offset in range(0, len(codes), 128):
        for fact in ReadCompanyFactRows(formal, codes[offset:offset + 128]):
            for event in fact.positive_events:
                before = event.measurements
                if not isinstance(before, RepurchaseMeasurements) or event.status is CompanyPositiveStatus.CANCELLED:
                    continue
                evidence_id = event.evidence_ids[-1]
                if sum(isinstance(other.measurements, RepurchaseMeasurements) and evidence_id in other.evidence_ids
                       for other in fact.positive_events) != 1:
                    continue
                rows = collected.run(
                    "SELECT d.id,d.source,d.source_key,d.record_hash,d.body_hash,d.title,d.published_at,d.url "
                    "FROM disclosures d JOIN disclosure_codes c ON c.disclosure_id=d.id "
                    "WHERE c.code=? AND d.source_key=? AND d.kind='announcement' "
                    "AND d.body_checked=1 AND d.body_retained=1", (fact.code, evidence_id),
                ).fetchall()
                if len(rows) != 1:
                    continue
                row = rows[0]
                body_hash = row[4]
                if (not isinstance(body_hash, bytes) or len(body_hash) != 32
                    or datetime.fromisoformat(row[6]) != event.last_disclosed_at
                    or event.last_disclosed_at > observed_at
                    or not _has_valid_material_receipt(formal, row[1], evidence_id, fact.code,
                                                       row[3], body_hash, extraction_hash)):
                    continue
                policies = tuple(_repair_policy(event.event_id, field, _REPURCHASE_RECHECK_RULE)
                                 for field in ("actual_pct", "repurchase_purpose"))
                if formal.run(
                    "SELECT 1 FROM company_input_receipt WHERE source=? AND source_key=? AND code=? "
                    "AND record_hash=? AND body_hash=? AND outcome='field_attempted' "
                    "AND ((policy_id=? AND extraction_hash=?) OR (policy_id=? AND extraction_hash=?))",
                    (row[1], evidence_id, fact.code, row[3], body_hash,
                     policies[0], hashlib.sha256(policies[0].encode()).digest(),
                     policies[1], hashlib.sha256(policies[1].encode()).digest()),
                ).fetchone() is not None:
                    continue
                try:
                    text = read_document(body_hash)
                except (OSError, ValueError, EOFError):
                    continue
                if hashlib.sha256(text.encode("utf-8")).digest() != body_hash:
                    continue
                document = CompanyDocument(
                    Disclosure(row[1], evidence_id, (fact.code,), row[5], event.last_disclosed_at, row[7], row[3]),
                    text, True,
                )
                if IsCompanyRepurchaseShareholderTable(row[5]):
                    if IsCompanyRepurchasePlaceholder(event) and not ExtractCompanyEvents(
                        (document,), observed_at, event_policy, previous_positives=(event,),
                    ).positives:
                        collected.run(
                            "DELETE FROM document_link_candidates WHERE source=? AND source_key=? "
                            "AND record_hash=? AND extraction_hash=?", (row[1], evidence_id, row[3], extraction_hash),
                        )
                        collected.run("DELETE FROM document_publications WHERE disclosure_id=? AND code=?", (row[0], fact.code))
                        collected.run("INSERT OR IGNORE INTO company_dirty_codes VALUES(?)", (fact.code,))
                    continue
                candidate = ExtractCompanyLinkCandidate(document, observed_at, event_policy)
                if candidate is None or candidate.positive is None or candidate.subject is not event.subject_type:
                    continue
                after = candidate.positive.measurements
                if not isinstance(after, RepurchaseMeasurements):
                    continue
                fields = tuple(field for field, needed in (
                    ("actual_pct", before.actual_ratio_pct is None and after.actual_ratio_pct is not None),
                    ("repurchase_purpose", after.purpose is not RepurchasePurpose.UNKNOWN and before.purpose is not after.purpose),
                ) if needed)
                for field in fields:
                    collected.run(
                        "INSERT OR IGNORE INTO company_field_repairs VALUES(?,?,?,?,?,?,'pending')",
                        (row[0], fact.code, row[3], event.event_id, field, _REPURCHASE_RECHECK_RULE),
                    )
                if fields:
                    collected.run("DELETE FROM document_publications WHERE disclosure_id=? AND code=?", (row[0], fact.code))
                    collected.run("INSERT OR IGNORE INTO company_dirty_codes VALUES(?)", (fact.code,))


def QueueCompanyReductionRechecks(
    collected: SqliteConnection,
    formal: SqliteConnection,
    codes: Sequence[str],
    observed_at: datetime,
    extraction_hash: bytes,
    event_policy: CompanyEventPolicy,
    read_document: Callable[[bytes], str],
) -> None:
    """Recompile a receipted closure previously mistaken for an independent plan.

    Only its exact retained input can retire that single-evidence derivation.
    Existing linked plans and shared evidence remain untouched; the normal
    compiler owns both the resulting review and any verified original closure.
    """
    for offset in range(0, len(codes), 128):
        for fact in ReadCompanyFactRows(formal, codes[offset:offset + 128]):
            for event in fact.risk_events:
                if (event.risk_type is not CompanyRiskType.MAJOR_SHAREHOLDER_REDUCTION
                    or event.status is not CompanyEventStatus.ONGOING or event.resolved_at is not None
                    or event.first_disclosed_at != event.last_disclosed_at or len(event.evidence_ids) != 1
                    or event.last_disclosed_at > observed_at):
                    continue
                evidence_id = event.evidence_ids[0]
                reference_count = sum(evidence_id in other.evidence_ids for other in fact.risk_events)
                reference_count += sum(evidence_id in other.evidence_ids for other in fact.positive_events)
                if reference_count != 1:
                    continue
                rows = collected.run(
                    "SELECT d.id,d.source,d.record_hash,d.body_hash,d.title,d.published_at,d.url "
                    "FROM disclosures d JOIN disclosure_codes c ON c.disclosure_id=d.id "
                    "WHERE c.code=? AND d.source_key=? AND d.kind='announcement' "
                    "AND d.body_checked=1 AND d.body_retained=1 "
                    "AND (SELECT COUNT(*) FROM disclosure_codes members WHERE members.disclosure_id=d.id)=1",
                    (fact.code, evidence_id),
                ).fetchall()
                if len(rows) != 1:
                    continue
                row = rows[0]
                body_hash = row[3]
                if (not isinstance(body_hash, bytes) or len(body_hash) != 32
                    or datetime.fromisoformat(row[5]) != event.last_disclosed_at
                    or not _has_valid_material_receipt(
                        formal, row[1], evidence_id, fact.code, row[2], body_hash, extraction_hash,
                    )):
                    continue
                try:
                    text = read_document(body_hash)
                except (OSError, ValueError, EOFError):
                    continue
                if hashlib.sha256(text.encode("utf-8")).digest() != body_hash:
                    continue
                issuer_codes = ExtractCompanyAnnouncementNumbers(text).issuer_codes
                if issuer_codes and fact.code not in issuer_codes:
                    continue
                document = CompanyDocument(
                    Disclosure(row[1], evidence_id, (fact.code,), row[4], event.last_disclosed_at, row[6], row[2]),
                    text, True,
                )
                parsed = ExtractCompanyEvents((document,), observed_at, event_policy, previous_risks=(event,))
                if (any(item.event_id == event.event_id for item in parsed.risks)
                    or not any(review.evidence_id == evidence_id and review.reason_code == "resolution_unlinked"
                               for review in parsed.reviews)):
                    continue
                collected.run(
                    "DELETE FROM document_link_candidates WHERE source=? AND source_key=? "
                    "AND record_hash=? AND extraction_hash=?", (row[1], evidence_id, row[2], extraction_hash),
                )
                candidate = ExtractCompanyLinkCandidate(document, observed_at, event_policy)
                if candidate is not None:
                    collected.run(
                        "INSERT OR REPLACE INTO document_link_candidates VALUES(?,?,?,?,?)",
                        (row[1], evidence_id, row[2], extraction_hash, EncodeCompanyLink(candidate)),
                    )
                collected.run("DELETE FROM document_publications WHERE disclosure_id=? AND code=?", (row[0], fact.code))
                collected.run("INSERT OR IGNORE INTO company_dirty_codes VALUES(?)", (fact.code,))


def _queue_unverified_repurchase_inputs(
    collected: SqliteConnection,
    formal: SqliteConnection,
    codes: Sequence[str],
    observed_at: datetime,
    extraction_hash: bytes,
    event_policy: CompanyEventPolicy,
    read_document: Callable[[bytes], str],
) -> None:
    for offset in range(0, len(codes), 128):
        batch = codes[offset:offset + 128]
        placeholders = ",".join("?" for _code in batch)
        reviewed = formal.run(
            "SELECT r.code,e.source_key FROM company_review r JOIN company_evidence_ref e ON e.id=r.evidence_id "
            f"WHERE r.code IN ({placeholders}) AND r.scope=? AND r.reason=?",
            (*batch, REVIEW_SCOPES["positive"], REVIEW_REASONS["subject_unverified"]),
        ).fetchall()
        facts = {fact.code: fact for fact in ReadCompanyFactRows(formal, batch)}
        for code, evidence_id in reviewed:
            if any(evidence_id in event.evidence_ids for event in facts[code].positive_events):
                continue
            rows = collected.run(
                "SELECT d.id,d.source,d.record_hash,d.body_hash,d.title,d.published_at,d.url "
                "FROM disclosures d JOIN disclosure_codes c ON c.disclosure_id=d.id "
                "WHERE c.code=? AND d.source_key=? AND d.kind='announcement' AND d.body_checked=1 AND d.body_retained=1 "
                "AND (SELECT COUNT(*) FROM disclosure_codes members WHERE members.disclosure_id=d.id)=1 "
                "AND NOT EXISTS(SELECT 1 FROM company_field_repairs f WHERE f.disclosure_id=d.id "
                "AND f.code=c.code AND f.record_hash=d.record_hash AND f.rule_id=?)",
                (code, evidence_id, _REPURCHASE_PLAN_RULE),
            ).fetchall()
            if len(rows) != 1:
                continue
            row = rows[0]
            table = IsCompanyRepurchaseShareholderTable(row[4])
            if not table and not any(marker in row[4] for marker in (
                "回购报告书", "回购股份方案", "回购公司股份方案",
            )):
                continue
            body_hash, published = row[3], datetime.fromisoformat(row[5])
            if (not isinstance(body_hash, bytes) or len(body_hash) != 32 or published > observed_at
                or not _has_valid_material_receipt(formal, row[1], evidence_id, code, row[2], body_hash, extraction_hash)):
                continue
            try:
                text = read_document(body_hash)
            except (OSError, ValueError, EOFError):
                continue
            if hashlib.sha256(text.encode("utf-8")).digest() != body_hash:
                continue
            document = CompanyDocument(
                Disclosure(row[1], evidence_id, (code,), row[4], published, row[6], row[2]), text, True,
            )
            if table:
                review = CompanyReviewReason("positive", "subject_unverified", evidence_id, published, "旧主体待核")
                if not ExtractCompanyEvents((document,), observed_at, event_policy, previous_reviews=(review,)).reviews:
                    collected.run(
                        "DELETE FROM document_link_candidates WHERE source=? AND source_key=? "
                        "AND record_hash=? AND extraction_hash=?", (row[1], evidence_id, row[2], extraction_hash),
                    )
                    collected.run("DELETE FROM document_publications WHERE disclosure_id=? AND code=?", (row[0], code))
                    collected.run("INSERT OR IGNORE INTO company_dirty_codes VALUES(?)", (code,))
                continue
            candidate = ExtractCompanyLinkCandidate(document, observed_at, event_policy)
            if (candidate is None or candidate.positive is None or candidate.cancellation
                or candidate.subject is not CompanySubject.ISSUER
                or candidate.positive.event_type is not CompanyPositiveType.SHARE_REPURCHASE):
                continue
            collected.run("INSERT OR REPLACE INTO document_link_candidates VALUES(?,?,?,?,?)",
                          (row[1], evidence_id, row[2], extraction_hash, EncodeCompanyLink(candidate)))
            collected.run("INSERT OR IGNORE INTO company_field_repairs VALUES(?,?,?,?,?,?,'pending')",
                          (row[0], code, row[2], candidate.positive.event_id, "repurchase_purpose", _REPURCHASE_PLAN_RULE))
            collected.run("DELETE FROM document_publications WHERE disclosure_id=? AND code=?", (row[0], code))
            collected.run("INSERT OR IGNORE INTO company_dirty_codes VALUES(?)", (code,))


def _refresh_repurchase_link_candidates(
    collected: SqliteConnection,
    formal: SqliteConnection,
    codes: Sequence[str],
    observed_at: datetime,
    extraction_hash: bytes,
    event_policy: CompanyEventPolicy,
    read_document: Callable[[bytes], str],
) -> None:
    # An unlinked progress input may have no formal event yet. Refresh its
    # derived candidate too, or it would later reintroduce the old measurement.
    for offset in range(0, len(codes), 128):
        batch = codes[offset:offset + 128]
        placeholders = ",".join("?" for _code in batch)
        rows = collected.run(
            "SELECT d.source,d.source_key,c.code,d.record_hash,d.body_hash,d.title,d.published_at,d.url,l.payload "
            "FROM document_link_candidates l JOIN disclosures d ON d.source=l.source AND d.source_key=l.source_key "
            "AND d.record_hash=l.record_hash JOIN disclosure_codes c ON c.disclosure_id=d.id "
            f"WHERE c.code IN ({placeholders}) AND l.extraction_hash=? AND d.body_checked=1 AND d.body_retained=1 "
            "AND (SELECT COUNT(*) FROM disclosure_codes members WHERE members.disclosure_id=d.id)=1",
            (*batch, extraction_hash),
        ).fetchall()
        for row in rows:
            old = DecodeCompanyLink(row[8])
            if old.positive is None or not isinstance(old.positive.measurements, (RepurchaseMeasurements, IncreaseMeasurements)):
                continue
            body_hash = row[4]
            published = datetime.fromisoformat(row[6])
            if (not isinstance(body_hash, bytes) or len(body_hash) != 32 or published > observed_at
                or old.evidence_id != row[1] or old.published_at != published
                or not _has_valid_material_receipt(formal, row[0], row[1], row[2], row[3], body_hash, extraction_hash)):
                continue
            try:
                text = read_document(body_hash)
            except (OSError, ValueError, EOFError):
                continue
            if hashlib.sha256(text.encode("utf-8")).digest() != body_hash:
                continue
            current = ExtractCompanyLinkCandidate(CompanyDocument(
                Disclosure(row[0], row[1], (row[2],), row[5], published, row[7], row[3]), text, True,
            ), observed_at, event_policy)
            if (current is None or current.positive is None or current.subject is not old.subject
                or type(current.positive.measurements) is not type(old.positive.measurements)
                or current.references != old.references or current.cancellation != old.cancellation):
                continue
            status_recheck = (
                current.positive.status in {CompanyPositiveStatus.IMPLEMENTING, CompanyPositiveStatus.COMPLETED}
                and old.positive.status in {CompanyPositiveStatus.PLANNED, current.positive.status}
                and old.positive.measurements.actual_ratio_pct is None
                and isinstance(current.positive.measurements, (RepurchaseMeasurements, IncreaseMeasurements))
                and current.positive.measurements.actual_ratio_pct is None
            )
            if isinstance(old.positive.measurements, IncreaseMeasurements) and not status_recheck:
                continue
            replay_status = False
            if status_recheck:
                matches = tuple(event for fact in ReadCompanyFactRows(formal, (row[2],)) for event in fact.positive_events
                                if row[1] in event.evidence_ids)
                if matches:
                    if len(matches) != 1:
                        continue
                    event = matches[0]
                    if (event.event_type is not current.positive.event_type or event.subject_type is not current.subject
                        or event.status not in {CompanyPositiveStatus.PLANNED, current.positive.status}
                        or event.evidence_ids[-1] != row[1]
                        or event.last_disclosed_at != published
                        or not isinstance(event.measurements, (RepurchaseMeasurements, IncreaseMeasurements))
                        or event.measurements.actual_ratio_pct is not None):
                        continue
                    if event.status is CompanyPositiveStatus.PLANNED:
                        # A prior interruption may have updated the cache before the formal fact.
                        # Replay collection confirmation; the exact formal valid receipt stays immutable.
                        collected.run(
                            "DELETE FROM document_publications WHERE code=? AND disclosure_id IN "
                            "(SELECT id FROM disclosures WHERE source=? AND source_key=? AND record_hash=?)",
                            (row[2], row[0], row[1], row[3]),
                        )
                        replay_status = True
            payload = EncodeCompanyLink(current)
            if payload != row[8]:
                collected.run(
                    "UPDATE document_link_candidates SET payload=? WHERE source=? AND source_key=? "
                    "AND record_hash=? AND extraction_hash=? AND payload=?",
                    (payload, row[0], row[1], row[3], extraction_hash, row[8]),
                )
            if payload != row[8] or replay_status:
                collected.run("INSERT OR IGNORE INTO company_dirty_codes VALUES(?)", (row[2],))


def PrioritizeCompanyRepurchaseInputs(
    collected: SqliteConnection,
    formal: SqliteConnection,
    codes: Sequence[str],
    observed_at: datetime,
    extraction_hash: bytes,
) -> None:
    """Rank prerequisite candidates; this proves neither their identity nor a value.

    A receipted, unlinked progress report may need an older plan already in the
    pending inventory. Download it first, then let the existing official-number
    and event-link owners decide the relationship. No new request is created.
    """
    collected.run("CREATE TEMP TABLE IF NOT EXISTS company_field_inputs(id INTEGER PRIMARY KEY)")
    collected.run("DELETE FROM temp.company_field_inputs")
    parent = ReadCompanyPublicationIdentity(formal)
    if parent is None or parent.extraction_hash != extraction_hash:
        return
    for offset in range(0, len(codes), 128):
        batch = codes[offset:offset + 128]
        placeholders = ",".join("?" for _code in batch)
        sources = formal.run(
            "SELECT r.code,e.source_key FROM company_review r JOIN company_evidence_ref e ON e.id=r.evidence_id "
            f"WHERE r.code IN ({placeholders}) AND r.scope=? AND r.reason=?",
            (*batch, REVIEW_SCOPES["positive"], REVIEW_REASONS["progress_unlinked"]),
        ).fetchall()
        for code, evidence_id in sources:
            rows = collected.run(
                "SELECT d.source,d.record_hash,d.body_hash,d.published_at,l.payload FROM disclosures d "
                "JOIN disclosure_codes c ON c.disclosure_id=d.id JOIN document_link_candidates l "
                "ON l.source=d.source AND l.source_key=d.source_key AND l.record_hash=d.record_hash "
                "WHERE c.code=? AND d.source_key=? AND d.body_checked=1 AND l.extraction_hash=?",
                (code, evidence_id, extraction_hash),
            ).fetchall()
            if len(rows) != 1:
                continue
            row = rows[0]
            candidate = DecodeCompanyLink(row[4])
            if (candidate.positive is None or not isinstance(candidate.positive.measurements, RepurchaseMeasurements)
                or candidate.evidence_id != evidence_id or candidate.published_at != datetime.fromisoformat(row[3])
                or candidate.published_at > observed_at or not isinstance(row[2], bytes)
                or not _has_valid_material_receipt(formal, row[0], evidence_id, code, row[1], row[2], extraction_hash)):
                continue
            for pending in collected.run(
                "SELECT d.id,d.title FROM disclosures d JOIN disclosure_codes c ON c.disclosure_id=d.id "
                "JOIN temp.pending_documents p ON p.id=d.id "
                "WHERE c.code=? AND d.source=? AND d.published_at<? AND d.kind='announcement'",
                (code, row[0], row[3]),
            ):
                title = "".join(pending[1].split())
                if (any(word in title for word in ("回购报告书", "回购股份方案", "回购公司股份方案", "回购股份的预案", "回购股份预案"))
                    and not any(word in title for word in ("进展", "前十", "核查", "意见"))):
                    collected.run("INSERT OR IGNORE INTO temp.company_field_inputs VALUES(?)", (pending[0],))


def CompleteCompanyFieldRepairs(
    connection: SqliteConnection,
    formal: SqliteConnection,
    facts: Sequence[CompanyFacts],
    extraction_hash: bytes,
    *,
    finish_attempts: bool = True,
) -> tuple[str, ...]:
    """Retire satisfied fields after commit, including older queued material.

    Restoring the collection projection requires the original formal document
    receipt. Field completion never creates a document or extraction receipt.
    """
    changed: set[str] = set()
    for fact in facts:
        gaps = _field_gaps(fact, connection)
        rows = connection.run(
            "SELECT f.disclosure_id,f.record_hash,f.field,f.rule_id,f.event_id,d.body_checked,"
            "d.source,d.source_key,d.body_hash "
            "FROM company_field_repairs f "
            "JOIN disclosures d ON d.id=f.disclosure_id AND d.record_hash=f.record_hash "
            "WHERE f.code=? AND f.state<>'complete'", (fact.code,),
        ).fetchall()
        for row in rows:
            if row[3] == _REPURCHASE_RECHECK_RULE and not finish_attempts:
                # The old non-null value is precisely what this task must
                # revalidate. Only its new formal field receipt can restore it.
                continue
            available = CompanyFieldIsAvailable(fact, row[4], row[2])
            if not available and (not row[5] or not finish_attempts):
                continue
            missing = any(gap.event_id == row[4] and gap.field == row[2] for gap in gaps)
            if not available and not missing:
                retired_reduction = (
                    row[4] == f"{row[7]}:{CompanyRiskType.MAJOR_SHAREHOLDER_REDUCTION.value}"
                    and row[2] in {"start_date", "end_date", "planned_pct"}
                    and not any(event.event_id == row[4] for event in fact.risk_events)
                    and (
                        any(review.evidence_id == row[7] and review.reason_code == "resolution_unlinked"
                            for review in fact.review_reasons)
                        or any(event.risk_type is CompanyRiskType.MAJOR_SHAREHOLDER_REDUCTION
                               and event.status is CompanyEventStatus.RESOLVED and row[7] in event.evidence_ids
                               for event in fact.risk_events)
                    )
                )
                if not retired_reduction:
                    continue
                policy = _repair_policy(row[4], row[2], row[3])
                if formal.run(
                    "SELECT 1 FROM company_input_receipt WHERE source=? AND source_key=? AND code=? "
                    "AND record_hash=? AND extraction_hash=? AND outcome='field_attempted' AND policy_id=? AND body_hash=?",
                    (row[6], row[7], fact.code, row[1], hashlib.sha256(policy.encode()).digest(), policy, row[8]),
                ).fetchone() is None:
                    continue
                # Reclassifying the notice does not prove any old plan field.
                # Its exact committed attempt merely retires the stale work.
            connection.run(
                "UPDATE company_field_repairs SET state=? WHERE disclosure_id=? AND code=? "
                "AND record_hash=? AND field=? AND rule_id=? AND event_id=?",
                ("complete" if available else "attempted",
                 row[0], fact.code, row[1], row[2], row[3], row[4]),
            )
            if not row[5] and available:
                changed.update(_restore_satisfied_document(connection, formal, row[0], extraction_hash))
    return tuple(sorted(changed))


def _restore_satisfied_document(
    connection: SqliteConnection, formal: SqliteConnection, disclosure_id: int, extraction_hash: bytes
) -> tuple[str, ...]:
    document = connection.run(
        "SELECT source,source_key,record_hash,body_hash FROM disclosures WHERE id=?", (disclosure_id,),
    ).require_one()
    changed: list[str] = []
    for row in connection.run(
        "SELECT c.code FROM disclosure_codes c JOIN company_population u ON u.code=c.code "
        "WHERE c.disclosure_id=? AND NOT EXISTS(SELECT 1 FROM company_field_repairs f "
        "WHERE f.disclosure_id=c.disclosure_id AND f.code=c.code AND f.record_hash=? AND f.state<>'complete') "
        "AND NOT EXISTS(SELECT 1 FROM document_publications p WHERE p.disclosure_id=c.disclosure_id "
        "AND p.code=c.code AND p.record_hash=? AND p.extraction_hash=?)",
        (disclosure_id, document[2], document[2], extraction_hash),
    ).fetchall():
        receipt = formal.run(
            "SELECT published_at FROM company_input_receipt WHERE source=? AND source_key=? AND code=? "
            "AND record_hash=? AND extraction_hash=? AND body_hash=? AND outcome='valid'",
            (document[0], document[1], row[0], document[2], extraction_hash, document[3]),
        ).fetchone()
        if receipt is None:
            continue
        connection.run("INSERT OR REPLACE INTO document_publications VALUES(?,?,?,?,?)",
                       (disclosure_id, row[0], document[2], extraction_hash, receipt[0]))
        changed.append(row[0])
    connection.run(
        "UPDATE disclosures SET body_checked=1 WHERE id=? AND EXISTS(SELECT 1 FROM document_publications p "
        "WHERE p.disclosure_id=disclosures.id AND p.record_hash=disclosures.record_hash AND p.extraction_hash=?) "
        "AND NOT EXISTS(SELECT 1 FROM disclosure_codes c JOIN company_population u ON u.code=c.code "
        "WHERE c.disclosure_id=disclosures.id AND NOT EXISTS(SELECT 1 FROM document_publications p "
        "WHERE p.disclosure_id=c.disclosure_id AND p.code=c.code AND p.record_hash=disclosures.record_hash "
        "AND p.extraction_hash=?))", (disclosure_id, extraction_hash, extraction_hash),
    )
    return tuple(changed)


def CompanyFieldRepairReceipts(
    connection: SqliteConnection, codes: Sequence[str], at: datetime
) -> tuple[CompanyInputReceipt, ...]:
    receipts: list[CompanyInputReceipt] = []
    for code in codes:
        for row in connection.run(
            "SELECT d.source,d.source_key,d.record_hash,d.body_hash,f.field,f.rule_id,f.event_id "
            "FROM company_field_repairs f JOIN disclosures d ON d.id=f.disclosure_id "
            "AND d.record_hash=f.record_hash WHERE f.code=? AND f.state='pending' "
            "AND d.body_checked=1 AND d.body_retained=1", (code,),
        ):
            policy = _repair_policy(row[6], row[4], row[5])
            receipts.append(CompanyInputReceipt(
                row[0], row[1], code, row[2], hashlib.sha256(policy.encode()).digest(), row[3], at,
                "field_attempted", policy,
            ))
    return tuple(receipts)


def RestoreCompanyFieldRepairs(collected: SqliteConnection, formal: SqliteConnection) -> None:
    # A first-pass task can roll back after its formal receipt has committed.
    # Reconstruct only a still-missing requirement with the exact current body;
    # a legacy document-level valid receipt is never a field attempt.
    _restore_first_field_attempts(collected, formal)
    for row in collected.run(
        "SELECT f.disclosure_id,f.code,f.record_hash,f.event_id,f.field,f.rule_id,d.source,d.source_key,d.body_hash "
        "FROM company_field_repairs f JOIN disclosures d ON d.id=f.disclosure_id "
        "AND d.record_hash=f.record_hash WHERE f.state='pending'",
    ).fetchall():
        policy = _repair_policy(row[3], row[4], row[5])
        receipt = formal.run(
            "SELECT 1 FROM company_input_receipt WHERE source=? AND source_key=? AND code=? "
            "AND record_hash=? AND extraction_hash=? AND outcome='field_attempted' AND policy_id=? AND body_hash=?",
            (row[6], row[7], row[1], row[2], hashlib.sha256(policy.encode()).digest(), policy, row[8]),
        ).fetchone()
        if receipt is not None:
            available = any(
                CompanyFieldIsAvailable(fact, row[3], row[4]) for fact in ReadCompanyFactRows(formal, (row[1],))
            )
            collected.run(
                "UPDATE company_field_repairs SET state=? WHERE disclosure_id=? AND code=? "
                "AND record_hash=? AND event_id=? AND field=? AND rule_id=?",
                ("complete" if available else "attempted", *row[:6]),
            )
    parent = formal.run("SELECT extraction_hash FROM company_publication WHERE singleton=1").fetchone()
    if parent is not None:
        codes = tuple(row[0] for row in collected.run(
            "SELECT DISTINCT code FROM company_field_repairs WHERE state='pending'"
        ))
        for offset in range(0, len(codes), 128):
            changed = CompleteCompanyFieldRepairs(
                collected, formal, ReadCompanyFactRows(formal, codes[offset:offset + 128]), parent[0],
                finish_attempts=False,
            )
            collected.run_many("INSERT OR IGNORE INTO company_dirty_codes VALUES(?)", ((code,) for code in changed))


def _restore_first_field_attempts(collected: SqliteConnection, formal: SqliteConnection) -> None:
    receipts = (
        CompanyInputReceipt(
            row[0], row[1], row[2], row[3], row[4], row[5], RequireCompanyTime(row[6]), row[7], row[8]
        )
        for row in formal.run(
            "SELECT source,source_key,code,record_hash,extraction_hash,body_hash,published_at,outcome,policy_id "
            "FROM company_input_receipt WHERE outcome='field_attempted' ORDER BY code"
        )
    )
    for code, inputs in groupby(receipts, key=lambda receipt: receipt.code):
        gaps = tuple(gap for fact in ReadCompanyFactRows(formal, (code,)) for gap in _field_gaps(fact, collected))
        for receipt in inputs:
            if receipt.body_hash is None:
                continue
            for gap in gaps:
                rule = _field_attempt_rule(gap)
                policy = _repair_policy(gap.event_id, gap.field, rule)
                if (
                    receipt.source_key != gap.evidence_id or receipt.policy_id != policy
                    or receipt.extraction_hash != hashlib.sha256(policy.encode()).digest()
                ):
                    continue
                collected.run(
                    "INSERT OR IGNORE INTO company_field_repairs "
                    "SELECT d.id,?,d.record_hash,?,?,?,'attempted' FROM disclosures d "
                    "JOIN disclosure_codes c ON c.disclosure_id=d.id "
                    "WHERE d.source=? AND d.source_key=? AND c.code=? AND d.record_hash=? "
                    "AND d.body_hash=? AND d.body_checked=1 AND d.body_retained=1",
                    (
                        code, gap.event_id, gap.field, rule, receipt.source, receipt.source_key, code,
                        receipt.record_hash, receipt.body_hash,
                    ),
                )


def _field_attempt_rule(gap: CompanyFieldGap) -> str:
    if gap.reason == "measurement_missing":
        for field, rule in COMPANY_FIELD_RULES:
            if gap.field == field:
                return rule
    if gap.reason in {"measurement_missing", "dependency_missing", "ambiguous", "implementation_missing"}:
        return _REQUIRED_FIELD_EVIDENCE_RULE
    raise ValueError("company_field_attempt_not_applicable")


def _repair_policy(event_id: str, field: str, rule: str) -> str:
    return "field:" + hashlib.sha256(event_id.encode()).hexdigest() + ":" + field + ":" + rule


def _local_file(root: Path, relative: object) -> bool:
    if not isinstance(relative, str):
        return False
    candidate = (root / relative).resolve()
    return candidate.is_relative_to(root.resolve()) and candidate.is_file()


def _has_repairs(connection: SqliteConnection) -> bool:
    return connection.run("SELECT 1 FROM sqlite_master WHERE name='company_field_repairs'").fetchone() is not None


def _field_gaps(fact: CompanyFacts, connection: SqliteConnection) -> tuple[CompanyFieldGap, ...]:
    gaps = list(BuildCompanyFieldGaps(fact))
    for event in fact.positive_events:
        if event.status is not CompanyPositiveStatus.PLANNED:
            continue
        if not isinstance(event.measurements, (RepurchaseMeasurements, IncreaseMeasurements)):
            continue
        if event.measurements.actual_ratio_pct is not None:
            continue
        source = connection.run(
            "SELECT d.title FROM disclosures d JOIN disclosure_codes c ON c.disclosure_id=d.id "
            "WHERE c.code=? AND d.source_key=? AND d.kind='announcement'",
            (fact.code, event.evidence_ids[-1]),
        ).fetchall()
        if len(source) == 1 and any(marker in source[0][0] for marker in ("进展", "实施情况", "结果", "完成")):
            gaps.append(CompanyFieldGap(fact.code, event.event_id, event.evidence_ids[-1], "actual_pct", "measurement_missing"))
    return tuple(gaps)


__all__ = [
    "COMPANY_FIELD_RULES",
    "EnsureCompanyFieldRepairs",
    "ReadCompanyFieldRecovery",
    "QueueCompanyFieldRepairs",
    "QueueCompanyRepurchaseRechecks",
    "QueueCompanyReductionRechecks",
    "PrioritizeCompanyRepurchaseInputs",
    "RegisterCompanyFieldAttempts",
    "CompleteCompanyFieldRepairs",
    "CompanyFieldRepairReceipts",
    "RestoreCompanyFieldRepairs",
    "CompanyMaterialDependency",
    "RegisterCompanyMaterialDependencies",
    "RestoreCompanyMaterialDependencies",
    "ReadRequiredCompanyMaterialDependencies",
]
