"""Prepare immutable company facts and evidence using fixed local read views."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime

from trader.download.domain.company_event_extraction import CompanyDocumentNeedsReview, ResolveCompanyLinks
from trader.download.domain.company_events import CompanyEventPolicy
from trader.download.domain.company_evidence import CompanyListing, CompanyPublicationProgress, CompanyStockProgress, IsTransientCompanyDocumentFailure
from trader.download.domain.company_facts import CompanyFacts, CompileCompanyFacts, FinalizeCompanyFacts
from trader.download.domain.company_field_results import CompanyFieldConclusion
from trader.download.infra.company_announcement_links import LinkCompanyAnnouncementNumbers
from trader.download.infra.company_contract_link_rules import CompanyContractLinkRefresh
from trader.download.infra.company_fact_inputs import BindCompanyEvidenceSource, BuildCompanySnapshot, ReadCompanySnapshotRows, ReadCompanySnapshotShared
from trader.download.infra.company_fact_rows import ReadCompanyFactRows
from trader.download.infra.company_facts_index import CompanyInputReceipt, ReadCompanyPublicationIdentity
from trader.download.infra.company_facts_protocol import EVIDENCE_SOURCES, DecodeCompanyLabel
from trader.download.infra.company_field_publication import PrepareCompanyFieldResults
from trader.download.infra.company_field_recovery import CompanyFieldRepairReceipts, PlanCompanyFieldAttempts, PrepareCompanyShareRecheckReceipts
from trader.download.infra.company_financial_publication import PrepareCompanyFinancialReports
from trader.download.infra.company_publication_candidate import CompanyDocumentPublication, CompanyPublicationCandidate
from trader.download.infra.company_publication_progress import ReportCompanyVerification
from trader.download.infra.company_publication_reads import ReadCompanyPublicationLinks
from trader.infra.sqlite_connection import SqliteConnection


def PrepareCompanyPublicationCandidate(
    source: SqliteConnection,
    formal: SqliteConnection,
    codes: Sequence[str],
    observed_at: datetime,
    *,
    extraction_hash: bytes,
    event_policy: CompanyEventPolicy,
    listings: tuple[CompanyListing, ...],
    batch_publication_reads: bool,
    read_document: Callable[[bytes], str],
    check_cancelled: Callable[[], None],
    cancel_requested: Callable[[], bool] | None,
    stock_progress: Callable[[CompanyStockProgress | CompanyPublicationProgress], None] | None,
    changed_only: bool,
) -> CompanyPublicationCandidate | None:
    requested = tuple(sorted(set(codes)))
    prepared: list[CompanyFacts] = []
    previous_facts: dict[str, CompanyFacts] = {}
    receipts: list[CompanyDocumentPublication] = []
    formal_receipts: list[CompanyInputReceipt] = []
    field_results: list[CompanyFieldConclusion] = []
    contract_refreshes: list[CompanyContractLinkRefresh] = []
    listing_dates = {item.code: item.listed_on for item in listings}
    parent = ReadCompanyPublicationIdentity(formal)
    rebuild = parent is not None and parent.extraction_hash != extraction_hash
    existing = {row[0] for row in formal.run("SELECT code FROM company_facts")}
    if rebuild:
        requested = tuple(sorted(set(requested) | existing))
    elif changed_only and parent is not None and parent.published_at.date() == observed_at.date():
        dirty = {row[0] for row in source.run("SELECT code FROM company_dirty_codes")}
        requested = tuple(code for code in requested if code in dirty or code not in existing)
    if not requested:
        return None
    sources: dict[str, str] = {}
    for row in formal.run("SELECT source,source_key FROM company_evidence_ref"):
        BindCompanyEvidenceSource(sources, row[1], DecodeCompanyLabel(EVIDENCE_SOURCES, row[0]))
    shared = ReadCompanySnapshotShared(source, observed_at)
    for offset in range(0, len(requested), 128):
        batch = requested[offset : offset + 128]
        inputs = ReadCompanySnapshotRows(source, formal, batch, observed_at, b"" if rebuild else extraction_hash,
                                        cancel_requested=cancel_requested)
        previous = {item.code: item for item in ReadCompanyFactRows(formal, batch)}
        previous_facts.update(previous)
        link_rows = ReadCompanyPublicationLinks(
            source, batch, extraction_hash, observed_at=observed_at,
            event_policy=event_policy, read_document=read_document,
        ) if batch_publication_reads else ()
        for code in batch:
            check_cancelled()
            rows = inputs[code].disclosures
            for row in rows:
                if row["kind"] == "announcement":
                    BindCompanyEvidenceSource(sources, row["source_key"], row["source"])
            snapshot = BuildCompanySnapshot(
                code,
                observed_at,
                inputs[code],
                shared,
                listed_on=listing_dates.get(code),
                read_document=read_document,
            )
            financial = PrepareCompanyFinancialReports(
                source, formal, snapshot.financials, previous[code].latest_financial if code in previous else None,
                snapshot.announcements, observed_at=observed_at, extraction_hash=extraction_hash,
                read_document=read_document,
            )
            snapshot = replace(snapshot, financials=financial.financials, failures=(*snapshot.failures, *financial.failures))
            formal_receipts.extend(financial.receipts)
            facts = CompileCompanyFacts(
                snapshot, event_policy, previous=None if rebuild else previous.get(code)
            )
            link_input = (
                next(item for item in link_rows if item.code == code)
                if batch_publication_reads
                else ReadCompanyPublicationLinks(
                    source, (code,), extraction_hash, observed_at=observed_at,
                    event_policy=event_policy, read_document=read_document,
                )[0]
            )
            contract_refreshes.extend(link_input.refreshes)
            review_keys = {(r.scope, r.reason_code, r.evidence_id) for r in facts.review_reasons}
            recompiled = {
                (r["source"], r["source_key"], r["record_hash"], r["body_hash"]) for r in rows
                if r["kind"] == "announcement" and r["body_checked"] and r["body_retained"]
                and not r["published_for_code"] and not r["superseded_for_code"] and CompanyDocumentNeedsReview(r["title"])
            }
            reviewed_inputs = {r.evidence_id for r in facts.review_reasons if r.scope == "positive"
                               and r.reason_code in {"progress_unlinked", "resolution_unlinked", "cancellation_unlinked"}}
            reviewed_inputs.difference_update(item[1] for item in recompiled)
            reviewed_inputs.update(
                r.binding.source_key for r in link_input.refreshes
                if (r.binding.source, r.binding.source_key, r.binding.record_hash, r.binding.body_hash) not in recompiled
            )
            # Current compiled input owns its business conclusions;
            # stale cache differences cannot add another blocker.
            reviews = (*facts.review_reasons, *(r for r in link_input.reviews if r.evidence_id in reviewed_inputs
                       and (r.scope, r.reason_code, r.evidence_id) not in review_keys))
            candidates = LinkCompanyAnnouncementNumbers(source, code, link_input.candidates)
            linked = ResolveCompanyLinks(candidates, facts.risk_events, facts.positive_events, reviews, event_policy)
            prior = previous.get(code)
            facts = FinalizeCompanyFacts(facts, snapshot, linked, prior)
            prepared.append(facts)
            formal_receipts.extend(PrepareCompanyShareRecheckReceipts(
                source, formal, facts, prior, observed_at=observed_at,
                extraction_hash=extraction_hash, event_policy=event_policy,
                read_document=read_document,
            ))
            fields = PrepareCompanyFieldResults(
                source, formal, facts, snapshot.announcements, observed_at=observed_at,
                extraction_hash=extraction_hash, event_policy=event_policy, read_document=read_document,
            )
            field_results.extend(fields.results)
            formal_receipts.extend(fields.receipts)
            receipts.extend(
                CompanyDocumentPublication(row["id"], code, row["record_hash"], extraction_hash, observed_at)
                for row in rows
                if row["kind"] == "announcement"
                and row["body_checked"]
                and row["body_retained"]
                and not row["published_for_code"]
            )
            formal_receipts.extend(
                CompanyInputReceipt(row["source"], row["source_key"], code, row["record_hash"],
                                    extraction_hash, row["body_hash"], observed_at)
                for row in rows if row["kind"] == "announcement" and row["body_checked"]
                and row["body_retained"] and not row["published_for_code"]
            )
            formal_receipts.extend(
                CompanyInputReceipt(
                    "eastmoney_annual_income",
                    income.source_key,
                    code,
                    income.record_hash,
                    extraction_hash,
                    None,
                    observed_at,
                )
                for income in inputs[code].annual_incomes
            )
            formal_receipts.extend(
                CompanyInputReceipt(row["source"], row["source_key"], code, row["record_hash"], extraction_hash,
                                    row["payload_hash"], observed_at, row["document_failure"], row["retry_policy_id"])
                for row in rows if row["document_failure"] and not IsTransientCompanyDocumentFailure(row["document_failure"])
            )
            ReportCompanyVerification(stock_progress, facts, len(prepared), len(requested),
                                      len(rows), sum(bool(row["body_checked"]) for row in rows), facts is prior)

    attempts = PlanCompanyFieldAttempts(source, prepared, formal_receipts, extraction_hash, observed_at)
    return CompanyPublicationCandidate(
        requested,
        observed_at,
        parent,
        tuple(prepared),
        tuple(item for item in prepared if item is not previous_facts.get(item.code)),
        tuple(sorted(sources.items())),
        (*formal_receipts, *CompanyFieldRepairReceipts(source, requested, observed_at), *(item.receipt for item in attempts)),
        tuple(receipts),
        tuple(field_results),
        tuple(contract_refreshes),
        attempts,
    )


__all__ = ["PrepareCompanyPublicationCandidate"]
