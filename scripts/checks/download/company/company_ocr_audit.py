"""Compare OCR candidates on seven failed PDFs without changing the source database."""

from __future__ import annotations

import time
from dataclasses import asdict
from pathlib import Path

from trader.bootstrap import OpenExternalHttp
from trader.download.domain.company_evidence import BuildCompanyDocumentPolicies, CompanyDocumentFailure
from trader.download.infra.company_disclosure_source import CninfoMarketDisclosureSource
from trader.download.infra.company_document_ocr import CompanyOcrEngine, DetectCompanyOcr
from trader.download.infra.company_fact_inputs import DecodeCompanyDisclosure
from trader.download.infra.company_raw_files import CompanyRawFiles
from trader.download.infra.download_settings import LoadCompanySyncConfiguration
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession
from trader.infra.sqlite_policy import SqliteIoPolicy
from trader.infra.atomic_files import AtomicWriteJson


def AuditCompanyOcr(
    path: Path, directory: Path, *, budget_seconds: float, sqlite_policy: SqliteIoPolicy,
) -> dict[str, object]:
    with SqliteSession(path, SqliteConnectionOptions(read_only=True, timeout_seconds=3), sqlite_policy=sqlite_policy) as connection:
        rows = connection.run(
            "SELECT d.source,d.source_key,d.kind,d.title,d.published_at,d.url,d.record_hash,MIN(c.code) AS code,r.payload_hash "
            "FROM document_retries r JOIN disclosures d ON d.id=r.disclosure_id "
            "JOIN disclosure_codes c ON c.disclosure_id=d.id "
            "WHERE r.failure='disclosure_document_ocr_low_quality' AND r.record_hash=d.record_hash "
            "GROUP BY d.id ORDER BY r.attempted_at,d.id LIMIT 7"
        ).fetchall()
    if not rows:
        return {"schema_version": "company-resume", "status": "degraded",
                "summary": {"ocr_audit": {"sample_size": 0, "active_writes": False, "reason": "no_failed_sample"}}}
    configuration = LoadCompanySyncConfiguration(Path(__file__).resolve().parents[4])
    capability = DetectCompanyOcr(configuration.document_ocr_languages)
    policies = BuildCompanyDocumentPolicies(configuration, capability.identity)
    started = time.monotonic()
    deadline = started + budget_seconds
    raw = CompanyRawFiles(directory / "inputs", capacity=configuration.raw_capacity_bytes,
                          analysis_reserve=configuration.raw_analysis_reserve_bytes,
                          free_reserve=configuration.temporary_disk_reserve_bytes)

    def cancelled() -> bool:
        return time.monotonic() >= deadline

    observations = []
    recovered = 0
    with OpenExternalHttp() as session:
        source = CninfoMarketDisclosureSource(
            session, monotonic=time.monotonic, sleeper=time.sleep,
            timeout_seconds=configuration.source_timeout_seconds,
            document_download_timeout_seconds=min(30.0, configuration.document_download_timeout_seconds),
            document_parse_timeout_seconds=configuration.document_parse_timeout_seconds,
            document_limits=configuration.document_limits, extended_document_limits=configuration.extended_document_limits,
            request_interval_seconds=configuration.request_interval_seconds, cancel_requested=cancelled,
            document_policies=policies, raw_files=raw,
            text_reserve_bytes=configuration.text_task_reserve_bytes,
            ocr_reserve_bytes=configuration.ocr_task_reserve_bytes,
        )
        for row in rows:
            item, previous_hash = DecodeCompanyDisclosure(row), row[8]
            observation: dict[str, object] = {"record": item.identity}
            runs = []
            try:
                source.download_document(item)
                local = raw.local(item)
                if local is None:
                    raise ValueError("audit_pdf_missing")
                observation["same_pdf"] = local.payload_hash == previous_hash
                for dpi, each_page in ((150, False), (150, True), (300, True)):
                    engine = CompanyOcrEngine(
                        capability, timeout_seconds=configuration.document_ocr_timeout_seconds,
                        page_timeout_seconds=configuration.document_ocr_page_timeout_seconds,
                        max_pages=configuration.document_ocr_max_pages, dpi=dpi,
                        cancel_requested=cancelled, retry_each_page=each_page,
                    )
                    before = time.monotonic()
                    run: dict[str, object] = {"dpi": dpi, "retry_each_page": each_page}
                    try:
                        text, quality = engine.extract(local.path, local.payload_hash)
                        run.update(passed=True, characters=len("".join(text.split())), confidence=quality)
                    except CompanyDocumentFailure as exc:
                        run.update(passed=False, failure=exc.reason,
                                   diagnostic=asdict(exc.diagnostic) if exc.diagnostic else None)
                    run["seconds"] = time.monotonic() - before
                    runs.append(run)
                    if each_page and run.get("passed") is True:
                        break
            except (OSError, RuntimeError, ValueError) as exc:
                observation["failure"] = type(exc).__name__
            observation["runs"] = runs
            if observation.get("same_pdf") is True and any(run.get("passed") is True and run.get("retry_each_page") is True for run in runs):
                recovered += 1
            observations.append(observation)
            AtomicWriteJson(directory / "ocr-progress.json", {"samples": observations})
            if cancelled():
                break
    passed = recovered == len(rows) and len(observations) == len(rows)
    return {
        "schema_version": "company-resume", "status": "passed" if passed else "degraded",
        "summary": {"ocr_audit": {"sample_size": len(observations), "active_writes": False, "policy": policies.ocr,
                                  "passed": passed, "recovered_documents": recovered,
                                  "seconds": time.monotonic() - started, "samples": observations}},
    }
