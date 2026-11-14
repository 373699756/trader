"""Compare failed-PDF requests or recover bounded documents in explicit isolation."""

from __future__ import annotations

from scripts.core.paths import PROJECT_ROOT as TOOL_ROOT

import hashlib
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from trader.bootstrap import OpenExternalHttp
from scripts.download.company.output_paths import RequireIsolatedReplay
from scripts.download.company.company_failure_audit import ParseCompanyFailureIdentity
from trader.download.application.company_failures import CompanyFailureCode
from trader.download.domain.company_evidence import BuildCompanyDocumentPolicies, CompanyDocumentFailure, Disclosure
from trader.download.infra.company_disclosure_source import CninfoMarketDisclosureSource
from trader.download.infra.company_document_ocr import CompanyOcrEngine, DetectCompanyOcr
from trader.download.infra.company_document_parser import DetectCompanyText
from trader.download.infra.company_fact_inputs import DecodeCompanyDisclosure
from trader.download.infra.company_raw_files import CompanyRawFiles
from trader.download.infra.download_settings import LoadCompanySyncConfiguration
from trader.infra.atomic_files import AtomicWriteJson
from trader.infra.http_contracts import HttpAttempt, HttpError, HttpRequest
from trader.infra.sqlite_connection import SqliteConnection, SqliteConnectionOptions, SqliteSession
from trader.infra.sqlite_policy import SqliteIoPolicy


@dataclass(frozen=True)
class _DownloadVariant:
    name: str
    timeout: float
    browser_headers: bool
    io_timeout: float | None = None
    expanded: bool = False


def AuditCompanyDocumentDownloads(
    path: Path, *, budget_seconds: float, sqlite_policy: SqliteIoPolicy, directory: Path | None = None,
) -> dict[str, object]:
    """An explicit directory selects capacity-failure recovery without active database writes."""
    if directory is not None:
        return _recover_capacity_documents(path, directory, budget_seconds, sqlite_policy)
    configuration = LoadCompanySyncConfiguration(TOOL_ROOT)
    deadline = time.monotonic() + budget_seconds
    with SqliteSession(path, SqliteConnectionOptions(read_only=True, timeout_seconds=3), sqlite_policy=sqlite_policy) as connection:
        samples = tuple(connection.run(
            "SELECT d.id,d.url FROM document_retries r JOIN disclosures d "
            "ON d.id=r.disclosure_id AND d.record_hash=r.record_hash "
            "WHERE r.failure IN ('timeout','HttpConnectionError','HttpError','http_transport_failed') "
            "AND EXISTS(SELECT 1 FROM disclosure_codes c JOIN company_population p ON p.code=c.code "
            "WHERE c.disclosure_id=d.id) ORDER BY r.attempted_at DESC,d.id LIMIT 2"
        ))
        encrypted = tuple(connection.run(
            "SELECT d.id,d.url,r.payload_hash FROM document_retries r JOIN disclosures d "
            "ON d.id=r.disclosure_id AND d.record_hash=r.record_hash "
            "WHERE r.failure='disclosure_document_encrypted' ORDER BY d.id LIMIT 3"
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
    encrypted_observations: list[dict[str, object]] = []
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
                    is_pdf = response.content.startswith(b"%PDF-")
                    observation.update({
                        "passed": response.status_code == 200 and is_pdf,
                        "is_pdf": is_pdf, "payload_hash": hashlib.sha256(response.content).hexdigest(),
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
    for sample in encrypted:
        if time.monotonic() >= deadline:
            break
        if not sample[1].startswith("https://static.cninfo.com.cn/finalpage/"):
            raise ValueError("company_document_audit_url_invalid")
        observation = {"disclosure_id": sample[0], "read_only": True}
        try:
            with OpenExternalHttp() as session:
                response = session.execute(HttpRequest(
                    "GET", sample[1], configuration.document_download_timeout_seconds,
                    deadline=min(deadline, time.monotonic() + configuration.document_download_timeout_seconds),
                    max_response_bytes=configuration.extended_document_limits.max_bytes,
                    io_timeout_seconds=configuration.source_timeout_seconds,
                ))
            observation["status_code"] = response.status_code
            observation["same_pdf"] = hashlib.sha256(response.content).digest() == sample[2]
            if response.status_code != 200 or not response.content.startswith(b"%PDF-"):
                observation["failure"] = "pdf_unavailable"
            else:
                with tempfile.TemporaryDirectory(prefix="trader-encrypted-pdf-audit-") as temporary:
                    pdf = Path(temporary) / "input.pdf"
                    output = Path(temporary) / "text.txt"
                    pdf.write_bytes(response.content)
                    metadata = subprocess.run(
                        ("pdfinfo", str(pdf)), capture_output=True, timeout=10, check=False,
                    )
                    lines = metadata.stdout.decode("utf-8", errors="replace").splitlines()
                    observation["metadata_exit_code"] = metadata.returncode
                    observation["encrypted"] = next((line.split(":", 1)[1].strip() for line in lines if line.startswith("Encrypted:")), None)
                    observation["pages"] = next((line.split(":", 1)[1].strip() for line in lines if line.startswith("Pages:")), None)
                    observation["password_required"] = b"Incorrect password" in metadata.stderr
                    extracted = subprocess.run(
                        ("pdftotext", "-enc", "UTF-8", str(pdf), str(output)),
                        capture_output=True, timeout=10, check=False,
                    )
                    observation["text_exit_code"] = extracted.returncode
                    observation["text_bytes"] = output.stat().st_size if output.exists() else 0
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            observation["failure"] = type(exc).__name__
        encrypted_observations.append(observation)
    complete = bool(samples) and len(observations) == len(samples) * len(variants)
    corrected = [row for row in observations if row["variant"] == "production_fixed"]
    passed = complete and len(corrected) == len(samples) and all(row["passed"] for row in corrected)
    return {
        "schema_version": "company-resume", "status": "passed" if passed else "degraded",
        "summary": {"document_download_audit": {
            "read_only": True, "sample_size": len(samples), "complete": complete,
            "production_passed": passed,
            "observations": observations,
            "encrypted_observations": encrypted_observations,
        }},
        "findings": [] if passed else ["company_document_download_incomplete"],
    }


def _capacity_samples(connection: SqliteConnection) -> tuple[Disclosure, ...]:
    if connection.run("SELECT 1 FROM sqlite_master WHERE name='download_failure'").fetchone() is None:
        return ()
    selected: dict[str, Disclosure] = {}
    for row in connection.run(
        "SELECT diagnostic FROM download_failure WHERE reason='company_raw_capacity_exhausted' ORDER BY id DESC LIMIT 50"
    ):
        identity = ParseCompanyFailureIdentity(row[0])
        if identity is None or identity.source_key in selected:
            continue
        document = connection.run(
            "SELECT d.*,MIN(c.code) AS code FROM disclosures d JOIN disclosure_codes c ON c.disclosure_id=d.id "
            "JOIN company_population p ON p.code=c.code WHERE d.source='cninfo' AND d.source_key=? "
            "AND d.record_hash=? AND d.body_checked=0 GROUP BY d.id",
            (identity.source_key, identity.record_hash),
        ).fetchone()
        if document is not None:
            selected[identity.source_key] = DecodeCompanyDisclosure(document)
    return tuple(selected.values())


def _recover_capacity_documents(
    path: Path, directory: Path, budget_seconds: float, sqlite_policy: SqliteIoPolicy,
) -> dict[str, object]:
    RequireIsolatedReplay(directory, (TOOL_ROOT, path.parent))
    if not directory.is_absolute() or any(item.is_symlink() for item in (directory, *directory.parents)):
        raise ValueError("company_document_recovery_path_invalid")
    if directory.exists() and (not directory.is_dir() or any(directory.iterdir())):
        raise ValueError("company_document_recovery_new_directory_required")
    deadline = time.monotonic() + budget_seconds
    cancelled = lambda: time.monotonic() >= deadline
    options = SqliteConnectionOptions(read_only=True, timeout_seconds=3)
    with SqliteSession(path, options, sqlite_policy=sqlite_policy, cancel_requested=cancelled) as connection:
        pending = _capacity_samples(connection)
    samples = pending[:2]
    directory.mkdir(parents=True, exist_ok=True)
    configuration = LoadCompanySyncConfiguration(TOOL_ROOT)
    capability = DetectCompanyOcr(configuration.document_ocr_languages)
    text = DetectCompanyText()
    policies = BuildCompanyDocumentPolicies(configuration, capability.identity, text_identity=text.identity)
    raw = CompanyRawFiles(
        directory / "inputs",
        capacity=configuration.raw_capacity_bytes,
        analysis_reserve=configuration.raw_analysis_reserve_bytes,
        free_reserve=configuration.temporary_disk_reserve_bytes,
        maximum_document_bytes=configuration.extended_document_limits.max_bytes,
    )
    engine = CompanyOcrEngine(
        capability,
        timeout_seconds=configuration.document_ocr_timeout_seconds,
        page_timeout_seconds=configuration.document_ocr_page_timeout_seconds,
        max_pages=configuration.document_ocr_max_pages,
        dpi=configuration.document_ocr_dpi,
        cancel_requested=cancelled,
        retry_each_page=True,
    )
    observations: list[dict[str, object]] = []
    if samples:
        with OpenExternalHttp() as session:
            source = CninfoMarketDisclosureSource(
                session,
                monotonic=time.monotonic,
                sleeper=time.sleep,
                timeout_seconds=configuration.source_timeout_seconds,
                document_download_timeout_seconds=configuration.document_download_timeout_seconds,
                document_parse_timeout_seconds=configuration.document_parse_timeout_seconds,
                request_interval_seconds=configuration.request_interval_seconds,
                document_limits=configuration.document_limits,
                extended_document_limits=configuration.extended_document_limits,
                document_policies=policies,
                text_capability=text,
                ocr_engine=engine,
                cancel_requested=cancelled,
                raw_files=raw,
                text_reserve_bytes=configuration.text_task_reserve_bytes,
                ocr_reserve_bytes=configuration.ocr_task_reserve_bytes,
            )
            for item in samples:
                observation: dict[str, object] = {"record": item.identity, "variant": "isolated_recovery", "passed": False}
                try:
                    source.download_document(item)
                    try:
                        result = source.extract_document(item)
                    except CompanyDocumentFailure as exc:
                        if exc.reason != "disclosure_document_ocr_required":
                            raise
                        result = source.extract_document(item, route="ocr")
                    observation.update(passed=True, text_extracted=True, characters=len(result.text),
                                       payload_hash=result.payload_hash.hex())
                except (OSError, RuntimeError, ValueError) as exc:
                    observation["failure"] = CompanyFailureCode(exc)
                observations.append(observation)
                if cancelled():
                    break
    complete = bool(samples) and len(observations) == len(samples) and len(pending) <= len(samples)
    passed = complete and all(row["passed"] for row in observations)
    report: dict[str, object] = {
        "schema_version": "company-resume", "status": "passed" if passed else "degraded",
        "summary": {"document_download_audit": {
            "read_only": True, "active_writes": False, "pending_records": len(pending), "sample_size": len(samples),
            "complete": complete, "production_passed": passed, "observations": observations,
        }},
        "findings": [] if passed else ["company_document_recovery_incomplete"],
    }
    AtomicWriteJson(directory / "document-recovery.json", report)
    return report
