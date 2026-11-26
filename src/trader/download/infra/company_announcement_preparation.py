"""Read exact retained official documents before the sole normalized publication."""

import hashlib
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from trader.download.domain.company_announcement_normalization import NormalizeCompanyAnnouncementFacts
from trader.download.domain.company_descriptions import CompanyReviewDescription
from trader.download.domain.company_document_links import CompanyPublishedEvidenceLink
from trader.download.domain.company_event_extraction import CompanyDocumentNeedsReview, CompanyDocumentRiskRelevant
from trader.download.domain.company_events import CompanyDataState, CompanyEventPolicy, CompanyEvidenceLink, CompanyReviewReason
from trader.download.domain.company_evidence import CompanyDocument, CompanyRatio, EvidenceMarket
from trader.download.domain.company_facts import CompanyFacts
from trader.download.domain.company_unlock_notice import ConfirmCompanyUnlockListing, IsCompanyUnlockListingNotice, IsCompanyUnlockListingRevision
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


@dataclass(frozen=True, slots=True)
class CompanyAnnouncementCoverage:
    pending: int
    directory_complete: bool
    has_retained_material: bool


def ReadCompanyAnnouncementCoverage(
    collected: SqliteConnection, codes: tuple[str, ...], cutoff: datetime, check: Callable[[], None],
) -> dict[str, CompanyAnnouncementCoverage]:
    """Count required missing bodies per issuer and reuse the market's exact range union in this transaction."""
    start = cutoff.date() - timedelta(days=365)
    markets = {code: EvidenceMarket(code) for code in codes}
    covered = {}
    for market in sorted(set(markets.values())):
        check()
        ranges = collected.run("SELECT start_date,end_date FROM coverage WHERE market=? ORDER BY start_date,end_date",
            (market,)).fetchall()
        covered[market] = CompanyCoverageComplete(ranges, start, cutoff.date())
    pending: Counter[str] = Counter()
    retained: set[str] = set()
    for offset in range(0, len(codes), 128):
        check()
        batch = codes[offset:offset + 128]
        slots = ",".join("?" for _ in batch)
        # Link candidates may predate the current annual window and do not require a document task.
        retained.update(row[0] for row in collected.run(f"""SELECT DISTINCT c.code FROM disclosure_codes c
            CROSS JOIN disclosures d ON d.id=c.disclosure_id
            WHERE c.code IN ({slots}) AND d.body_checked=1 AND d.body_retained=1 AND d.published_at<=?""",
            (*batch, cutoff.isoformat())))
        for code, title in collected.run(f"""SELECT c.code,d.title FROM disclosure_codes c
            CROSS JOIN disclosures d ON d.id=c.disclosure_id
            JOIN company_document_requirements required ON required.disclosure_id=d.id
            WHERE c.code IN ({slots}) AND d.kind='announcement' AND d.published_at>=? AND d.published_at<=?
            AND (d.body_checked=0 OR d.body_retained=0)""", (*batch, start.isoformat(), cutoff.isoformat())):
            if CompanyDocumentNeedsReview(title) or not (IsCompanyUnlockListingNotice(title) or IsCompanyUnlockListingRevision(title)):
                pending[code] += 1
    return {code: CompanyAnnouncementCoverage(pending[code], covered[market], code in retained) for code, market in markets.items()}


def PrepareCompanyAnnouncements(
    collected: SqliteConnection, formal: SqliteConnection, previous: CompanyFacts, *, cutoff: datetime,
    extraction_hash: bytes, rule_identity: bytes, event_policy: CompanyEventPolicy,
    read_document: Callable[[bytes], str], check: Callable[[], None],
    unlock_observations: tuple[CompanyRatio, ...] = (),
    coverage: CompanyAnnouncementCoverage | None = None,
) -> CompanyAnnouncementPreparation:
    """A source receipt validates its exact body, not risk population completeness."""
    check()
    if coverage is None:
        coverage = ReadCompanyAnnouncementCoverage(collected, (previous.code,), cutoff, check)[previous.code]
    if not coverage.has_retained_material:
        normalized = NormalizeCompanyAnnouncementFacts(previous, (), cutoff, event_policy, candidates=(),
            pending_count=coverage.pending, directory_complete=coverage.directory_complete,
            input_identity=CanonicalDigest(((), cutoff)))
        return CompanyAnnouncementPreparation(normalized.facts, (), (), (), 0)
    start = cutoff.date() - timedelta(days=365)
    documents: list[CompanyDocument] = []
    unlock_documents: list[CompanyDocument] = []
    reviews: list[CompanyReviewReason] = []
    identities: list[tuple[str, str, str, bytes, str]] = []
    pending_retained = 0
    unlock_pending = 0
    rule_id = rule_identity.hex()
    unlock_end = cutoff.date() + timedelta(days=event_policy.unlock_days)
    unlock_observations = tuple(item for item in unlock_observations if item.code == previous.code
        and item.ratio_pct > 0 and cutoff.date() <= item.day <= unlock_end
    ) if previous.unlock_state is not CompanyDataState.COMPLETE else ()
    revision = _latest_unlock_revision(collected, previous.code, start.isoformat(), cutoff.isoformat()) if unlock_observations else None
    unlock_policy = CanonicalDigest((rule_id, unlock_observations, revision)) if unlock_observations else None
    rows = collected.run("""SELECT d.*,c.code,x.payload_hash FROM disclosure_codes c
        CROSS JOIN disclosures d ON d.id=c.disclosure_id
        JOIN company_document_requirements required ON required.disclosure_id=d.id
        JOIN document_extractions x ON x.disclosure_id=d.id AND x.record_hash=d.record_hash
        WHERE c.code=? AND d.source='cninfo' AND d.kind='announcement'
        AND d.body_checked=1 AND d.body_retained=1 AND d.published_at>=? AND d.published_at<=?
        AND (SELECT COUNT(*) FROM disclosure_codes members WHERE members.disclosure_id=d.id)=1
        ORDER BY d.published_at,d.source_key""", (previous.code, start.isoformat(), cutoff.isoformat()))
    for row in rows:
        check()
        title = row["title"]
        supplemental = not CompanyDocumentNeedsReview(title)
        is_unlock = supplemental and IsCompanyUnlockListingNotice(title)
        if is_unlock and (unlock_policy is None or revision is not None and row["published_at"] <= revision):
            continue
        if supplemental and IsCompanyUnlockListingRevision(title):
            continue
        proof_policy = unlock_policy if is_unlock and unlock_policy is not None else rule_id
        selected = unlock_documents if is_unlock else documents
        body_hash, payload_hash = row["body_hash"], row["payload_hash"]
        if not isinstance(body_hash, bytes) or len(body_hash) != 32 or not isinstance(payload_hash, bytes) or len(payload_hash) != 32:
            raise ValueError("company_announcement_material_identity_invalid")
        item = DecodeCompanyDisclosure(row)
        receipt = formal.run("""SELECT outcome,body_hash,policy_id FROM company_input_receipt
            WHERE code=? AND source=? AND source_key=? AND record_hash=? AND extraction_hash=?
            AND published_at<=?""", (previous.code, item.source, item.identity, item.digest, extraction_hash, EncodeCompanyTime(cutoff))).fetchone()
        if receipt is not None and receipt[1] == body_hash and (
            receipt[0] == "valid" or receipt[0] == "directory_observed" and receipt[2] == proof_policy
        ):
            continue
        if formal.run("""SELECT 1 FROM company_input_receipt WHERE code=? AND source=? AND source_key=?
            AND outcome='valid' AND (record_hash<>? OR body_hash<>?) LIMIT 1""",
            (previous.code, item.source, item.identity, item.digest, body_hash)).fetchone() is not None:
            scopes = tuple(scope for scope, events in (("risk", previous.risk_events), ("positive", previous.positive_events))
                if any(item.identity in event.evidence_ids for event in events))
            if not scopes:
                scopes = ("unlock" if is_unlock else "risk" if CompanyDocumentRiskRelevant(item.title) else "positive",)
            reviews.extend(CompanyReviewReason(scope, "revision_pending", item.identity, cutoff,
                "同来源公告内容变化，正式修订关系尚未核实") for scope in scopes)
            identities.append((item.source, item.identity, item.digest, body_hash, proof_policy))
            continue
        if len(selected) >= (2 if is_unlock else 8):
            if is_unlock:
                unlock_pending += 1
            else:
                pending_retained += 1
            continue
        text = read_document(body_hash)
        check()
        if hashlib.sha256(text.encode()).digest() != body_hash:
            raise ValueError("company_announcement_body_hash_mismatch")
        selected.append(CompanyDocument(item, text, True))
        identities.append((item.source, item.identity, item.digest, body_hash, proof_policy))
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
        candidates=linked_candidates, pending_count=coverage.pending + pending_retained, directory_complete=coverage.directory_complete,
        input_identity=CanonicalDigest((tuple((source, key, record, body.hex()) for source, key, record, body, policy in identities if policy == rule_id), cutoff)),
        revision_reviews=tuple(reviews), verified_candidate_ids=tuple(verified_candidate_ids))
    facts, unlock_ids = _prepare_unlock_events(normalized.facts, unlock_documents, unlock_observations, cutoff)
    verified_ids = frozenset((*normalized.verified_evidence_ids, *unlock_ids))
    receipts = tuple(CompanyInputReceipt(source, key, previous.code, record, extraction_hash, body, cutoff,
        "valid" if key in verified_ids else "directory_observed", policy)
        for source, key, record, body, policy in identities)
    referenced = {key for event in facts.risk_events + facts.positive_events for key in event.evidence_ids}
    referenced.update(review.evidence_id for review in facts.review_reasons if review.evidence_id is not None)
    links = tuple(CompanyPublishedEvidenceLink(previous.code, document.disclosure.source, document.disclosure.digest,
        CompanyEvidenceLink(document.disclosure.identity, document.disclosure.title, document.disclosure.url,
            document.disclosure.published_at, document.disclosure.identity in verified_ids))
        for document in (*documents, *unlock_documents) if document.disclosure.identity in referenced)
    verified_documents = tuple(document for document in documents if document.disclosure.identity in verified_ids)
    return CompanyAnnouncementPreparation(facts, verified_documents, receipts, links, pending_retained + unlock_pending)


def _latest_unlock_revision(collected: SqliteConnection, code: str, start: str, cutoff: str) -> str | None:
    rows = collected.run("""SELECT d.title,d.published_at FROM disclosure_codes c
        CROSS JOIN disclosures d ON d.id=c.disclosure_id
        JOIN company_document_requirements required ON required.disclosure_id=d.id
        WHERE c.code=? AND d.source='cninfo' AND d.kind='announcement' AND d.published_at>=? AND d.published_at<=?
        ORDER BY d.published_at DESC""", (code, start, cutoff))
    return next((row[1] for row in rows if IsCompanyUnlockListingRevision(row[0])), None)


def _prepare_unlock_events(
    facts: CompanyFacts, documents: list[CompanyDocument], observations: tuple[CompanyRatio, ...], cutoff: datetime,
) -> tuple[CompanyFacts, tuple[str, ...]]:
    confirmed = tuple((document.disclosure.identity, event) for document in documents
        if (event := ConfirmCompanyUnlockListing(document, observations, cutoff)) is not None)
    existing_days = {item.day for item in facts.unlock_schedule}
    accepted = tuple((evidence, event) for evidence, event in confirmed
        if event.day not in existing_days and sum(other.day == event.day for _, other in confirmed) == 1)
    if not accepted:
        return facts, ()
    reviews = tuple(CompanyReviewReason("unlock", "window_incomplete", evidence, cutoff, CompanyReviewDescription("window_incomplete"))
        for evidence, _ in accepted)
    return replace(facts, unlock_schedule=tuple(sorted((*facts.unlock_schedule, *(event for _, event in accepted)), key=lambda item: item.day)),
        unlock_state=CompanyDataState.PARTIAL,
        review_reasons=tuple(sorted((*facts.review_reasons, *reviews), key=lambda item: (item.scope, item.evidence_id or "", item.reason_code))),
        published_at=cutoff), tuple(evidence for evidence, _ in accepted)


__all__ = [
    "CompanyAnnouncementCoverage", "CompanyAnnouncementPreparation", "PrepareCompanyAnnouncements", "ReadCompanyAnnouncementCoverage",
]
