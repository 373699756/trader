"""One recorded low-quality PDF, no source writes or complete company replay."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy

import time
from dataclasses import asdict
from pathlib import Path

from trader.bootstrap import OpenExternalHttp
from trader.download.domain.company_evidence import BuildCompanyDocumentPolicies, CompanyDocumentFailure
from trader.download.infra.company_disclosure_source import CninfoMarketDisclosureSource
from trader.download.infra.company_document_ocr import CompanyOcrEngine, DetectCompanyOcr
from trader.download.infra.company_fact_inputs import DecodeCompanyDisclosure
from trader.download.infra.download_settings import LoadCompanySyncConfiguration
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession


def AuditCompanyOcr(path: Path, *, budget_seconds: float, sqlite_policy: SqliteIoPolicy) -> dict[str, object]:
    with SqliteSession(path, SqliteConnectionOptions(read_only=True, timeout_seconds=3), sqlite_policy=sqlite_policy) as connection:
        row = connection.run(
            "SELECT d.source,d.source_key,d.kind,d.title,d.published_at,d.url,d.record_hash,c.code,r.payload_hash "
            "FROM document_retries r JOIN disclosures d ON d.id=r.disclosure_id "
            "JOIN disclosure_codes c ON c.disclosure_id=d.id "
            "WHERE r.failure='disclosure_document_ocr_low_quality' AND r.record_hash=d.record_hash "
            "ORDER BY r.attempted_at,d.id,c.code LIMIT 1"
        ).fetchone()
    if row is None:
        return {"schema_version": "company-resume", "status": "degraded", "summary": {"ocr_audit": {"reason": "no_failed_sample"}}}
    item, previous_hash = DecodeCompanyDisclosure(row), row[8]
    configuration = LoadCompanySyncConfiguration(Path(__file__).resolve().parents[2])
    capability = DetectCompanyOcr(configuration.document_ocr_languages)
    policies = BuildCompanyDocumentPolicies(configuration, capability.identity)
    started = time.monotonic()
    deadline = started + budget_seconds

    def cancelled() -> bool:
        return time.monotonic() >= deadline

    engine = CompanyOcrEngine(
        capability,
        timeout_seconds=configuration.document_ocr_timeout_seconds,
        max_pages=configuration.document_ocr_max_pages,
        dpi=configuration.document_ocr_dpi,
        cancel_requested=cancelled,
    )
    observation: dict[str, object] = {"sample_size": 1, "active_writes": False, "policy": policies.ocr}
    with OpenExternalHttp() as session:
        source = CninfoMarketDisclosureSource(
            session,
            monotonic=time.monotonic,
            sleeper=time.sleep,
            timeout_seconds=configuration.source_timeout_seconds,
            document_parse_timeout_seconds=configuration.document_parse_timeout_seconds,
            document_limits=configuration.document_limits,
            extended_document_limits=configuration.extended_document_limits,
            request_interval_seconds=configuration.request_interval_seconds,
            cancel_requested=cancelled,
            ocr_engine=engine,
            document_policies=policies,
        )
        try:
            result = source.document(item, route="ocr")
            observation.update({"passed": True, "characters": len("".join(result.text.split())),
                                "confidence": result.quality, "same_pdf": result.payload_hash == previous_hash})
        except CompanyDocumentFailure as exc:
            observation.update({"passed": False, "failure": exc.reason, "same_pdf": exc.payload_hash == previous_hash,
                                "diagnostic": asdict(exc.diagnostic) if exc.diagnostic is not None else None})
        except (OSError, RuntimeError, ValueError) as exc:
            observation.update({"passed": False, "failure": type(exc).__name__})
    observation["seconds"] = time.monotonic() - started
    return {"schema_version": "company-resume", "status": "passed" if observation["passed"] else "degraded",
            "summary": {"ocr_audit": observation}}
