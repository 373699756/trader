"""Prepare typed local facts, exact proof inputs and public links before commit."""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime

from trader.download.domain.company_document_links import CompanyPublishedEvidenceLink
from trader.download.domain.company_annual_income import CompanyAnnualIncome, ApplyCompanyAnnualIncome, SelectCompanyAnnualIncome
from trader.download.domain.company_events import CompanyEventPolicy, CompanyEvidenceLink, CompanyPositiveType, RepurchaseMeasurements
from trader.download.domain.company_event_extraction import IsCompanyRepurchaseCancellationOnly
from trader.download.domain.company_evidence import CompanyDocument, CompanyRatio
from trader.download.domain.company_facts import CompanyFacts
from trader.download.domain.company_field_results import CompanyFieldConclusion
from trader.download.domain.company_measurement_corrections import CorrectCompanyRepurchaseDisposalMeasurements, RefineCompanyIsolatedPositiveMeasurements
from trader.download.infra.company_sync_inputs import CompanyRecoveredInput
from trader.download.infra.company_field_publication import CompanyFieldProofRulesHash, PrepareCompanyFieldResults, StoredCompanyFieldResults
from trader.download.infra.company_announcement_preparation import CompanyAnnouncementCoverage, PrepareCompanyAnnouncements
from trader.download.infra.company_facts_index import CompanyInputReceipt
from trader.download.infra.company_fact_inputs import DecodeCompanyDisclosure
from trader.infra.sqlite_connection import SqliteConnection


@dataclass(frozen=True, slots=True)
class CompanyIssuerPreparation:
    facts: CompanyFacts
    fields: tuple[CompanyFieldConclusion, ...]
    receipts: tuple[CompanyInputReceipt, ...]
    links: tuple[CompanyPublishedEvidenceLink, ...]
    corrected_event_ids: tuple[str, ...]
    pending_retained: int


def PrepareCompanyIssuerInputs(
    collected: SqliteConnection, formal: SqliteConnection, previous: CompanyFacts, *, cutoff: datetime,
    extraction_hash: bytes, event_policy: CompanyEventPolicy, read_document: Callable[[bytes], str],
    check: Callable[[], None], revalidate: bool, include_new_announcements: bool, correct_measurements: bool = True,
    published_links: tuple[CompanyPublishedEvidenceLink, ...] = (),
    recovered: tuple[CompanyRecoveredInput, ...] = (),
    annual_incomes: tuple[CompanyAnnualIncome, ...] = (),
    unlock_observations: tuple[CompanyRatio, ...] = (),
    field_rules_hash: bytes | None = None,
    announcement_coverage: CompanyAnnouncementCoverage | None = None,
    stored_fields: StoredCompanyFieldResults | None = None,
) -> CompanyIssuerPreparation:
    """Use the sole field proofer and only valid-backed exact bodies for corrections."""
    rules_hash = CompanyFieldProofRulesHash() if field_rules_hash is None else field_rules_hash
    original = previous
    announcement = PrepareCompanyAnnouncements(collected, formal, previous, cutoff=cutoff, extraction_hash=extraction_hash,
        rule_identity=rules_hash, event_policy=event_policy, read_document=read_document, check=check,
        unlock_observations=unlock_observations, coverage=announcement_coverage) if include_new_announcements and correct_measurements else None
    if announcement is not None:
        previous = announcement.facts
    if (announcement is not None and announcement_coverage is not None and not announcement_coverage.has_retained_material
        and not revalidate and stored_fields is not None and stored_fields.code == previous.code and stored_fields.rules_hash == rules_hash
        and stored_fields.extraction_hash == extraction_hash and not stored_fields.results and not published_links
        and not previous.risk_events and not previous.positive_events
        and all(reason.evidence_id is None for reason in previous.review_reasons)
        and not any(item.code == previous.code for item in recovered)):
        return CompanyIssuerPreparation(previous, (), announcement.receipts, announcement.links, (), announcement.pending_retained)
    annual_receipts: list[CompanyInputReceipt] = []
    if correct_measurements and annual_incomes:
        annual_incomes = tuple(income for income in annual_incomes if formal.run(
            """SELECT 1 FROM company_input_receipt WHERE source='eastmoney_annual_income'
            AND source_key=? AND code=? AND record_hash<>? AND outcome='valid' LIMIT 1""",
            (income.source_key, previous.code, income.record_hash)).fetchone() is None)
        financials = tuple(row for row in (previous.financial, previous.latest_financial)
            if row is not None and row.published_at <= cutoff)
        events = ApplyCompanyAnnualIncome(previous.code, previous.positive_events, annual_incomes, cutoff, financials=financials)
        for event, updated in zip(previous.positive_events, events, strict=True):
            if event.measurements == updated.measurements:
                continue
            income = SelectCompanyAnnualIncome(previous.code, event, annual_incomes, cutoff, financials=financials)
            if income is None:
                raise ValueError("company_annual_income_selection_missing")
            receipt = CompanyInputReceipt("eastmoney_annual_income", income.source_key, previous.code,
                income.record_hash, extraction_hash, None, cutoff, "valid", "annual_income_point_in_time")
            if receipt not in annual_receipts:
                annual_receipts.append(receipt)
        if events != previous.positive_events:
            previous = replace(previous, positive_events=events, published_at=cutoff)
    identities = tuple(sorted({identity for event in previous.risk_events + previous.positive_events for identity in event.evidence_ids}
                              | {review.evidence_id for review in previous.review_reasons if review.evidence_id is not None}))
    documents: list[CompanyDocument] = list(announcement.documents) if announcement is not None else []
    links: list[CompanyPublishedEvidenceLink] = list(announcement.links) if announcement is not None else []
    directory_receipts: list[CompanyInputReceipt] = [*annual_receipts, *(announcement.receipts if announcement is not None else ())]
    recovered_ids = frozenset(item.source_key for item in recovered if item.code == previous.code)
    correction_ids = frozenset(identity for event in previous.positive_events
        if correct_measurements and event.event_type is CompanyPositiveType.SHARE_REPURCHASE
        and isinstance(event.measurements, RepurchaseMeasurements) and event.measurements.actual_ratio_pct is not None
        and len(event.evidence_ids) == 1 and event.first_disclosed_at == event.last_disclosed_at
        for identity in event.evidence_ids)
    for identity in identities:
        check()
        rows = collected.run("""SELECT d.*,c.code FROM disclosures d JOIN disclosure_codes c ON c.disclosure_id=d.id
            WHERE c.code=? AND d.source_key=? AND d.published_at<=? ORDER BY d.source""", (previous.code, identity, cutoff.isoformat())).fetchall()
        # A public evidence ID cannot silently choose between suppliers.
        if len(rows) != 1:
            continue
        row = rows[0]
        record = row["record_hash"]
        if not isinstance(record, str) or len(record) != 64 or any(character not in "0123456789abcdef" for character in record):
            continue
        body_hash = row["body_hash"]
        pending_receipt = next((receipt for receipt in directory_receipts if (receipt.code, receipt.source, receipt.source_key,
            receipt.record_hash, receipt.body_hash) == (previous.code, row["source"], identity, record, body_hash)), None)
        valid = isinstance(body_hash, bytes) and len(body_hash) == 32 and (pending_receipt is not None and pending_receipt.outcome == "valid" or formal.run(
            """SELECT 1 FROM company_input_receipt WHERE source=? AND source_key=? AND code=? AND record_hash=?
            AND extraction_hash=? AND body_hash=? AND outcome='valid' LIMIT 1""",
            (row["source"], identity, previous.code, record, extraction_hash, body_hash),
        ).fetchone() is not None)
        disclosure = DecodeCompanyDisclosure(row)
        checked = bool(valid and row["body_checked"])
        previous_link = next((item for item in published_links if item.code == previous.code and item.source == disclosure.source
            and item.link.evidence_id == identity), None)
        if not any(link.link.evidence_id == identity for link in links) and (previous_link is None or previous_link.record_hash == record):
            links.append(CompanyPublishedEvidenceLink(previous.code, disclosure.source, record,
                CompanyEvidenceLink(identity, disclosure.title, disclosure.url, disclosure.published_at, checked)))
        elif previous_link is not None and previous_link.record_hash != record:
            links.append(previous_link)
        if not valid and pending_receipt is None and formal.run("SELECT 1 FROM company_input_receipt WHERE source=? AND source_key=? AND code=? AND record_hash=? AND extraction_hash=?",
            (disclosure.source, identity, previous.code, record, extraction_hash)).fetchone() is None:
            directory_receipts.append(CompanyInputReceipt(disclosure.source, identity, previous.code, record,
                extraction_hash, None, cutoff, "directory_observed", ""))
        if any(document.disclosure.identity == identity for document in documents):
            continue
        if ((identity not in correction_ids or not IsCompanyRepurchaseCancellationOnly(row["title"])) and identity not in recovered_ids
            or not checked or not row["body_retained"] or row["kind"] != "announcement"
            or collected.run("SELECT COUNT(*) FROM disclosure_codes WHERE disclosure_id=?", (row["id"],)).require_one()[0] != 1):
            continue
        assert isinstance(body_hash, bytes)
        try:
            text = read_document(body_hash)
        except InterruptedError:
            raise
        except (OSError, ValueError, EOFError):
            continue
        if hashlib.sha256(text.encode()).digest() == body_hash:
            documents.append(CompanyDocument(disclosure, text, True))
    facts = CorrectCompanyRepurchaseDisposalMeasurements(previous, tuple(documents), cutoff, event_policy) if correct_measurements else previous
    if recovered_ids:
        fresh = tuple(document for document in documents if any(item.code == previous.code and item.source == document.disclosure.source
            and item.source_key == document.disclosure.identity and item.record_hash == document.disclosure.digest
            and item.body_hash == hashlib.sha256(document.text.encode()).digest() for item in recovered))
        facts = RefineCompanyIsolatedPositiveMeasurements(facts, fresh, cutoff, event_policy)
    prepared = PrepareCompanyFieldResults(collected, formal, facts, tuple(documents), observed_at=cutoff,
        extraction_hash=extraction_hash, event_policy=event_policy, read_document=read_document, revalidate=revalidate,
        field_rules_hash=rules_hash, stored=stored_fields)
    corrected = tuple(item.event_id for item in facts.positive_events if item not in original.positive_events)
    return CompanyIssuerPreparation(facts, prepared.results, (*prepared.receipts, *directory_receipts), tuple(links), corrected,
        announcement.pending_retained if announcement is not None else 0)


__all__ = ["CompanyIssuerPreparation", "PrepareCompanyIssuerInputs"]
