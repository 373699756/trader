"""Bind field conclusions to current source material before the sole formal publication."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import cast

from trader.download.domain.company_event_extraction import ExtractCompanyFieldEvidence
from trader.download.domain.company_contract_fields import COMPANY_CONTRACT_FIELD_RULES
from trader.download.domain.company_events import CompanyEventPolicy, CompanyPositiveEvent, CompanyRiskEvent
from trader.download.domain.company_evidence import CompanyDocument
from trader.download.domain.company_facts import CompanyFacts
from trader.download.domain.company_field_plan import BuildCompanyFieldResults
from trader.download.domain.company_field_results import (
    CompanyFieldConclusion,
    CompanyFieldConclusionState,
    CompanyFieldName,
)
from trader.download.infra.company_fact_inputs import DecodeCompanyDisclosure
from trader.download.infra.company_facts_index import CompanyInputReceipt, ReadCompanyPublicationIdentity
from trader.download.infra.company_field_recovery import COMPANY_FIELD_RULES
from trader.download.infra.company_field_result_rows import ReadCompanyFieldResults
from trader.infra.sqlite_connection import SqliteConnection

_PROOF_RULE = "field_source_lines:1"
_RISK_FIELDS = frozenset({"start_date", "end_date", "planned_pct", "completion_disclosed_at"})
_SUPPORTED_FIELDS = _RISK_FIELDS | {"actual_pct", "repurchase_purpose"} | {field for field, _ in COMPANY_CONTRACT_FIELD_RULES}


@dataclass(frozen=True, slots=True)
class CompanyFieldPublication:
    results: tuple[CompanyFieldConclusion, ...]
    receipts: tuple[CompanyInputReceipt, ...]


def PrepareCompanyFieldResults(
    collected: SqliteConnection,
    formal: SqliteConnection,
    facts: CompanyFacts,
    documents: Sequence[CompanyDocument],
    *,
    observed_at: datetime,
    extraction_hash: bytes,
    event_policy: CompanyEventPolicy,
    read_document: Callable[[bytes], str],
) -> CompanyFieldPublication:
    """Reuse current proofs; inspect an unchanged old input once per field rule.

    Failed proof extraction is an attempted input, never non-disclosure. Its
    receipt commits with the facts and conclusions, so interruption cannot make
    a failed attempt complete or force another download of a retained body.
    """
    baseline = tuple(CompanyFieldConclusion(
        item.code, item.event_id, cast(CompanyFieldName, item.field), item.value,
        cast(CompanyFieldConclusionState, item.state), item.applicable, item.reason, (),
    ) for item in BuildCompanyFieldResults(facts))
    publication = ReadCompanyPublicationIdentity(formal)
    previous = ReadCompanyFieldResults(formal, (facts.code,)) if (
        publication is not None and publication.extraction_hash == extraction_hash
    ) else ()
    results = list(baseline)
    for position, item in enumerate(results):
        existing = next((old for old in previous if old.event_id == item.event_id and old.field == item.field), None)
        event = next((event for event in _field_events(facts, item.field) if event.event_id == item.event_id), None)
        if (existing is not None and existing.state == "verified" and item.applicable
            and type(existing.value) is type(item.value) and existing.value == item.value
            and event is not None
            and all(proof.source_key in event.evidence_ids and proof.subject is event.subject_type
                    and (item.field, proof.rule_id) in COMPANY_FIELD_RULES
                    and _current_material(collected, facts, proof.source, proof.source_key, proof.record_hash,
                                          proof.body_hash, proof.published_at) for proof in existing.evidence)):
            results[position] = existing
    inputs: list[CompanyDocument] = []
    receipts: list[CompanyInputReceipt] = []
    for item in tuple(results):
        if item.state == "verified" or not item.applicable or item.value is None or item.field not in _SUPPORTED_FIELDS:
            continue
        event = next((event for event in _field_events(facts, item.field) if event.event_id == item.event_id), None)
        if event is None:
            continue
        for evidence_id in reversed(event.evidence_ids):
            rows = collected.run(
                "SELECT d.*,c.code FROM disclosures d JOIN disclosure_codes c ON c.disclosure_id=d.id "
                "WHERE c.code=? AND d.source_key=? AND d.kind='announcement' AND d.body_checked=1 "
                "AND d.body_retained=1 AND d.published_at<=? "
                "AND (SELECT COUNT(*) FROM disclosure_codes members WHERE members.disclosure_id=d.id)=1",
                (facts.code, evidence_id, observed_at.isoformat()),
            ).fetchall()
            if len(rows) != 1:
                continue
            row = rows[0]
            body_hash = row["body_hash"]
            if not isinstance(body_hash, bytes) or len(body_hash) != 32:
                continue
            policy = _field_policy(item.event_id, item.field)
            if formal.run(
                "SELECT 1 FROM company_input_receipt WHERE source=? AND source_key=? AND code=? "
                "AND record_hash=? AND body_hash=? AND extraction_hash=? AND policy_id=? AND outcome='field_attempted'",
                (row["source"], evidence_id, facts.code, row["record_hash"], body_hash,
                 hashlib.sha256(policy.encode()).digest(), policy),
            ).fetchone() is not None:
                continue
            document = next((document for document in (*inputs, *documents)
                             if document.disclosure.source == row["source"]
                             and document.disclosure.identity == evidence_id
                             and document.disclosure.digest == row["record_hash"]), None)
            if document is not None:
                if (not document.checked or document.terminal_failure or document.disclosure.codes != (facts.code,)
                    or document.disclosure.title != row["title"] or document.disclosure.kind != row["kind"]
                    or document.disclosure.url != row["url"]
                    or document.disclosure.published_at.isoformat() != row["published_at"]
                    or hashlib.sha256(document.text.encode()).digest() != body_hash):
                    continue
            else:
                if formal.run(
                    "SELECT 1 FROM company_input_receipt WHERE source=? AND source_key=? AND code=? "
                    "AND record_hash=? AND body_hash=? AND extraction_hash=? AND outcome='valid'",
                    (row["source"], evidence_id, facts.code, row["record_hash"], body_hash, extraction_hash),
                ).fetchone() is None:
                    continue
                try:
                    text = read_document(body_hash)
                except InterruptedError:
                    raise
                except (OSError, ValueError, EOFError):
                    continue
                if hashlib.sha256(text.encode()).digest() != body_hash:
                    continue
                document = CompanyDocument(DecodeCompanyDisclosure(row), text, True)
            if document not in inputs:
                inputs.append(document)
            receipts.append(CompanyInputReceipt(
                row["source"], evidence_id, facts.code, row["record_hash"], hashlib.sha256(policy.encode()).digest(),
                body_hash, observed_at, "field_attempted", policy,
            ))
    candidates = tuple(candidate for document in inputs
                       for candidate in ExtractCompanyFieldEvidence(document, observed_at, event_policy))
    for candidate in candidates:
        if candidate.code != facts.code or not candidate.evidence:
            continue
        proof = candidate.evidence[0]
        if any((candidate.field, part.rule_id) not in COMPANY_FIELD_RULES for part in candidate.evidence):
            raise ValueError("company_field_evidence_rule_mismatch")
        events = tuple(event for event in _field_events(facts, candidate.field)
                       if proof.source_key in event.evidence_ids
                       and proof.subject is event.subject_type
                       and candidate.event_id == f"{proof.source_key}:" + (
                           event.risk_type.value if isinstance(event, CompanyRiskEvent) else event.event_type.value
                       ))
        if len(events) != 1 or not all(
            (part.source, part.source_key, part.record_hash, part.body_hash, part.published_at, part.subject)
            == (proof.source, proof.source_key, proof.record_hash, proof.body_hash, proof.published_at, proof.subject)
            and _current_material(collected, facts, part.source, part.source_key, part.record_hash, part.body_hash, part.published_at)
            for part in candidate.evidence
        ):
            continue
        event = events[0]
        for position, item in enumerate(results):
            if (item.event_id == event.event_id and item.field == candidate.field and item.applicable
                and type(item.value) is type(candidate.value) and item.value == candidate.value):
                results[position] = replace(candidate, event_id=event.event_id)
    return CompanyFieldPublication(tuple(results), tuple(receipts))


def _field_events(facts: CompanyFacts, field: str) -> Sequence[CompanyRiskEvent | CompanyPositiveEvent]:
    return facts.risk_events if field in _RISK_FIELDS else facts.positive_events


def _current_material(
    collected: SqliteConnection,
    facts: CompanyFacts,
    source: str,
    source_key: str,
    record_hash: str,
    body_hash: bytes,
    published_at: datetime,
) -> bool:
    if published_at > facts.checked_at:
        return False
    rows = collected.run(
        "SELECT d.record_hash,d.body_hash,d.published_at FROM disclosures d "
        "JOIN disclosure_codes c ON c.disclosure_id=d.id WHERE d.source=? AND d.source_key=? AND c.code=? "
        "AND (SELECT COUNT(*) FROM disclosure_codes members WHERE members.disclosure_id=d.id)=1",
        (source, source_key, facts.code),
    ).fetchall()
    return len(rows) == 1 and tuple(rows[0]) == (record_hash, body_hash, published_at.isoformat())


def _field_policy(event_id: str, field: str) -> str:
    rule = next(rule for name, rule in COMPANY_FIELD_RULES if name == field)
    return f"{_PROOF_RULE}:{rule}:{event_id}:{field}"


__all__ = [
    "CompanyFieldPublication",
    "PrepareCompanyFieldResults",
]
