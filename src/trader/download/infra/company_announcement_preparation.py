"""Read exact retained official documents before the sole normalized publication."""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from trader.download.domain.company_announcement_normalization import NormalizeCompanyAnnouncementFacts
from trader.download.domain.company_document_links import CompanyPublishedEvidenceLink
from trader.download.domain.company_event_extraction import CompanyDocumentRiskRelevant
from trader.download.domain.company_events import CompanyEventPolicy, CompanyEvidenceLink, CompanyReviewReason
from trader.download.domain.company_evidence import CompanyDocument, EvidenceMarket
from trader.download.domain.company_facts import CompanyFacts
from trader.download.infra.company_announcement_links import LinkCompanyAnnouncementNumbers
from trader.download.infra.company_fact_inputs import CompanyCoverageComplete, DecodeCompanyDisclosure
from trader.download.infra.company_facts_index import CompanyInputReceipt
from trader.download.infra.company_facts_protocol import EncodeCompanyTime
from trader.download.infra.company_link_codec import DecodeCompanyLink
from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.infra.sqlite_connection import SqliteConnection


@dataclass(frozen=True, slots=True)
class CompanyAnnouncementPreparation:
    facts: CompanyFacts
    documents: tuple[CompanyDocument, ...]
    receipts: tuple[CompanyInputReceipt, ...]
    links: tuple[CompanyPublishedEvidenceLink, ...]
    pending_retained: int


def PrepareCompanyAnnouncements(
    collected: SqliteConnection, formal: SqliteConnection, previous: CompanyFacts, *, cutoff: datetime,
    extraction_hash: bytes, rule_identity: bytes, event_policy: CompanyEventPolicy,
    read_document: Callable[[bytes], str], check: Callable[[], None],
) -> CompanyAnnouncementPreparation:
    """A source receipt validates its exact body, not risk population completeness."""
    check()
    start = cutoff.date() - timedelta(days=365)
    documents: list[CompanyDocument] = []
    reviews: list[CompanyReviewReason] = []
    identities: list[tuple[str, str, str, bytes]] = []
    pending_retained = 0
    rows = collected.run("""SELECT d.*,c.code,x.payload_hash FROM disclosures d
        JOIN disclosure_codes c ON c.disclosure_id=d.id
        JOIN company_document_requirements required ON required.disclosure_id=d.id
        JOIN document_extractions x ON x.disclosure_id=d.id AND x.record_hash=d.record_hash
        WHERE c.code=? AND d.source='cninfo' AND d.kind='announcement'
        AND d.body_checked=1 AND d.body_retained=1 AND d.published_at>=? AND d.published_at<=?
        AND (SELECT COUNT(*) FROM disclosure_codes members WHERE members.disclosure_id=d.id)=1
        ORDER BY d.published_at,d.source_key""", (previous.code, start.isoformat(), cutoff.isoformat()))
    for row in rows:
        check()
        body_hash, payload_hash = row["body_hash"], row["payload_hash"]
        if not isinstance(body_hash, bytes) or len(body_hash) != 32 or not isinstance(payload_hash, bytes) or len(payload_hash) != 32:
            raise ValueError("company_announcement_material_identity_invalid")
        item = DecodeCompanyDisclosure(row)
        receipt = formal.run("""SELECT outcome,body_hash,policy_id FROM company_input_receipt
            WHERE code=? AND source=? AND source_key=? AND record_hash=? AND extraction_hash=?
            AND published_at<=?""", (previous.code, item.source, item.identity, item.digest, extraction_hash, EncodeCompanyTime(cutoff))).fetchone()
        if receipt is not None and receipt[1] == body_hash and (
            receipt[0] == "valid" or receipt[0] == "directory_observed" and receipt[2] == rule_identity.hex()
        ):
            continue
        if formal.run("""SELECT 1 FROM company_input_receipt WHERE code=? AND source=? AND source_key=?
            AND outcome='valid' AND (record_hash<>? OR body_hash<>?) LIMIT 1""",
            (previous.code, item.source, item.identity, item.digest, body_hash)).fetchone() is not None:
            scopes = tuple(scope for scope, events in (("risk", previous.risk_events), ("positive", previous.positive_events))
                if any(item.identity in event.evidence_ids for event in events))
            if not scopes:
                scopes = ("risk" if CompanyDocumentRiskRelevant(item.title) else "positive",)
            reviews.extend(CompanyReviewReason(scope, "revision_pending", item.identity, cutoff,
                "同来源公告内容变化，正式修订关系尚未核实") for scope in scopes)
            identities.append((item.source, item.identity, item.digest, body_hash))
            continue
        if len(documents) >= 8:
            pending_retained += 1
            continue
        text = read_document(body_hash)
        check()
        if hashlib.sha256(text.encode()).digest() != body_hash:
            raise ValueError("company_announcement_body_hash_mismatch")
        documents.append(CompanyDocument(item, text, True))
        identities.append((item.source, item.identity, item.digest, body_hash))
    pending = collected.run("""SELECT COUNT(*) FROM disclosures d
        JOIN disclosure_codes c ON c.disclosure_id=d.id
        JOIN company_document_requirements required ON required.disclosure_id=d.id
        WHERE c.code=? AND d.kind='announcement' AND d.published_at>=? AND d.published_at<=?
        AND (d.body_checked=0 OR d.body_retained=0)""", (previous.code, start.isoformat(), cutoff.isoformat())).require_one()[0]
    ranges = collected.run("SELECT start_date,end_date FROM coverage WHERE market=? ORDER BY start_date,end_date",
        (EvidenceMarket(previous.code),)).fetchall()
    candidates = []
    verified_candidate_ids = []
    for row in collected.run("""SELECT link.payload,d.source,d.source_key,d.record_hash,d.body_hash,d.published_at FROM document_link_candidates link
        JOIN disclosures d ON d.source=link.source AND d.source_key=link.source_key AND d.record_hash=link.record_hash
        JOIN disclosure_codes c ON c.disclosure_id=d.id
        WHERE c.code=? AND link.extraction_hash=? AND d.body_checked=1 AND d.body_retained=1 AND d.published_at<=?
        ORDER BY d.published_at,d.source_key""", (previous.code, extraction_hash, cutoff.isoformat())):
        check()
        candidate = DecodeCompanyLink(row[0])
        if candidate.evidence_id != row[2] or candidate.published_at.isoformat() != row[5]:
            raise ValueError("company_announcement_link_identity_invalid")
        candidates.append(candidate)
        if formal.run("""SELECT 1 FROM company_input_receipt WHERE code=? AND source=? AND source_key=?
            AND record_hash=? AND body_hash=? AND extraction_hash=? AND outcome='valid' AND published_at<=?""",
            (previous.code, row[1], row[2], row[3], row[4], extraction_hash, EncodeCompanyTime(cutoff))).fetchone() is not None:
            verified_candidate_ids.append(candidate.evidence_id)
    linked_candidates = LinkCompanyAnnouncementNumbers(collected, previous.code, tuple(candidates))
    normalized = NormalizeCompanyAnnouncementFacts(previous, tuple(documents), cutoff, event_policy,
        candidates=linked_candidates, pending_count=pending + pending_retained, directory_complete=CompanyCoverageComplete(ranges, start, cutoff.date()),
        input_identity=CanonicalDigest((tuple((source, key, record, body.hex()) for source, key, record, body in identities), cutoff)),
        revision_reviews=tuple(reviews), verified_candidate_ids=tuple(verified_candidate_ids))
    receipts = tuple(CompanyInputReceipt(source, key, previous.code, record, extraction_hash, body, cutoff,
        "valid" if key in normalized.verified_evidence_ids else "directory_observed", rule_identity.hex())
        for source, key, record, body in identities)
    referenced = {key for event in normalized.facts.risk_events + normalized.facts.positive_events for key in event.evidence_ids}
    referenced.update(review.evidence_id for review in normalized.facts.review_reasons if review.evidence_id is not None)
    links = tuple(CompanyPublishedEvidenceLink(previous.code, document.disclosure.source, document.disclosure.digest,
        CompanyEvidenceLink(document.disclosure.identity, document.disclosure.title, document.disclosure.url,
            document.disclosure.published_at, document.disclosure.identity in normalized.verified_evidence_ids))
        for document in documents if document.disclosure.identity in referenced)
    verified_documents = tuple(document for document in documents if document.disclosure.identity in normalized.verified_evidence_ids)
    return CompanyAnnouncementPreparation(normalized.facts, verified_documents, receipts, links, pending_retained)


__all__ = ["CompanyAnnouncementPreparation", "PrepareCompanyAnnouncements"]
