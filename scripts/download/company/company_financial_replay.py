"""Replay real financial inputs and reviewed official PDFs in a new isolated publication."""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import cast

from scripts.core.paths import PROJECT_ROOT
from scripts.download.company.output_paths import RequireIsolatedReplay
from trader.download.application.company_publication import PublishCompanyEvidence
from trader.download.domain.company_events import CompanyEventPolicy
from trader.download.domain.company_facts import CompanyFacts
from trader.download.domain.company_evidence import CompanyFinancial, CompanySyncConfiguration, Disclosure, StructuredCompanyPage, StructuredCompanyQuery
from trader.download.domain.company_financial_report import (
    COMPANY_FINANCIAL_REPORT_DIGEST, ExtractCompanyFinancialReport,
    CompanyFinancialReportField, CompanyFinancialReportRequest, CompanyFinancialReportState,
)
from trader.download.domain.company_evidence import CompanyDocument
from trader.download.infra.company_disclosure_source import CninfoMarketDisclosureSource
from trader.download.infra.company_document_files import CompanyDocumentFiles
from trader.download.infra.company_evidence_index import SQLiteCompanyEvidenceIndex
from trader.download.infra.company_fact_inputs import DecodeCompanyFinancial
from trader.download.infra.company_fact_rows import ReadCompanyFactRows
from trader.download.infra.company_facts_index import ReadCompanyPublicationIdentity
from trader.download.infra.company_facts_reader import SQLiteCompanyEvidenceReader
from trader.download.infra.company_financial_publication import CompanyFinancialReportValue, ReadCompanyFinancialSourceProofs
from trader.download.infra.company_raw_files import CompanyRawFiles
from trader.infra.atomic_files import AtomicWriteJson
from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession, SqliteTransaction
from trader.infra.sqlite_policy import SqliteIoPolicy


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


def AuditCompanyFinancialReplay(
    *,
    collection_path: Path,
    sources_report: Path,
    annotations: Path,
    materials: CompanyRawFiles,
    directory: Path,
    observed_at: datetime,
    configuration: CompanySyncConfiguration,
    event_policy: CompanyEventPolicy,
    sqlite_policy: SqliteIoPolicy,
    create_source: Callable[[CompanyRawFiles], CninfoMarketDisclosureSource],
    budget_seconds: float,
) -> dict[str, object]:
    """Exercise the normal writer with real financial records; no active database writes."""
    RequireIsolatedReplay(directory, (PROJECT_ROOT, collection_path.parent, materials.root))
    if directory.exists() or observed_at.utcoffset() is None or not 30 <= budget_seconds <= 3600:
        raise ValueError("company_financial_replay_new_directory_and_budget_required")
    deadline = time.monotonic() + budget_seconds

    def check() -> None:
        if time.monotonic() >= deadline:
            raise TimeoutError("company_financial_replay_deadline_exceeded")

    expected = _expectations(annotations)
    codes = tuple(sorted({cell.code for cell in expected}))
    inputs = _financials(collection_path, codes, observed_at, sqlite_policy)
    for cell in expected:
        report = next((report for report in inputs if report.code == cell.code and report.report_date == cell.period), None)
        if report is None or report.published_at != cell.disclosed_at or CompanyFinancialReportValue(report, cell.field) is not None:
            raise ValueError("company_financial_replay_source_identity_changed")
    payload = _json(sources_report)
    if not isinstance(payload, dict):
        raise ValueError("company_financial_replay_sources_invalid")
    catalog = payload["checks"][0]["official_financial_probe"]["items"]
    directory.mkdir(parents=True)
    raw_root = directory / "raw"
    raw_files = CompanyRawFiles(raw_root, capacity=configuration.raw_capacity_bytes,
        analysis_reserve=configuration.raw_analysis_reserve_bytes, free_reserve=configuration.temporary_disk_reserve_bytes,
        maximum_document_bytes=configuration.extended_document_limits.max_bytes)
    documents = _CountingText(raw_root / "text", max_text_characters=configuration.extended_document_limits.max_text_characters,
        max_bytes=configuration.temporary_text_capacity_bytes, reserve_bytes=configuration.temporary_disk_reserve_bytes)
    path, facts_path = raw_root / "company_collection.sqlite3", directory / "company_facts.sqlite3"

    def build() -> SQLiteCompanyEvidenceIndex:
        return SQLiteCompanyEvidenceIndex(path, facts_path=facts_path, documents=documents, event_policy=event_policy,
                                           sqlite_policy=sqlite_policy, raw_files=raw_files)

    def commit_structured(index: SQLiteCompanyEvidenceIndex) -> None:
        index.commit_structured(StructuredCompanyQuery("financial", observed_at.date() - timedelta(days=365), observed_at.date()),
                                StructuredCompanyPage("financial", 1, 1, len(inputs), financials=inputs), observed_at)

    index = build()
    index.initialize()
    commit_structured(index)
    PublishCompanyEvidence(index, codes, observed_at)
    source = create_source(raw_files)
    for item in catalog:
        if item["code"] not in codes:
            continue
        for metadata in item["sources"]:
            check()
            disclosure = Disclosure(metadata["source"], metadata["source_key"], (item["code"],), metadata["title"],
                                    datetime.fromisoformat(metadata["published_at"]), metadata["url"], metadata["record_hash"])
            wanted = tuple(cell for cell in expected if cell.code == item["code"] and cell.source_key == disclosure.identity)
            local = materials.local(disclosure, check=check)
            if not wanted or local is None or any(cell.record_hash != disclosure.digest or cell.pdf_hash != local.payload_hash.hex() for cell in wanted):
                raise ValueError("company_financial_replay_exact_pdf_unavailable")
            request = CompanyFinancialReportRequest(disclosure.codes[0], wanted[0].period, wanted[0].disclosed_at)
            index.commit_financial_reports(request, (disclosure,), observed_at)
            destination = raw_files.directory(disclosure) / "download.part"
            with raw_files.reserve(destination.parent, local.size + 65536):
                shutil.copyfile(local.path, destination)
                raw_files.complete(disclosure, destination, check=check)
            source.download_document(disclosure)
            extraction = source.extract_document(disclosure)
            if not index.commit_document(disclosure, extraction, observed_at):
                raise ValueError("company_financial_replay_document_not_accepted")
    publication = PublishCompanyEvidence(index, codes, observed_at)
    if publication.pending_codes:
        raise ValueError("company_financial_replay_publication_pending")
    reader = SQLiteCompanyEvidenceReader(facts_path, event_policy=event_policy, sqlite_policy=sqlite_policy)
    facts = reader.read(codes)
    observations: list[dict[str, object]] = []
    with SqliteSession(path, SqliteConnectionOptions(read_only=True), sqlite_policy=sqlite_policy) as collected, SqliteSession(
        facts_path, SqliteConnectionOptions(read_only=True), sqlite_policy=sqlite_policy,
    ) as formal:
        for cell in expected:
            current = next(fact.latest_financial for fact in facts if fact.code == cell.code)
            original = next(report for report in inputs if report.code == cell.code and report.report_date == cell.period)
            proofs = ReadCompanyFinancialSourceProofs(collected, formal, original, current, observed_at)
            proof = next((proof for proof in proofs if proof.field == cell.field), None)
            actual = CompanyFinancialReportValue(current, cell.field) if current is not None else None
            matched = proof is not None and proof.state == cell.state and actual == cell.value and cell.source_key in proof.source_keys
            observations.append({"code": cell.code, "field": cell.field, "expected": cell.value, "actual": actual,
                                 "state": proof.state if proof is not None else "unproved", "matched": matched,
                                 "source_key": cell.source_key, "record_hash": cell.record_hash, "pdf_sha256": cell.pdf_hash})
        first_identity = ReadCompanyPublicationIdentity(formal)
        valid = tuple(tuple(row) for row in formal.run("SELECT * FROM company_input_receipt WHERE outcome='valid' ORDER BY code,source_key"))
        attempts = formal.run("SELECT COUNT(*) FROM company_input_receipt WHERE outcome='field_attempted'").require_one()[0]
    restarted = build()
    restarted.initialize()
    commit_structured(restarted)
    reads_before = documents.reads
    restarted.prepare_publication(codes, observed_at)
    second_facts = reader.read(codes)
    with SqliteSession(facts_path, SqliteConnectionOptions(read_only=True), sqlite_policy=sqlite_policy) as formal:
        second_identity = ReadCompanyPublicationIdentity(formal)
        same_valid = valid == tuple(tuple(row) for row in formal.run("SELECT * FROM company_input_receipt WHERE outcome='valid' ORDER BY code,source_key"))
        second_attempts = formal.run("SELECT COUNT(*) FROM company_input_receipt WHERE outcome='field_attempted'").require_one()[0]
    publication_reads = documents.reads - reads_before
    source_unchanged = _financials(collection_path, codes, observed_at, sqlite_policy) == inputs
    preserved = first_identity == second_identity and facts == second_facts and same_valid and attempts == second_attempts
    passed = all(item["matched"] is True for item in observations) and preserved and publication_reads == 0 and source_unchanged
    result: dict[str, object] = {"status": "passed" if passed else "degraded", "isolated": True, "production_adopted": False,
              "historical_edition_availability_verified": False, "risk_preservation_verified": False,
              "read_only_source": True, "stocks": len(codes), "fields": len(expected), "matched": sum(item["matched"] is True for item in observations),
              "input_digest": CanonicalDigest(inputs), "annotations_sha256": hashlib.sha256(annotations.read_bytes()).hexdigest(),
              "source_financial_inputs_unchanged": source_unchanged, "restart_facts_and_identity_preserved": preserved,
              "old_valid_preserved": same_valid, "second_new_attempts": second_attempts - attempts,
              "second_publication_text_reads": publication_reads, "counts": dict(Counter(item["state"] for item in observations)),
              "publication_hash": second_identity.content_hash.hex() if second_identity is not None else None,
              "items": observations}
    AtomicWriteJson(directory / "financial-replay.json", result)
    return result


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
    cancel = lambda: time.monotonic() >= deadline
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


__all__ = ["AuditCompanyFinancialReplay", "AuditCompanyFinancialHoldout", "AuditCompanyFinancialAdoption"]
