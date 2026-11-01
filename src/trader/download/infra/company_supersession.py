"""Accept only explicit whole-document replacements backed by formal receipts."""

from collections.abc import Sequence

from trader.download.domain.company_facts import CompanyFacts
from trader.download.infra.company_link_codec import DecodeCompanyLink
from trader.infra.sqlite_connection import SqliteConnection


def PublishCompanySupersessions(connection: SqliteConnection, facts: Sequence[CompanyFacts], extraction_hash: bytes) -> tuple[str, ...]:
    changed: list[str] = []
    for fact in facts:
        references = {key for event in fact.risk_events for key in event.evidence_ids}
        references.update(key for event in fact.positive_events for key in event.evidence_ids)
        pending = {review.evidence_id for review in fact.review_reasons}
        blocking = {review.evidence_id for review in fact.review_reasons if review.reason_code != "document_pending"}
        candidates = connection.run(
            "SELECT d.id,d.source,d.record_hash,l.payload FROM document_link_candidates l JOIN disclosures d "
            "ON d.source=l.source AND d.source_key=l.source_key AND d.record_hash=l.record_hash "
            "JOIN document_publications p ON p.disclosure_id=d.id AND p.code=? AND p.record_hash=d.record_hash "
            "AND p.extraction_hash=l.extraction_hash WHERE l.extraction_hash=? AND l.payload LIKE ?",
            (fact.code, extraction_hash, '%"supersedes":["%'),
        ).fetchall()
        for row in candidates:
            candidate = DecodeCompanyLink(row[3])
            if candidate.evidence_id in pending:
                continue
            for key in candidate.supersedes:
                # A verified earlier independent event cannot be erased by the
                # replacement of a document that still supplies its evidence.
                if key in references or key in blocking:
                    continue
                old = connection.run(
                    "SELECT d.id,d.record_hash FROM disclosures d JOIN disclosure_codes c ON c.disclosure_id=d.id "
                    "WHERE d.source=? AND d.source_key=? AND c.code=? AND d.published_at<?",
                    (row[1], key, fact.code, candidate.published_at.isoformat()),
                ).fetchone()
                if old is not None:
                    before = connection.total_changes
                    connection.run("INSERT INTO document_supersessions VALUES(?,?,?,?,?,?,?) "
                                   "ON CONFLICT(disclosure_id,code,record_hash) DO UPDATE SET "
                                   "replacement_id=excluded.replacement_id,replacement_hash=excluded.replacement_hash,"
                                   "extraction_hash=excluded.extraction_hash "
                                   "WHERE replacement_id<>excluded.replacement_id OR replacement_hash<>excluded.replacement_hash "
                                   "OR extraction_hash<>excluded.extraction_hash",
                                   (old[0], fact.code, old[1], row[0], row[2], extraction_hash, "official_complete_replacement"))
                    if connection.total_changes != before:
                        changed.append(fact.code)
    return tuple(sorted(set(changed)))


__all__ = ["PublishCompanySupersessions"]
