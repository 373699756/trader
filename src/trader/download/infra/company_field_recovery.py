"""Field repair tasks belong to the collection ledger, never to a second facts source."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from itertools import groupby
from pathlib import Path
from dataclasses import replace
import hashlib

from trader.download.domain.company_events import (
    CompanyPositiveEvent, CompanyPositiveStatus, CompanyRiskEvent, IncreaseMeasurements, RepurchaseMeasurements,
)
from trader.download.domain.company_evidence import CompanyDocumentFailureRules
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
from trader.download.infra.company_facts_index import CompanyInputReceipt
from trader.download.infra.company_facts_protocol import RequireCompanyTime
from trader.infra.sqlite_connection import SqliteConnection

# These rules change measurements only; old document receipts keep their identity.
# Unknown dependencies/subjects still require verification and are not download jobs.
COMPANY_FIELD_RULES = (
    ("planned_pct", "reduction_total_capital_ratio:1"),
    ("actual_pct", "implemented_total_capital_ratio:1"),
    ("contract_amount", "explicit_cny_contract_amount:1"),
    ("revenue_report_date", "explicit_contract_revenue_year:1"),
)
_REQUIRED_FIELD_EVIDENCE_RULE = "required_field_evidence:1"


def EnsureCompanyFieldRepairs(connection: SqliteConnection) -> None:
    connection.run(
        "CREATE TABLE IF NOT EXISTS company_field_repairs("
        "disclosure_id INTEGER NOT NULL REFERENCES disclosures(id) ON DELETE CASCADE,"
        "code TEXT NOT NULL,record_hash TEXT NOT NULL,event_id TEXT NOT NULL,field TEXT NOT NULL,rule_id TEXT NOT NULL,"
        "state TEXT NOT NULL CHECK(state IN ('pending','attempted','complete')),"
        "PRIMARY KEY(disclosure_id,code,record_hash,event_id,field,rule_id)) WITHOUT ROWID"
    )


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
        events: tuple[CompanyRiskEvent | CompanyPositiveEvent, ...] = tuple(
            event for fact in facts for event in fact.risk_events
        ) + tuple(event for fact in facts for event in fact.positive_events)
        gaps = tuple(gap for fact in facts for gap in _field_gaps(fact, collected))
        materials: list[CompanyFieldMaterial] = []
        for key in sorted({gap.evidence_id for gap in gaps}):
            rows = collected.run(
                "SELECT d.published_at,d.body_retained,d.body_hash,t.path,r.failure "
                "FROM disclosures d LEFT JOIN document_tasks t ON t.source=d.source "
                "AND t.source_key=d.source_key AND t.record_hash=d.record_hash "
                "LEFT JOIN document_retries r ON r.disclosure_id=d.id AND r.record_hash=d.record_hash "
                "WHERE d.source_key=? AND d.kind='announcement'", (key,),
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
                "AND d.record_hash=f.record_hash WHERE d.source_key=? AND f.code=? "
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
            "SELECT f.disclosure_id,f.record_hash,f.field,f.rule_id,f.event_id,d.body_checked "
            "FROM company_field_repairs f "
            "JOIN disclosures d ON d.id=f.disclosure_id AND d.record_hash=f.record_hash "
            "WHERE f.code=? AND f.state<>'complete'", (fact.code,),
        ).fetchall()
        for row in rows:
            available = CompanyFieldIsAvailable(fact, row[4], row[2])
            if not available and (not row[5] or not finish_attempts):
                continue
            missing = any(gap.event_id == row[4] and gap.field == row[2] for gap in gaps)
            if not available and not missing:
                continue
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
        source = connection.run("SELECT title FROM disclosures WHERE source_key=?", (event.evidence_ids[-1],)).fetchall()
        if len(source) == 1 and any(marker in source[0][0] for marker in ("进展", "实施情况", "结果", "完成")):
            gaps.append(CompanyFieldGap(fact.code, event.event_id, event.evidence_ids[-1], "actual_pct", "measurement_missing"))
    return tuple(gaps)


__all__ = [
    "COMPANY_FIELD_RULES",
    "EnsureCompanyFieldRepairs",
    "ReadCompanyFieldRecovery",
    "QueueCompanyFieldRepairs",
    "RegisterCompanyFieldAttempts",
    "CompleteCompanyFieldRepairs",
    "CompanyFieldRepairReceipts",
    "RestoreCompanyFieldRepairs",
]
