"""Compare bounded requests for recorded failed PDFs without parsing or active writes."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path

from trader.bootstrap import OpenExternalHttp
from trader.download.infra.download_settings import LoadCompanySyncConfiguration
from trader.infra.http_contracts import HttpAttempt, HttpError, HttpRequest
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession
from trader.infra.sqlite_policy import SqliteIoPolicy


@dataclass(frozen=True)
class _DownloadVariant:
    name: str
    timeout: float
    browser_headers: bool
    io_timeout: float | None = None
    expanded: bool = False


def AuditCompanyDocumentDownloads(
    path: Path, *, budget_seconds: float, sqlite_policy: SqliteIoPolicy
) -> dict[str, object]:
    configuration = LoadCompanySyncConfiguration(Path(__file__).resolve().parents[2])
    deadline = time.monotonic() + budget_seconds
    with SqliteSession(path, SqliteConnectionOptions(read_only=True, timeout_seconds=3), sqlite_policy=sqlite_policy) as connection:
        samples = tuple(connection.run(
            "SELECT d.id,d.url FROM document_retries r JOIN disclosures d "
            "ON d.id=r.disclosure_id AND d.record_hash=r.record_hash "
            "WHERE r.failure IN ('timeout','HttpConnectionError','HttpError','http_transport_failed') "
            "AND EXISTS(SELECT 1 FROM disclosure_codes c JOIN company_population p ON p.code=c.code "
            "WHERE c.disclosure_id=d.id) ORDER BY r.attempted_at DESC,d.id LIMIT 2"
        ))
    variants = (
        _DownloadVariant("legacy_8_seconds", configuration.source_timeout_seconds, False),
        _DownloadVariant("browser_headers", configuration.source_timeout_seconds, True),
        _DownloadVariant("browser_headers_longer", 30.0, True),
        _DownloadVariant("legacy_repeat", configuration.source_timeout_seconds, False),
        _DownloadVariant(
            "production_fixed", configuration.document_download_timeout_seconds, False,
            configuration.source_timeout_seconds, True,
        ),
    )
    observations: list[dict[str, object]] = []
    for sample in samples:
        if not sample[1].startswith("https://static.cninfo.com.cn/finalpage/"):
            raise ValueError("company_document_audit_url_invalid")
        for variant in variants:
            if time.monotonic() >= deadline:
                break
            if observations:
                next_request = min(deadline, time.monotonic() + configuration.request_interval_seconds)
                while (delay := next_request - time.monotonic()) > 0:
                    time.sleep(min(0.25, delay))
                if time.monotonic() >= deadline:
                    break
            attempts: list[HttpAttempt] = []
            observation: dict[str, object] = {
                "disclosure_id": sample[0], "variant": variant.name,
                "timeout_seconds": variant.timeout, "passed": False,
                "io_timeout_seconds": variant.io_timeout,
            }
            headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.cninfo.com.cn/"} if variant.browser_headers else None
            with OpenExternalHttp(observe=attempts.append) as session:
                try:
                    response = session.execute(
                        HttpRequest(
                            "GET", sample[1], variant.timeout, tuple((headers or {}).items()),
                            deadline=min(deadline, time.monotonic() + variant.timeout),
                            max_response_bytes=(configuration.extended_document_limits if variant.expanded else configuration.document_limits).max_bytes,
                            cancel_requested=lambda: time.monotonic() >= deadline,
                            io_timeout_seconds=variant.io_timeout,
                        )
                    )
                    pdf = response.content.startswith(b"%PDF-")
                    observation.update({
                        "passed": response.status_code == 200 and pdf,
                        "is_pdf": pdf, "payload_hash": hashlib.sha256(response.content).hexdigest(),
                    })
                except HttpError as exc:
                    observation.update({
                        "failure": type(exc).__name__,
                        "cause": type(exc.__cause__).__name__ if exc.__cause__ is not None else None,
                    })
            observation["attempts"] = [{
                "seconds": round(attempt.elapsed_seconds, 3), "response_bytes": attempt.response_bytes,
                "status_code": attempt.status_code, "failure": attempt.failure,
                "phase": "before_response_headers" if attempt.status_code is None else "response_body",
            } for attempt in attempts]
            observations.append(observation)
    complete = bool(samples) and len(observations) == len(samples) * len(variants)
    corrected = [row for row in observations if row["variant"] == "production_fixed"]
    passed = complete and len(corrected) == len(samples) and all(row["passed"] for row in corrected)
    return {
        "schema_version": "company-resume", "status": "passed" if passed else "degraded",
        "summary": {"document_download_audit": {
            "read_only": True, "sample_size": len(samples), "complete": complete,
            "production_passed": passed,
            "observations": observations,
        }},
        "findings": [] if passed else ["company_document_download_incomplete"],
    }
