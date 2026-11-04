"""Field repair tasks belong to the collection ledger, never to a second facts source."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
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
    PlanCompanyFieldRecovery,
)
from trader.download.infra.company_fact_rows import ReadCompanyFactRows
from trader.download.infra.company_facts_index import CompanyInputReceipt
from trader.infra.sqlite_connection import SqliteConnection

# These rules change measurements only; old document receipts keep their identity.
# Unknown dependencies/subjects still require verification and are not download jobs.
COMPANY_FIELD_RULES = (
    ("planned_pct", "reduction_total_capital_ratio:1"),
    ("actual_pct", "implemented_total_capital_ratio:1"),
    ("contract_amount", "explicit_cny_contract_amount:1"),
)


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
            rule = next((rule for field, rule in COMPANY_FIELD_RULES if field == item.gap.field), None)
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
            recoveries.append(replace(item, action="verify", reason="same_rule_already_attempted") if attempted else item)
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


def CompleteCompanyFieldRepairs(connection: SqliteConnection, facts: Sequence[CompanyFacts]) -> None:
    """Formal commit owns success; unresolved attempts retain material without retry loops."""
    for fact in facts:
        gaps = _field_gaps(fact, connection)
        rows = connection.run(
            "SELECT f.disclosure_id,f.record_hash,f.field,f.rule_id,f.event_id FROM company_field_repairs f "
            "JOIN disclosures d ON d.id=f.disclosure_id AND d.record_hash=f.record_hash "
            "WHERE f.code=? AND d.body_checked=1", (fact.code,),
        ).fetchall()
        for row in rows:
            missing = any(gap.event_id == row[4] and gap.field == row[2] for gap in gaps)
            connection.run(
                "UPDATE company_field_repairs SET state=? WHERE disclosure_id=? AND code=? "
                "AND record_hash=? AND field=? AND rule_id=? AND event_id=?",
                ("attempted" if missing else "complete", row[0], fact.code, row[1], row[2], row[3], row[4]),
            )


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
    for row in collected.run(
        "SELECT f.disclosure_id,f.code,f.record_hash,f.event_id,f.field,f.rule_id,d.source,d.source_key "
        "FROM company_field_repairs f JOIN disclosures d ON d.id=f.disclosure_id "
        "AND d.record_hash=f.record_hash WHERE f.state='pending'",
    ).fetchall():
        policy = _repair_policy(row[3], row[4], row[5])
        receipt = formal.run(
            "SELECT 1 FROM company_input_receipt WHERE source=? AND source_key=? AND code=? "
            "AND record_hash=? AND extraction_hash=? AND outcome='field_attempted' AND policy_id=?",
            (row[6], row[7], row[1], row[2], hashlib.sha256(policy.encode()).digest(), policy),
        ).fetchone()
        if receipt is not None:
            gaps = tuple(gap for fact in ReadCompanyFactRows(formal, (row[1],)) for gap in _field_gaps(fact, collected))
            missing = any(gap.event_id == row[3] and gap.field == row[4] for gap in gaps)
            collected.run(
                "UPDATE company_field_repairs SET state=? WHERE disclosure_id=? AND code=? "
                "AND record_hash=? AND event_id=? AND field=? AND rule_id=?",
                ("attempted" if missing else "complete", *row[:6]),
            )


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
    "CompleteCompanyFieldRepairs",
    "CompanyFieldRepairReceipts",
    "RestoreCompanyFieldRepairs",
]
