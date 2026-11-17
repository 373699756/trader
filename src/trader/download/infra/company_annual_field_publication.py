"""Bind an independent annual source to a contract field without creating case links."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from trader.download.domain.company_annual_report import COMPANY_ANNUAL_REPORT_DIGEST, COMPANY_ANNUAL_REPORT_DISCLOSURE_RULE, ExtractCompanyAnnualReportRevenue
from trader.download.domain.company_events import CompanyPositiveEvent, CompanySubject, ContractMeasurements
from trader.download.domain.company_evidence import CompanyDocument, Disclosure
from trader.download.domain.company_facts import CompanyFacts
from trader.download.domain.company_field_results import CompanyFieldConclusion, CompanyFieldEvidence
from trader.download.domain.company_field_source import BuildCompanyFieldEvidence
from trader.download.infra.company_fact_inputs import DecodeCompanyDisclosure
from trader.download.infra.company_facts_index import CompanyInputReceipt
from trader.infra.sqlite_connection import SqliteConnection
from trader.infra.serialization.canonical_codec import CanonicalDigest


_RULE = "explicit_cny_annual_operating_revenue:1"


@dataclass(frozen=True, slots=True)
class CompanyAnnualFieldPublication:
    results: tuple[CompanyFieldConclusion, ...]
    receipts: tuple[CompanyInputReceipt, ...]


@dataclass(frozen=True, slots=True)
class _AnnualSource:
    disclosure: Disclosure
    body_hash: bytes | None
    body_checked: bool
    body_retained: bool


def IsCompanyAnnualFieldEvidence(
    event: CompanyPositiveEvent,
    field: str,
    proof: CompanyFieldEvidence,
) -> bool:
    measurement = event.measurements
    return (isinstance(measurement, ContractMeasurements) and measurement.reference_report_date is not None
            and (field == "annual_revenue" and proof.rule_id == _RULE and proof.unit == "cny"
                 or field == "revenue_disclosed_at" and proof.rule_id == COMPANY_ANNUAL_REPORT_DISCLOSURE_RULE
                 and proof.unit == "time" and proof.published_at == measurement.reference_disclosed_at)
            and proof.subject is event.subject_type is CompanySubject.ISSUER
            and proof.published_at.date() < event.first_disclosed_at.date()
            and f"{measurement.reference_report_date.year}年" in "".join(proof.quote.split()))


def _source_rows(collected: SqliteConnection, facts: CompanyFacts, event: CompanyPositiveEvent) -> tuple[_AnnualSource, ...]:
    measurement = event.measurements
    if not isinstance(measurement, ContractMeasurements) or measurement.reference_report_date is None:
        return ()
    rows = collected.run(
        "SELECT d.*,c.code FROM disclosures d JOIN disclosure_codes c ON c.disclosure_id=d.id "
        "WHERE c.code=? AND d.kind='announcement' AND d.published_at>=? AND d.published_at<? AND d.title LIKE ? "
        "AND (SELECT COUNT(*) FROM disclosure_codes members WHERE members.disclosure_id=d.id)=1 "
        "ORDER BY d.published_at DESC,d.source_key LIMIT 9",
        (facts.code, (facts.checked_at.date() - timedelta(days=365)).isoformat(),
         event.first_disclosed_at.date().isoformat(), f"%{measurement.reference_report_date.year}%年度报告%"),
    ).fetchall()
    sources: list[_AnnualSource] = []
    for row in rows:
        if row["published_at"] != rows[0]["published_at"]:
            continue
        body_hash = row["body_hash"]
        if body_hash is not None and (not isinstance(body_hash, bytes) or len(body_hash) != 32):
            raise ValueError("company_annual_source_body_hash_invalid")
        sources.append(_AnnualSource(DecodeCompanyDisclosure(row), body_hash, bool(row["body_checked"]), bool(row["body_retained"])))
    return tuple(sources)


def _policy(event: CompanyPositiveEvent, item: CompanyFieldConclusion, rows: Sequence[_AnnualSource]) -> str:
    return "annual_source:" + CanonicalDigest((
        COMPANY_ANNUAL_REPORT_DIGEST.hex(), event.event_id, event.first_disclosed_at, item.field, item.value,
        tuple((row.disclosure, row.body_hash.hex() if row.body_hash is not None else None) for row in rows),
    ))


def CanReuseCompanyAnnualFieldResults(
    collected: SqliteConnection,
    formal: SqliteConnection,
    facts: CompanyFacts,
    event: CompanyPositiveEvent,
    item: CompanyFieldConclusion,
) -> bool:
    rows = _source_rows(collected, facts, event)
    if (not rows or len(rows) > 8 or len(item.evidence) != len(rows)
        or not all(IsCompanyAnnualFieldEvidence(event, item.field, proof) for proof in item.evidence)):
        return False
    identities = {(row.disclosure.source, row.disclosure.identity, row.disclosure.digest, row.body_hash,
                   row.disclosure.published_at.isoformat()) for row in rows}
    if identities != {(proof.source, proof.source_key, proof.record_hash, proof.body_hash, proof.published_at.isoformat())
                      for proof in item.evidence}:
        return False
    policy = _policy(event, item, rows)
    return all(formal.run(
        "SELECT 1 FROM company_input_receipt WHERE source=? AND source_key=? AND code=? AND record_hash=? "
        "AND body_hash=? AND extraction_hash=? AND outcome='field_attempted' AND policy_id=?",
        (proof.source, proof.source_key, facts.code, proof.record_hash, proof.body_hash,
         hashlib.sha256(policy.encode()).digest(), policy),
    ).fetchone() is not None for proof in item.evidence)


def PrepareCompanyAnnualFieldResults(
    collected: SqliteConnection,
    formal: SqliteConnection,
    facts: CompanyFacts,
    results: Sequence[CompanyFieldConclusion],
    documents: Sequence[CompanyDocument],
    *,
    observed_at: datetime,
    extraction_hash: bytes,
    read_document: Callable[[bytes], str],
    revalidate: bool = False,
) -> CompanyAnnualFieldPublication:
    prepared = list(results)
    receipts: list[CompanyInputReceipt] = []
    for position, item in enumerate(prepared):
        if item.field not in {"annual_revenue", "revenue_disclosed_at"} or not item.applicable or item.state == "verified" or item.value is None:
            continue
        event = next(event for event in facts.positive_events if event.event_id == item.event_id)
        measurement = event.measurements
        if (not isinstance(measurement, ContractMeasurements) or measurement.reference_report_date is None
            or event.subject_type is not CompanySubject.ISSUER):
            continue
        rows = _source_rows(collected, facts, event)
        if not rows or len(rows) > 8:
            continue
        candidates: list[CompanyFieldEvidence] = []
        conflicting = False
        policy = _policy(event, item, rows)
        for row in rows:
            body_hash = row.body_hash
            disclosure = row.disclosure
            if body_hash is None or not row.body_checked or not row.body_retained:
                conflicting = True
                continue
            if not revalidate and formal.run(
                "SELECT 1 FROM company_input_receipt WHERE source=? AND source_key=? AND code=? "
                "AND record_hash=? AND body_hash=? AND extraction_hash=? AND outcome='field_attempted' AND policy_id=?",
                (disclosure.source, disclosure.identity, facts.code, disclosure.digest, body_hash,
                 hashlib.sha256(policy.encode()).digest(), policy),
            ).fetchone() is not None:
                conflicting = True
                continue
            document = next((document for document in documents if document.disclosure == disclosure), None)
            if document is None:
                if formal.run(
                    "SELECT 1 FROM company_input_receipt WHERE source=? AND source_key=? AND code=? AND record_hash=? "
                    "AND body_hash=? AND extraction_hash=? AND outcome='valid'",
                    (disclosure.source, disclosure.identity, facts.code, disclosure.digest, body_hash, extraction_hash),
                ).fetchone() is None:
                    conflicting = True
                    continue
                try:
                    text = read_document(body_hash)
                except InterruptedError:
                    raise
                except (OSError, ValueError, EOFError):
                    conflicting = True
                    continue
                document = CompanyDocument(disclosure, text, True)
            if hashlib.sha256(document.text.encode()).digest() != body_hash:
                conflicting = True
                continue
            parsed = ExtractCompanyAnnualReportRevenue(document)
            if parsed is not None and parsed.report_date == measurement.reference_report_date:
                actual_value = parsed.revenue if item.field == "annual_revenue" else disclosure.published_at
                if actual_value != item.value:
                    conflicting = True
                else:
                    proof = BuildCompanyFieldEvidence(
                        document, parsed.statement,
                        _RULE if item.field == "annual_revenue" else COMPANY_ANNUAL_REPORT_DISCLOSURE_RULE,
                        "cny" if item.field == "annual_revenue" else "time", event.subject_type,
                    )
                    if proof is not None:
                        candidates.append(proof)
                    else:
                        conflicting = True
            else:
                conflicting = True
            receipts.append(CompanyInputReceipt(
                disclosure.source, disclosure.identity, facts.code, disclosure.digest, hashlib.sha256(policy.encode()).digest(),
                body_hash, observed_at, "field_attempted", policy,
            ))
        if candidates and not conflicting:
            prepared[position] = replace(item, state="verified", reason=(
                "explicit_annual_source_revenue" if item.field == "annual_revenue" else "official_annual_catalog_disclosure"
            ), evidence=tuple(candidates))
    return CompanyAnnualFieldPublication(tuple(prepared), tuple(receipts))


__all__ = [
    "CompanyAnnualFieldPublication",
    "IsCompanyAnnualFieldEvidence",
    "CanReuseCompanyAnnualFieldResults",
    "PrepareCompanyAnnualFieldResults",
]
