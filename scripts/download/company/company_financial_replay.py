"""Replay real financial inputs and reviewed official PDFs in a new isolated publication."""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import cast

from scripts.download.company.company_research_baseline import ReadCompanyResearchBaseline, ReadCompanyResearchIdentity
from scripts.core.paths import PROJECT_ROOT
from scripts.download.company.output_paths import RequireIsolatedReplay
from trader.download.domain.company_events import CompanyEventPolicy
from trader.download.domain.company_facts import CompanyFacts
from trader.download.domain.company_evidence import CompanyFinancial, StructuredCompanyQuery
from trader.download.domain.company_financial_report import (
    COMPANY_FINANCIAL_REPORT_DIGEST, ExtractCompanyFinancialReport,
    CompanyFinancialReportField, CompanyFinancialReportRequest, CompanyFinancialReportState,
)
from trader.download.domain.company_evidence import CompanyDocument
from trader.download.infra.company_disclosure_source import CninfoMarketDisclosureSource
from trader.download.infra.company_document_files import CompanyDocumentFiles
from trader.download.infra.company_fact_inputs import DecodeCompanyFinancial
from trader.download.infra.company_fact_rows import ReadCompanyFactRows
from trader.download.infra.company_facts_index import ReadCompanyPublicationIdentity
from trader.download.infra.company_facts_reader import SQLiteCompanyEvidenceReader
from trader.download.infra.company_financial_publication import CompanyFinancialReportValue, ReadCompanyFinancialSourceProofs
from trader.download.application.company_financial_producer import CompanyFinancialProducer
from trader.download.application.company_sync import RunCompanySyncRound
from trader.download.domain.company_sync import CompanySyncBudget, CompanySyncFamily, CompanySyncRoundScope
from trader.download.infra.company_sync_journal import SQLiteCompanySyncRoundJournal
from trader.download.domain.company_source_records import CompanyFinancialSourcePage
from trader.download.infra.company_facts_index import CompanyInputReceipt, SQLiteCompanyFactsIndex
from trader.download.infra.company_facts_protocol import RequireCompanyTime, DecodeCompanyLabel, EVIDENCE_SOURCES
from trader.download.infra.company_field_result_rows import ReadCompanyFieldResults
from trader.download.infra.company_normalized_index import CompanyNormalizationBaseline, CompanyNormalizationBaselineDigest, SQLiteCompanyNormalizedIndex
from trader.download.infra.company_field_publication import CompanyFieldProofRulesHash
from trader.download.infra.company_financial_observations import ReadCompanyFinancialObservations
from trader.download.infra.company_structured_source import DecodeCompanyFinancialSourcePage
from trader.infra.atomic_files import AtomicWriteJson
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession, SqliteTransaction
from trader.infra.sqlite_policy import SqliteIoPolicy
from trader.infra.sqlite_publication import VerifySqliteFile


@dataclass(frozen=True, slots=True)
class _ExpectedCell:
    code: str
    period: date
    disclosed_at: datetime
    field: CompanyFinancialReportField
    value: float | None
    state: CompanyFinancialReportState
    source_key: str
    record_hash: str
    pdf_hash: str


class _CountingText(CompanyDocumentFiles):
    reads = 0

    def read(self, digest: bytes, *, check: Callable[[], None] | None = None) -> str:
        self.reads += 1
        text = super().read(digest, check=check)
        if not isinstance(text, str):
            raise ValueError("company_financial_replay_text_type_invalid")
        return text


def _json(path: Path) -> object:
    with path.open("rb") as stream:
        content = stream.read(16 * 1024 * 1024 + 1)
    if len(content) > 16 * 1024 * 1024:
        raise ValueError("company_financial_replay_input_too_large")
    return json.loads(content)


def _expectations(path: Path) -> tuple[_ExpectedCell, ...]:
    payload = _json(path)
    if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
        raise ValueError("company_financial_replay_annotations_invalid")
    rows = payload["rows"]
    if not 1 <= len(rows) <= 400:
        raise ValueError("company_financial_replay_annotations_limit")
    expected: list[_ExpectedCell] = []
    for row in rows:
        field = "basic_eps" if row["field"] == "eps" else row["field"]
        if field not in {"basic_eps", "roe_pct", "core_profit_growth_pct", "revenue_growth_pct"}:
            raise ValueError("company_financial_replay_field_unsupported")
        state = row["state"]
        if state not in {"numeric", "not_applicable", "not_disclosed", "not_meaningful", "unquantified"}:
            raise ValueError("company_financial_replay_expectation_invalid")
        value = row["reported_value"]
        if (state == "numeric") != (type(value) in {int, float}):
            raise ValueError("company_financial_replay_expectation_invalid")
        if state != "numeric" and value is not None:
            raise ValueError("company_financial_replay_expectation_invalid")
        expected.append(_ExpectedCell(
            row["code"], date.fromisoformat(row["report_date"]), datetime.fromisoformat(row["disclosed_at"]),
            cast(CompanyFinancialReportField, field), float(value) if value is not None else None,
            cast(CompanyFinancialReportState, state), row["source_key"], row["record_hash"], row["pdf_sha256"],
        ))
    if len({(cell.code, cell.period, cell.field) for cell in expected}) != len(expected) or len({cell.code for cell in expected}) > 50:
        raise ValueError("company_financial_replay_population_invalid")
    return tuple(expected)


def _financials(path: Path, codes: tuple[str, ...], observed_at: datetime, policy: SqliteIoPolicy) -> tuple[CompanyFinancial, ...]:
    slots = ",".join("?" for _ in codes)
    with SqliteSession(path, SqliteConnectionOptions(read_only=True), sqlite_policy=policy) as connection:
        return tuple(DecodeCompanyFinancial(row) for row in connection.run(
            f"SELECT * FROM financials WHERE code IN ({slots}) AND published_at>=? AND published_at<=? ORDER BY code,report_date",
            (*codes, (observed_at.date() - timedelta(days=365)).isoformat(), observed_at.isoformat()),
        ))


def AuditCompanyFinancialHoldout(
    source: CninfoMarketDisclosureSource,
    requests: tuple[CompanyFinancialReportRequest, ...],
    *,
    observed_at: datetime,
    directory: Path,
    deadline: float,
) -> dict[str, object]:
    """Collect new, bounded source observations with the parser frozen before retrieval."""
    RequireIsolatedReplay(directory, (PROJECT_ROOT,))
    if (not 1 <= len(requests) <= 12 or len({item.code for item in requests}) != len(requests)
        or any(item.report_date.month != 9 for item in requests) or directory.exists()
        or observed_at.utcoffset() is None or not 1 <= deadline - time.monotonic() <= 900):
        raise ValueError("company_financial_holdout_selection_invalid")
    directory.mkdir(parents=True)
    results: list[dict[str, object]] = []
    for request in requests:
        if time.monotonic() >= deadline:
            raise TimeoutError("company_financial_holdout_deadline_exceeded")
        rows = source.financial_reports(request, observed_at=observed_at)
        if not 1 <= len(rows) <= 4:
            raise ValueError("company_financial_holdout_sources_unbound")
        for row in rows:
            if time.monotonic() >= deadline:
                raise TimeoutError("company_financial_holdout_deadline_exceeded")
            source.download_document(row)
            extraction = source.extract_document(row)
            document = CompanyDocument(row, extraction.text, True)
            parsed = ExtractCompanyFinancialReport(document)
            results.append({
                "code": request.code, "report_date": str(request.report_date), "source_key": row.identity,
                "title": row.title, "url": row.url,
                "record_hash": row.digest, "published_at": row.published_at.isoformat(),
                "pdf_sha256": extraction.payload_hash.hex(), "body_sha256": hashlib.sha256(extraction.text.encode()).hexdigest(),
                "extraction_policy": extraction.policy_id, "supported": parsed is not None,
                "cells": [{"field": cell.field, "value": cell.value, "state": cell.state,
                           "raw_value": cell.raw_value, "statement": cell.statement,
                           "basis_statement": cell.basis_statement} for cell in parsed.cells] if parsed is not None else [],
            })
    result: dict[str, object] = {
        "status": "degraded", "production_adopted": False, "field_correctness_verified": False,
        "observed_at": observed_at.isoformat(), "parser_digest": COMPANY_FINANCIAL_REPORT_DIGEST.hex(),
        "stocks": len(requests), "sources": len(results), "items": results,
        "interpretation": "independent_source_column_annotation_required; do_not_tune_parser_from_holdout",
    }
    AtomicWriteJson(directory / "holdout-observations.json", result)
    return result


def AuditCompanyFinancialAdoption(
    collection_path: Path, facts_path: Path, annotations: Path, *, observed_at: datetime,
    event_policy: CompanyEventPolicy, sqlite_policy: SqliteIoPolicy, budget_seconds: float,
) -> dict[str, object]:
    """Compare reviewed cells and original PDFs with the live writer's exact receipts."""
    if not 30 <= budget_seconds <= 900 or observed_at.utcoffset() is None:
        raise ValueError("company_financial_adoption_budget_or_time_invalid")
    deadline = time.monotonic() + budget_seconds
    def cancel() -> bool:
        return time.monotonic() >= deadline
    expected = _expectations(annotations)
    codes = tuple(sorted({cell.code for cell in expected}))
    observations: list[dict[str, object]] = []
    options = SqliteConnectionOptions(read_only=True)
    with SqliteSession(collection_path, options, sqlite_policy=sqlite_policy, cancel_requested=cancel) as raw, SqliteSession(
        facts_path, options, sqlite_policy=sqlite_policy, cancel_requested=cancel,
    ) as formal:
        versions = raw.run("PRAGMA data_version").require_one()[0], formal.run("PRAGMA data_version").require_one()[0]
        with SqliteTransaction(raw), SqliteTransaction(formal):
            facts = ReadCompanyFactRows(formal, codes)
            identity = ReadCompanyPublicationIdentity(formal)
            for cell in expected:
                if cancel():
                    raise TimeoutError("company_financial_adoption_deadline_exceeded")
                source_row = raw.run("SELECT * FROM financials WHERE code=? AND report_date=?", (cell.code, str(cell.period))).fetchone()
                original = DecodeCompanyFinancial(source_row) if source_row is not None else None
                current = next((fact.latest_financial for fact in facts if fact.code == cell.code), None)
                proofs = ReadCompanyFinancialSourceProofs(raw, formal, original, current, observed_at) if original is not None else ()
                proof = next((proof for proof in proofs if proof.field == cell.field), None)
                material = raw.run(
                    "SELECT d.record_hash,e.payload_hash FROM disclosures d JOIN document_extractions e "
                    "ON e.disclosure_id=d.id AND e.record_hash=d.record_hash WHERE d.source='cninfo' AND d.source_key=?",
                    (cell.source_key,),
                ).fetchone()
                actual = CompanyFinancialReportValue(current, cell.field) if current is not None else None
                matched = (proof is not None and proof.state == cell.state and actual == cell.value
                           and cell.source_key in proof.source_keys and original is not None
                           and original.published_at == cell.disclosed_at and material is not None
                           and material[0] == cell.record_hash and material[1] == bytes.fromhex(cell.pdf_hash))
                observations.append({"code": cell.code, "field": cell.field, "value": actual,
                                     "state": proof.state if proof is not None else "unproved", "matched": matched,
                                     "source_key": cell.source_key})
        reader = SQLiteCompanyEvidenceReader(facts_path, event_policy=event_policy, sqlite_policy=sqlite_policy)
        actual_facts: list[CompanyFacts] = []
        reader.visit(codes, actual_facts.append, cancel_requested=cancel)
        same_financials = tuple((fact.code, fact.latest_financial) for fact in actual_facts) == tuple((fact.code, fact.latest_financial) for fact in facts)
        same_publication = identity is not None and reader.publication_identity(cancel_requested=cancel) == identity.content_hash.hex()
        stable = versions == (raw.run("PRAGMA data_version").require_one()[0], formal.run("PRAGMA data_version").require_one()[0])
    matched_count = sum(item["matched"] is True for item in observations)
    return {"status": "passed" if matched_count == len(expected) and same_financials and same_publication and stable else "degraded",
            "read_only_source": True, "http_checked": False, "full_market_verified": False,
            "stocks": len(codes), "fields": len(expected), "matched": matched_count,
            "stable_read": stable, "consumer_financial_equal": same_financials, "consumer_publication_equal": same_publication,
            "publication_hash": identity.content_hash.hex() if identity is not None else None,
            "counts": dict(Counter(item["state"] for item in observations)), "items": observations}


class _FrozenFinancialPages:
    def __init__(self, pages: tuple[CompanyFinancialSourcePage, ...]) -> None:
        self.pages, self.calls = pages, 0

    def financial_observations(self, query: StructuredCompanyQuery, *, observed_at: datetime) -> CompanyFinancialSourcePage:
        self.calls += 1
        selected = next((page for page in self.pages if page.page == query.page), None)
        if selected is None or any(item.observed_at != observed_at for item in selected.observations):
            raise ValueError("company_normalized_replay_frozen_page_missing")
        return selected


def _source_file_identity(path: Path) -> tuple[tuple[int, int, int] | None, ...]:
    result: list[tuple[int, int, int] | None] = []
    for item in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        try:
            metadata = item.stat()
            result.append((metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns))
        except FileNotFoundError:
            result.append(None)
    return tuple(result)


def _decode_frozen_page(path: Path, query: StructuredCompanyQuery, observed_at: datetime) -> CompanyFinancialSourcePage:
    with path.open("rb") as stream:
        content = stream.read(16 * 1024 * 1024 + 1)
    if len(content) > 16 * 1024 * 1024:
        raise ValueError("company_normalized_replay_page_too_large")
    payload: object = json.loads(content)
    if not isinstance(payload, Mapping):
        raise ValueError("company_normalized_replay_page_invalid")
    return DecodeCompanyFinancialSourcePage(payload, query, observed_at=observed_at, response_hash=hashlib.sha256(content).hexdigest())


def AuditCompanyNormalizedRound(
    *,
    collection_path: Path,
    facts_path: Path,
    documents: CompanyDocumentFiles,
    page_paths: tuple[Path, ...],
    query: StructuredCompanyQuery,
    observed_at: datetime,
    directory: Path,
    event_policy: CompanyEventPolicy,
    sqlite_policy: SqliteIoPolicy,
    budget_seconds: float,
    document_overlays: tuple[CompanyDocumentFiles, ...] = (),
) -> dict[str, object]:
    """Use typed reproof plus frozen real pages; never write or migrate either input database."""
    RequireIsolatedReplay(directory, (PROJECT_ROOT, collection_path.parent, facts_path.parent, documents.root, *(owner.root for owner in document_overlays)))
    if directory.exists() or not 1 <= len(page_paths) <= 256 or not 30 <= budget_seconds <= 900:
        raise ValueError("company_normalized_replay_new_directory_required")
    deadline = time.monotonic() + budget_seconds

    def check() -> None:
        if time.monotonic() >= deadline:
            raise TimeoutError("company_normalized_replay_deadline_exceeded")

    before = (_source_file_identity(collection_path), _source_file_identity(facts_path))
    VerifySqliteFile(facts_path, sqlite_policy=sqlite_policy, check=check, file_only=True)
    research = ReadCompanyResearchBaseline(collection_path, facts_path, documents,
        event_policy=event_policy, sqlite_policy=sqlite_policy, check=check,
        cancel_requested=lambda: time.monotonic() >= deadline, document_overlays=document_overlays, cutoff=observed_at)
    baseline, original_fields, material_reads = research.baseline, research.original_fields, research.material_reads
    original_facts, original_identity, receipts = baseline.facts, baseline.identity, baseline.receipts
    codes = tuple(fact.code for fact in original_facts)
    options = SqliteConnectionOptions(read_only=True)
    pages = tuple(_decode_frozen_page(path, replace(query, page=position), observed_at)
                  for position, path in enumerate(page_paths, 1))
    source = _FrozenFinancialPages(pages)
    directory.mkdir(parents=True)
    formal_writer = SQLiteCompanyFactsIndex(directory / "facts.sqlite3", event_policy=event_policy, sqlite_policy=sqlite_policy)
    index = SQLiteCompanyNormalizedIndex(directory / "normalized.sqlite3", formal_writer,
                                         event_policy=event_policy, sqlite_policy=sqlite_policy, clean_material=lambda: None)
    index.initialize()
    index.seed(baseline)
    reader = SQLiteCompanyEvidenceReader(formal_writer.path, event_policy=event_policy, sqlite_policy=sqlite_policy)
    seeded_facts: list[CompanyFacts] = []
    reader.visit(codes, seeded_facts.append)
    seed_consumer_equal = tuple(seeded_facts) == original_facts
    failed: str | None = None
    result = None
    try:
        journal = SQLiteCompanySyncRoundJournal(directory / "requests.sqlite3", sqlite_policy=sqlite_policy, publication_identity=reader.publication_identity)
        journal.initialize()
        result = RunCompanySyncRound(journal, (CompanyFinancialProducer(index, source, query,
            qualification_gaps=("latest_financial_population_unverified", "financial_first_disclosure_revision_unverified"), publication_scope="isolated_candidate"),),
            CompanySyncRoundScope(codes, observed_at, (CompanySyncFamily.FINANCIAL,)), CompanySyncBudget(512, 2048, 256, deadline),
            monotonic=time.monotonic, cancel_requested=lambda: time.monotonic() >= deadline)
        failed = result.families[0].result.failure if result.families else None
    except ValueError as exc:
        if str(exc) != "company_normalization_revision_unproven":
            raise
        failed = str(exc)
    with SqliteSession(formal_writer.path, options, sqlite_policy=sqlite_policy) as formal:
        current_facts = ReadCompanyFactRows(formal, codes)
        current_fields = ReadCompanyFieldResults(formal, codes)
        identity = ReadCompanyPublicationIdentity(formal)
        current_receipts = tuple(CompanyInputReceipt(row[0], row[1], row[2], row[3], row[4], row[5], RequireCompanyTime(row[6]), row[7], row[8])
                                 for row in formal.run("SELECT * FROM company_input_receipt ORDER BY source,source_key,code,record_hash,extraction_hash"))
    final_facts: list[CompanyFacts] = []
    reader.visit(codes, final_facts.append)
    old_verified = tuple(item for item in original_fields if item.state == "verified")
    reproved = sum(any((item.code, item.event_id, item.field, item.value, item.state) ==
                      (old.code, old.event_id, old.field, old.value, "verified") for item in current_fields) for old in old_verified)
    risk_preserved = tuple((item.code, item.risk_events) for item in original_facts) == tuple((item.code, item.risk_events) for item in current_facts)
    receipt_preserved = set(receipts).issubset(current_receipts)
    original_fields_preserved = original_fields == current_fields
    stable = before == (_source_file_identity(collection_path), _source_file_identity(facts_path))
    summary: dict[str, object] = {
        "status": "failed_revision_unproven" if failed and "revision_unproven" in failed else "failed_financial_input" if failed else "candidate_completed_pending_source_admission",
        "failure": failed, "source_data_writes": 0, "network_requests": 0, "source_unchanged": stable,
        "source_publication_hash": original_identity.content_hash.hex(), "field_rules_hash": baseline.field_rules_hash.hex(),
        "source_pages": len(pages), "source_raw_rows": sum(page.raw_count for page in pages),
        "source_observations": sum(len(page.observations) for page in pages), "frozen_source_calls": source.calls,
        "stocks": len(codes), "fields": len(original_fields), "old_verified": len(old_verified),
        "current_rule_reproved": reproved, "current_rule_unreproved": len(old_verified) - reproved,
        "material_reads": material_reads, "seed_consumer_equal": seed_consumer_equal,
        "consumer_equal": tuple(final_facts) == current_facts,
        "publication_hash": identity.content_hash.hex() if identity else None,
        "original_risks_preserved": risk_preserved, "original_receipts_preserved": receipt_preserved,
        "original_fields_preserved": original_fields_preserved,
        "preservation_status": "passed" if risk_preserved and receipt_preserved and original_fields_preserved and stable else "failed_preservation",
        "completed_financial_scope": result.families[0].result.processed_scope_complete if result and result.families else False,
        "needs_retry": True, "source_admission_verified": False, "production_adopted": False,
        "field_correctness_verified": False, "http_checked": False,
    }
    AtomicWriteJson(directory / "replay.json", summary)
    return summary


def AuditCompanyNormalizedRestart(
    *,
    directory: Path,
    query: StructuredCompanyQuery,
    observed_at: datetime,
    event_policy: CompanyEventPolicy,
    sqlite_policy: SqliteIoPolicy,
    budget_seconds: float,
) -> dict[str, object]:
    """Resume exactly the existing isolated cohort without reseeding or reproving text."""
    RequireIsolatedReplay(directory, (PROJECT_ROOT,))
    if not 10 <= budget_seconds <= 120:
        raise ValueError("company_normalized_restart_budget_invalid")
    deadline = time.monotonic() + budget_seconds
    def cancel() -> bool:
        return time.monotonic() >= deadline
    facts = SQLiteCompanyFactsIndex(directory / "facts.sqlite3", event_policy=event_policy, sqlite_policy=sqlite_policy)
    index = SQLiteCompanyNormalizedIndex(directory / "normalized.sqlite3", facts, event_policy=event_policy,
                                         sqlite_policy=sqlite_policy, clean_material=lambda: None)
    index.initialize()
    options = SqliteConnectionOptions(read_only=True)
    with SqliteSession(facts.path, options, sqlite_policy=sqlite_policy, cancel_requested=cancel) as formal, SqliteTransaction(formal):
        codes = tuple(row[0] for row in formal.run("SELECT code FROM company_facts ORDER BY code"))
        before_head = ReadCompanyPublicationIdentity(formal)
        before_fields = ReadCompanyFieldResults(formal, codes)
        before_metadata = ReadCompanyFinancialObservations(formal, codes)
        before_receipts = tuple(tuple(row) for row in formal.run("SELECT * FROM company_input_receipt ORDER BY source,source_key,code,record_hash,extraction_hash"))
    source = _FrozenFinancialPages(())
    reader = SQLiteCompanyEvidenceReader(facts.path, event_policy=event_policy, sqlite_policy=sqlite_policy)
    journal = SQLiteCompanySyncRoundJournal(directory / "requests.sqlite3", sqlite_policy=sqlite_policy, publication_identity=reader.publication_identity)
    journal.initialize()
    result = RunCompanySyncRound(journal, (CompanyFinancialProducer(index, source, query,
        qualification_gaps=("latest_financial_population_unverified", "financial_first_disclosure_revision_unverified"), publication_scope="isolated_candidate"),),
        CompanySyncRoundScope(codes, observed_at, (CompanySyncFamily.FINANCIAL,)), CompanySyncBudget(512, 2048, 256, deadline),
        monotonic=time.monotonic, cancel_requested=cancel)
    with SqliteSession(facts.path, options, sqlite_policy=sqlite_policy, cancel_requested=cancel) as formal, SqliteTransaction(formal):
        after_head = ReadCompanyPublicationIdentity(formal)
        fields_equal = before_fields == ReadCompanyFieldResults(formal, codes)
        metadata_equal = before_metadata == ReadCompanyFinancialObservations(formal, codes)
        receipts_equal = before_receipts == tuple(tuple(row) for row in formal.run("SELECT * FROM company_input_receipt ORDER BY source,source_key,code,record_hash,extraction_hash"))
    summary: dict[str, object] = {
        "completed_financial_scope": result.families[0].result.processed_scope_complete, "frozen_source_calls": source.calls,
        "changed_codes": [] if before_head == after_head else list(result.families[0].result.changed_codes), "publication_unchanged": before_head == after_head,
        "fields_unchanged": fields_equal, "metadata_unchanged": metadata_equal, "receipts_unchanged": receipts_equal,
        "publication_hash": after_head.content_hash.hex() if after_head else None,
        "network_requests": 0, "reproof_reads": 0, "needs_retry": result.needs_retry,
    }
    AtomicWriteJson(directory / "restart.json", summary)
    return summary


def AuditCompanyNormalizedSourceStability(
    *,
    collection_path: Path,
    facts_path: Path,
    directory: Path,
    sqlite_policy: SqliteIoPolicy,
    budget_seconds: float,
) -> dict[str, object]:
    """Audit current payloads and bind the explicit source codec to the existing typed seed.

    Keep the original round's physical-stat result. SQLite reader SHM metadata
    is measured separately; actual DB, WAL and seal bytes must remain identical.
    No proof text is read, no seed is replaced and no HTTP is issued.
    """
    RequireIsolatedReplay(directory, (PROJECT_ROOT, collection_path.parent, facts_path.parent))
    if not 10 <= budget_seconds <= 180:
        raise ValueError("company_normalized_source_audit_budget_invalid")
    deadline = time.monotonic() + budget_seconds
    def cancelled() -> bool:
        return time.monotonic() >= deadline
    def check() -> None:
        if cancelled():
            raise TimeoutError("company_normalized_source_audit_deadline_exceeded")
    def payload(path: Path) -> tuple[str | None, ...]:
        values: list[str | None] = []
        for suffix in ("", "-wal", ".sha256"):
            target = Path(str(path)+suffix)
            if not target.exists():
                values.append(None)
                continue
            digest = hashlib.sha256()
            with target.open("rb") as stream:
                while block := stream.read(1024*1024):
                    check()
                    digest.update(block)
            values.append(digest.hexdigest())
        return tuple(values)
    before_stat = (_source_file_identity(collection_path),_source_file_identity(facts_path))
    before_payload = (payload(collection_path),payload(facts_path))
    VerifySqliteFile(facts_path, sqlite_policy=sqlite_policy, file_only=True, check=check)
    options = SqliteConnectionOptions(read_only=True)
    with (
        SqliteSession(collection_path,options,sqlite_policy=sqlite_policy,cancel_requested=cancelled) as raw,
        SqliteSession(facts_path,options,sqlite_policy=sqlite_policy,cancel_requested=cancelled) as formal,
        SqliteSession(directory/"normalized.sqlite3",options,sqlite_policy=sqlite_policy,cancel_requested=cancelled) as normalized,
    ):
        versions = tuple(connection.run("PRAGMA data_version").require_one()[0] for connection in (raw,formal,normalized))
        with SqliteTransaction(raw),SqliteTransaction(formal),SqliteTransaction(normalized):
            source_identity = ReadCompanyResearchIdentity(formal)
            codes = tuple(row[0] for row in formal.run("SELECT code FROM company_facts ORDER BY code"))
            source_facts = ReadCompanyFactRows(formal,codes)
            source_fields = ReadCompanyFieldResults(formal,codes)
            normalized_fields = ReadCompanyFieldResults(normalized,codes)
            receipts = tuple(CompanyInputReceipt(row[0],row[1],row[2],row[3],row[4],row[5],RequireCompanyTime(row[6]),row[7],row[8])
                for row in formal.run("SELECT * FROM company_input_receipt ORDER BY source,source_key,code,record_hash,extraction_hash"))
            sources = tuple((row[0],DecodeCompanyLabel(EVIDENCE_SOURCES,row[1])) for row in formal.run("SELECT source_key,source FROM company_evidence_ref ORDER BY source_key"))
            seed = normalized.run("SELECT baseline_digest,source_content_hash,field_rules_hash,complete FROM company_normalization_seed WHERE singleton=1").require_one()
            baseline = CompanyNormalizationBaseline(source_facts,normalized_fields,receipts,sources,source_identity,CompanyFieldProofRulesHash())
            bound = seed[0] == CompanyNormalizationBaselineDigest(baseline) and seed[1] == source_identity.content_hash and seed[2] == baseline.field_rules_hash and seed[3] == 1
            values_equal = tuple((item.code,item.event_id,item.field,item.value) for item in source_fields) == tuple((item.code,item.event_id,item.field,item.value) for item in normalized_fields)
        stable_view = versions == tuple(connection.run("PRAGMA data_version").require_one()[0] for connection in (raw,formal,normalized))
    VerifySqliteFile(facts_path, sqlite_policy=sqlite_policy, file_only=True, check=check)
    after_payload = (payload(collection_path),payload(facts_path))
    after_stat = (_source_file_identity(collection_path),_source_file_identity(facts_path))
    original = directory/"replay.json"
    report: dict[str,object] = {
        "source_payload_unchanged":before_payload==after_payload,"stable_read_view":stable_view,
        "typed_seed_source_binding":bound,"source_field_values_preserved":values_equal,
        "source_publication_hash":source_identity.content_hash.hex(),"seed_baseline_digest":seed[0],
        "field_rules_hash":baseline.field_rules_hash.hex(),"stocks":len(codes),"fields":len(source_fields),
        "payload_suffixes":["database","wal","seal"],"payload_sha256_before":before_payload,"payload_sha256_after":after_payload,
        "physical_stat_suffixes":["database","wal","shm"],"physical_stat_before":before_stat,"physical_stat_after":after_stat,
        "physical_stat_unchanged":before_stat==after_stat,"shm_metadata_changed":tuple(item[2] for item in before_stat)!=tuple(item[2] for item in after_stat),
        "original_replay_sha256":hashlib.sha256(original.read_bytes()).hexdigest(),"original_replay_retained":True,
        "reproof_reads":0,"network_requests":0,"source_data_writes":0,
    }
    report["status"] = "passed" if before_payload==after_payload and stable_view and bound and values_equal else "failed"
    AtomicWriteJson(directory/"source-stability.json",report)
    return report


__all__ = ["AuditCompanyFinancialHoldout", "AuditCompanyFinancialAdoption", "AuditCompanyNormalizedRound", "AuditCompanyNormalizedRestart", "AuditCompanyNormalizedSourceStability"]
