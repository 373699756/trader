"""Same-document serial/parallel probe with real PDFs and disposable company indexes."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Sequence
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from trader.bootstrap import OpenExternalHttp
from trader.download.application.company_evidence import CompanyEvidenceDependencies, SyncCompanyEvidence
from trader.download.domain.company_event_extraction import CompanyDocumentNeedsReview
from trader.download.domain.company_evidence import (
    CompanyEvidenceRequest,
    Disclosure,
    DisclosurePage,
    DisclosurePartition,
    EvidenceMarket,
    EvidenceSyncProgress,
    Market,
)
from trader.download.infra.company_disclosure_source import CninfoMarketDisclosureSource
from trader.download.infra.company_document_files import CompanyDocumentFiles
from trader.download.infra.company_evidence_index import SQLiteCompanyEvidenceIndex
from trader.download.infra.company_fact_inputs import DecodeCompanyDisclosure
from trader.download.infra.company_facts_reader import SQLiteCompanyEvidenceReader
from trader.download.infra.company_structured_source import EastmoneyCompanySource
from trader.download.infra.download_settings import LoadCompanySyncConfiguration
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession
from trader.infra.workers import BoundedExecutor


class _SampleIndex(SQLiteCompanyEvidenceIndex):
    def partitions(self, markets: Sequence[Market], observed_at: datetime) -> tuple[DisclosurePartition, ...]:
        return ()

    def component_due(self, component: str, scope: str, observed_at: datetime) -> bool:
        return False


def _sample(path: Path) -> tuple[Disclosure, ...]:
    selected: dict[tuple[str, str], Disclosure] = {}
    with SqliteSession(path, SqliteConnectionOptions(read_only=True, timeout_seconds=3)) as connection:
        rows = connection.run(
            "SELECT d.source,d.source_key,d.kind,d.title,d.published_at,d.url,d.record_hash,c.code "
            "FROM disclosures d JOIN disclosure_codes c ON c.disclosure_id=d.id "
            "WHERE d.id IN (SELECT id FROM disclosures WHERE kind='announcement' ORDER BY published_at DESC LIMIT 3000) "
            "ORDER BY d.published_at DESC,d.id,c.code"
        )
        for row in rows:
            if CompanyDocumentNeedsReview(row["title"]):
                item = DecodeCompanyDisclosure(row)
                key = item.source, item.identity
                if key not in selected and len(selected) == 12:
                    break
                prior = selected.get(key)
                selected[key] = item if prior is None else replace(prior, codes=(*prior.codes, *item.codes))
    return tuple(selected.values())


def AuditCompanyPipeline(path: Path, directory: Path, *, budget_seconds: float) -> dict[str, object]:
    items = _sample(path)
    if not items:
        return {"schema_version": "company-resume", "status": "degraded", "findings": ["no_document_sample"]}
    codes = tuple(sorted({code for item in items for code in item.codes}))
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    configuration = LoadCompanySyncConfiguration(Path(__file__).resolve().parents[2])
    deadline = time.monotonic() + budget_seconds
    runs: list[dict[str, object]] = []
    signatures: list[str] = []
    for count in (1, 2):
        database = directory / f"pipeline-{count}.sqlite3"
        if database.exists():
            raise ValueError("company_probe_requires_fresh_directory")
        index = _SampleIndex(
            database,
            facts_path=directory / f"facts-{count}.sqlite3",
            documents=CompanyDocumentFiles(directory / f"text-{count}"),
        )
        index.initialize()
        for market, day in sorted({(EvidenceMarket(item.codes[0]), item.published_at.date()) for item in items}):
            # This is explicitly a sampled input fixture, never a coverage certificate.
            rows = tuple(
                item for item in items if EvidenceMarket(item.codes[0]) == market and item.published_at.date() == day
            )
            index.commit_page(DisclosurePartition(market, day, day), DisclosurePage(rows, len(rows), False), now)
        started = time.monotonic()
        first_publication: float | None = None
        attempts = 0

        def progress(event: EvidenceSyncProgress, started: float = started) -> None:
            nonlocal first_publication, attempts
            attempts = max(attempts, event.attempted_documents)
            if event.component == "publish" and event.status == "complete" and first_publication is None:
                first_publication = time.monotonic() - started

        with ExitStack() as resources:
            sessions = tuple(resources.enter_context(OpenExternalHttp()) for _ in range(count))

            def cancelled() -> bool:
                return time.monotonic() >= deadline

            sources = tuple(
                CninfoMarketDisclosureSource(
                    session,
                    monotonic=time.monotonic,
                    sleeper=time.sleep,
                    timeout_seconds=configuration.source_timeout_seconds,
                    document_parse_timeout_seconds=configuration.document_parse_timeout_seconds,
                    document_limits=configuration.document_limits,
                    request_interval_seconds=configuration.request_interval_seconds,
                    cancel_requested=cancelled,
                )
                for session in sessions
            )
            workers = BoundedExecutor(worker_count=count, queue_capacity=2, thread_name_prefix="company-probe")
            workers.start()
            resources.callback(workers.stop, wait=True, cancel_futures=True)
            dependencies = CompanyEvidenceDependencies(
                index,
                sources[0],
                EastmoneyCompanySource(sessions[0], monotonic=time.monotonic),
                time.monotonic,
                progress,
                cancelled,
                document_sources=sources,
                document_executor=workers,
            )
            result = SyncCompanyEvidence(
                dependencies, CompanyEvidenceRequest(codes, now, replace(configuration, document_workers=count))
            )
            elapsed = time.monotonic() - started
            first_attempts = attempts
            attempts = 0
            resumed = SyncCompanyEvidence(
                dependencies, CompanyEvidenceRequest(codes, now, replace(configuration, document_workers=count))
            )
        facts = SQLiteCompanyEvidenceReader(index.facts_path).read(codes)
        # Compare business facts; observation timestamps naturally differ by worker count.
        comparable = tuple(
            replace(
                row,
                published_at=now,
                review_reasons=tuple(replace(reason, detected_at=now) for reason in row.review_reasons),
            )
            for row in facts
        )
        signature = hashlib.sha256(repr(comparable).encode()).hexdigest()
        signatures.append(signature)
        runs.append(
            {
                "workers": count,
                "sample_documents": len(items),
                "attempts": first_attempts,
                "pending_documents": result.pending_documents,
                "published_codes": result.published_codes,
                "seconds": round(elapsed, 3),
                "first_publication_seconds": first_publication,
                "resume_changed_rows": resumed.changed_rows,
                "resume_pending_documents": resumed.pending_documents,
                "resume_attempts": attempts,
                "failures": result.failures,
                "facts_digest": signature,
            }
        )
        if time.monotonic() >= deadline:
            break
    equivalent = len(signatures) == 2 and signatures[0] == signatures[1]
    passed = equivalent and all(
        row["pending_documents"] == 0
        and row["attempts"] == len(items)
        and row["resume_changed_rows"] == 0
        and row["resume_attempts"] == 0
        and row["published_codes"] == len(codes)
        for row in runs
    )
    return {
        "schema_version": "company-resume",
        "status": "passed" if passed else "degraded",
        "summary": {"pipeline_audit": {"isolated_sample": True, "equivalent_facts": equivalent, "runs": runs}},
        "findings": [] if passed else ["company_pipeline_probe_incomplete"],
    }
