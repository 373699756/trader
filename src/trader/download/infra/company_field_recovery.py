"""Explicit material protection/recovery and read-only field needs; no automatic dispatch."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

from trader.download.domain.company_announcement_numbers import ExtractCompanyAnnouncementNumbers
from trader.download.domain.company_annual_report import COMPANY_ANNUAL_REPORT_DISCLOSURE_RULE
from trader.download.domain.company_contract_fields import COMPANY_CONTRACT_FIELD_RULES
from trader.download.domain.company_events import CompanyPositiveEvent, CompanyPositiveStatus, CompanyRiskEvent, IncreaseMeasurements, RepurchaseMeasurements
from trader.download.domain.company_evidence import CompanyDocumentFailureRules
from trader.download.domain.company_facts import CompanyFacts
from trader.download.domain.company_field_plan import BuildCompanyFieldGaps, CompanyFieldGap, CompanyFieldMaterial, CompanyFieldRecovery, PlanCompanyFieldRecovery
from trader.download.infra.company_fact_rows import ReadCompanyFactRows
from trader.download.infra.company_facts_index import ReadCompanyPublicationIdentity
from trader.download.infra.company_field_result_rows import ReadCompanyFieldResults
from trader.download.infra.company_financial_publication import ReadCompanyFinancialMaterials
from trader.download.infra.company_document_files import CompanyDocumentFiles
from trader.download.infra.company_facts_protocol import DecodeCompanyLabel, EVIDENCE_SOURCES, REVIEW_REASONS, REVIEW_SCOPES
from trader.infra.sqlite_connection import SqliteConnection

COMPANY_FIELD_RULES = (
    ("start_date", "explicit_reduction_start_date:2"),
    ("end_date", "explicit_reduction_end_date:2"),
    ("planned_pct", "reduction_total_capital_ratio:3"),
    ("completion_disclosed_at", "explicit_reduction_completion:1"),
    ("actual_pct", "implemented_total_capital_ratio:3"),
    ("repurchase_purpose", "explicit_repurchase_purpose:2"),
) + COMPANY_CONTRACT_FIELD_RULES + (("revenue_disclosed_at", COMPANY_ANNUAL_REPORT_DISCLOSURE_RULE),)
_REQUIRED_FIELD_EVIDENCE_RULE = "required_field_evidence:1"
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
        financial_materials = ReadCompanyFinancialMaterials(formal, batch)
        for fact in ReadCompanyFactRows(formal, batch):
            periods = {str(report.report_date) for report in (fact.financial, fact.latest_financial) if report is not None}
            for material in financial_materials:
                if material.code == fact.code:
                    destination = required if str(material.report_date) in periods else settled
                    destination.setdefault(fact.code, set()).add((material.source_key, "financial", _FIELD_SOURCE_PROOF_REQUIRED))
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
                    destination[fact.code].update(
                        (proof.source_key, scope, _FIELD_SOURCE_PROOF_REQUIRED)
                        for item in fields if item.code == fact.code and item.event_id == event.event_id
                        for proof in item.evidence
                    )
    return (
        {code: frozenset(items) for code, items in required.items()},
        {code: frozenset(items) for code, items in settled.items()},
    )

def _field_material_source(formal: SqliteConnection, code: str, scope: str, evidence_id: str, source: str) -> bool:
    if scope == "financial" and source in EVIDENCE_SOURCES:
        return formal.run(
            "SELECT 1 FROM company_input_receipt WHERE code=? AND source=? AND source_key=? "
            "AND outcome='field_attempted' AND policy_id LIKE 'financial_source:%' LIMIT 1", (code, source, evidence_id),
        ).fetchone() is not None
    if scope not in {"risk", "positive"} or source not in EVIDENCE_SOURCES:
        return False
    linked = formal.run(
        f"SELECT 1 FROM company_{scope}_event e JOIN company_{scope}_evidence l ON l.{scope}_id=e.id "
        "JOIN company_evidence_ref r ON r.id=l.evidence_id "
        "WHERE e.code=? AND r.source_key=? AND r.source=? LIMIT 1",
        (code, evidence_id, EVIDENCE_SOURCES[source]),
    ).fetchone() is not None
    return linked or scope == "positive" and formal.run(
        "SELECT 1 FROM company_field_evidence WHERE code=? AND source_key=? AND source=? "
        "AND field IN ('annual_revenue','revenue_report_date','revenue_disclosed_at') LIMIT 1",
        (code, evidence_id, source),
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
                ).fetchall() if scope != "financial" else ()
                sources = {DecodeCompanyLabel(EVIDENCE_SOURCES, reference[0]) for reference in references}
                if scope == "financial":
                    sources.update(row[0] for row in formal.run(
                        "SELECT DISTINCT source FROM company_input_receipt WHERE code=? AND source_key=? "
                        "AND outcome='field_attempted' AND policy_id LIKE 'financial_source:%'", (code, evidence_id),
                    ))
                if scope == "positive":
                    sources.update(row[0] for row in formal.run(
                        "SELECT DISTINCT source FROM company_field_evidence WHERE code=? AND source_key=? "
                        "AND field IN ('annual_revenue','revenue_report_date','revenue_disclosed_at')", (code, evidence_id),
                    ))
                for source in sorted(sources):
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

def RecoverRequiredCompanyText(
    collected: SqliteConnection,
    formal: SqliteConnection,
    source_key: str,
    text: str,
    observed_at: datetime,
    extraction_hash: bytes,
    documents: CompanyDocumentFiles,
    check: Callable[[], None],
    reserve: Callable[[int], AbstractContextManager[object]] | None,
) -> bool:
    """Recover only an explicitly supplied, exact, still-required original text.

    This neither accepts a new extraction nor completes a field. The normal
    compiler must inspect the material and publish any changed conclusions.
    """
    if observed_at.utcoffset() is None:
        raise ValueError("company_material_recovery_time_invalid")
    rows = collected.run(
        "SELECT DISTINCT d.id,d.source,d.record_hash,m.body_hash,d.body_retained,c.code,d.published_at "
        "FROM disclosures d JOIN disclosure_codes c ON c.disclosure_id=d.id "
        "JOIN company_population p ON p.code=c.code "
        "JOIN company_material_dependencies m ON m.disclosure_id=d.id AND m.code=c.code "
        "AND m.record_hash=d.record_hash AND (d.body_hash IS NULL OR m.body_hash=d.body_hash) AND m.extraction_hash=? "
        "WHERE d.source_key=? AND d.kind='announcement' "
        "AND (SELECT COUNT(*) FROM disclosure_codes members WHERE members.disclosure_id=d.id)=1",
        (extraction_hash, source_key),
    ).fetchall()
    if len(rows) != 1:
        raise ValueError("company_material_recovery_required_version_missing")
    row = rows[0]
    if (datetime.fromisoformat(row[6]) > observed_at
        or hashlib.sha256(text.encode("utf-8")).digest() != row[3]
        or not _has_valid_material_receipt(formal, row[1], source_key, row[5], row[2], row[3], extraction_hash)):
        raise ValueError("company_material_recovery_identity_mismatch")
    issuer_codes = ExtractCompanyAnnouncementNumbers(text).issuer_codes
    if issuer_codes and row[5] not in issuer_codes:
        raise ValueError("company_material_recovery_issuer_mismatch")
    try:
        documents.read(row[3], check=check)
    except FileNotFoundError:
        missing = True
    else:
        missing = False
    if not missing and row[4]:
        return False
    with documents.prepare(text, check=check, reserve=reserve):
        check()
        collected.run("UPDATE disclosures SET body_retained=1,body_checked=1,body_hash=? WHERE id=?", (row[3], row[0]))
        collected.run("INSERT OR IGNORE INTO company_dirty_codes VALUES(?)", (row[5],))
    return True

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
                ).fetchone()
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

def _field_attempt_rule(gap: CompanyFieldGap) -> str:
    if gap.reason == "measurement_missing":
        for field, rule in COMPANY_FIELD_RULES:
            if gap.field == field:
                return rule
    if gap.reason in {"measurement_missing", "dependency_missing", "ambiguous", "implementation_missing"}:
        return _REQUIRED_FIELD_EVIDENCE_RULE
    raise ValueError("company_field_attempt_not_applicable")

def _local_file(root: Path, relative: object) -> bool:
    if not isinstance(relative, str):
        return False
    candidate = (root / relative).resolve()
    return candidate.is_relative_to(root.resolve()) and candidate.is_file()

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

__all__ = ["COMPANY_FIELD_RULES", "CompanyMaterialDependency", "ReadCompanyFieldRecovery", "RegisterCompanyMaterialDependencies", "RestoreCompanyMaterialDependencies", "RecoverRequiredCompanyText", "ReadRequiredCompanyMaterialDependencies"]
