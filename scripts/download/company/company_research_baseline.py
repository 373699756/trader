"""Explicit current-source codec to typed research baseline; no database admission or migration."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime
from trader.download.domain.company_document_links import CompanyPublishedEvidenceLink

from trader.download.domain.company_events import CompanyEventPolicy
from trader.download.domain.company_field_results import CompanyFieldConclusion
from trader.download.domain.company_facts import CompanyFacts
from trader.download.infra.company_document_files import CompanyDocumentFiles
from trader.download.infra.company_fact_rows import ReadCompanyFactRows
from trader.download.infra.company_field_result_rows import ReadCompanyFieldResults
from trader.download.infra.company_field_publication import CompanyFieldProofRulesHash
from trader.download.infra.company_facts_index import CompanyInputReceipt, CompanyPublicationIdentity
from trader.download.infra.company_facts_protocol import DecodeCompanyLabel, EVIDENCE_SOURCES, RequireCompanyTime
from trader.download.infra.company_normalized_index import CompanyNormalizationBaseline, ReadCompanyNormalizationReceipts
from trader.download.infra.company_risk_adoption import ProjectCompanyRiskAdoptions, ReadCompanyRiskAdoptionWitnesses
from trader.download.infra.company_document_link_rows import ReadCompanyDocumentLinks
from trader.download.infra.company_sync_preparation import PrepareCompanyIssuerInputs
from trader.infra.sqlite_connection import SqliteConnection, SqliteConnectionOptions, SqliteSession, SqliteTransaction
from trader.infra.sqlite_policy import SqliteIoPolicy
from trader.infra.sqlite_publication import VerifySqliteFile


def ReadCompanyResearchIdentity(connection: SqliteConnection) -> CompanyPublicationIdentity:
    row = connection.run("SELECT content_hash,extraction_hash,published_at FROM company_publication WHERE singleton=1").require_one()
    if not isinstance(row[0], bytes) or not isinstance(row[1], bytes) or len(row[0]) != 32 or len(row[1]) != 32:
        raise ValueError("company_normalized_replay_source_identity_invalid")
    return CompanyPublicationIdentity(row[0], row[1], RequireCompanyTime(row[2]))


@dataclass(frozen=True, slots=True)
class CompanyResearchBaseline:
    baseline: CompanyNormalizationBaseline
    original_fields: tuple[CompanyFieldConclusion, ...]
    material_reads: int
    consumer_facts: tuple[CompanyFacts, ...]


def ReadCompanyResearchBaseline(
    collection_path: Path,
    facts_path: Path,
    documents: CompanyDocumentFiles,
    *,
    event_policy: CompanyEventPolicy,
    sqlite_policy: SqliteIoPolicy,
    check: Callable[[], None],
    cancel_requested: Callable[[], bool],
    document_overlays: tuple[CompanyDocumentFiles, ...] = (),
    cutoff: datetime | None = None,
) -> CompanyResearchBaseline:
    VerifySqliteFile(facts_path, sqlite_policy=sqlite_policy, check=check, file_only=True)
    material_reads = 0

    def read_document(digest: bytes) -> str:
        nonlocal material_reads
        material_reads += 1
        for owner in (documents, *document_overlays):
            try:
                return owner.read(digest, check=check)
            except FileNotFoundError:
                continue
        raise FileNotFoundError("company_research_original_text_missing")

    options = SqliteConnectionOptions(read_only=True)
    with (
        SqliteSession(collection_path, options, sqlite_policy=sqlite_policy, cancel_requested=cancel_requested) as raw,
        SqliteSession(facts_path, options, sqlite_policy=sqlite_policy, cancel_requested=cancel_requested) as formal,
        SqliteTransaction(raw), SqliteTransaction(formal),
    ):
        original_identity = ReadCompanyResearchIdentity(formal)
        codes = tuple(row[0] for row in formal.run("SELECT code FROM company_facts ORDER BY code"))
        if not 1 <= len(codes) <= 12000:
            raise ValueError("company_normalized_replay_population_invalid")
        original_facts = ReadCompanyFactRows(formal, codes)
        original_fields = ReadCompanyFieldResults(formal, codes)
        original_links: dict[str, list[CompanyPublishedEvidenceLink]] = {}
        for link in ReadCompanyDocumentLinks(formal, codes):
            original_links.setdefault(link.code, []).append(link)
        revalidated: list[CompanyFieldConclusion] = []
        prepared_facts = []
        prepared_receipts: list[CompanyInputReceipt] = []
        prepared_links: list[CompanyPublishedEvidenceLink] = []
        for fact in original_facts:
            check()
            prepared = PrepareCompanyIssuerInputs(raw, formal, fact, cutoff=cutoff or original_identity.published_at,
                extraction_hash=original_identity.extraction_hash, event_policy=event_policy,
                read_document=read_document, check=check, revalidate=True, include_new_announcements=False,
                published_links=tuple(original_links.get(fact.code, ())))
            prepared_facts.append(prepared.facts)
            revalidated.extend(prepared.fields)
            prepared_receipts.extend(prepared.receipts)
            links = {(link.source, link.link.evidence_id): link for link in original_links.get(fact.code, ())}
            links.update(((link.source, link.link.evidence_id), link) for link in prepared.links)
            relevant = {key for event in prepared.facts.risk_events for key in event.evidence_ids}
            relevant.update(key for event in prepared.facts.positive_events for key in event.evidence_ids)
            relevant.update(milestone.evidence_id for event in prepared.facts.positive_events for milestone in event.level_milestones)
            relevant.update(proof.source_key for field in prepared.fields for proof in field.evidence)
            relevant.update(reason.evidence_id for reason in prepared.facts.review_reasons if reason.evidence_id is not None)
            prepared_links.extend(link for (_, key), link in links.items() if key in relevant)
        fields = tuple(sorted(revalidated, key=lambda item: (item.code, item.event_id, item.field)))
        receipts = ReadCompanyNormalizationReceipts(formal)
        risk_adoptions = ReadCompanyRiskAdoptionWitnesses(formal)
        consumer_facts = ProjectCompanyRiskAdoptions(formal, prepared_facts, original_identity.extraction_hash)
        sources = tuple((row[0], DecodeCompanyLabel(EVIDENCE_SOURCES, row[1])) for row in formal.run("SELECT source_key,source FROM company_evidence_ref ORDER BY source_key"))
        if ReadCompanyResearchIdentity(formal) != original_identity:
            raise ValueError("company_normalized_replay_source_changed")
    seen = {(item.source, item.source_key, item.code, item.record_hash, item.extraction_hash) for item in receipts}
    new_receipts = []
    for item in prepared_receipts:
        key = item.source, item.source_key, item.code, item.record_hash, item.extraction_hash
        if key not in seen:
            seen.add(key)
            new_receipts.append(item)
    baseline = CompanyNormalizationBaseline(tuple(prepared_facts), fields, (*receipts, *new_receipts), sources,
        original_identity, CompanyFieldProofRulesHash(), tuple(sorted(prepared_links, key=lambda item: (item.code, item.source, item.link.evidence_id))),
        risk_adoptions=risk_adoptions)
    return CompanyResearchBaseline(baseline, original_fields, material_reads, consumer_facts)




__all__ = ["CompanyResearchBaseline", "ReadCompanyResearchBaseline", "ReadCompanyResearchIdentity"]
