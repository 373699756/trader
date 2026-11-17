"""Official metadata replay and one explicitly injected, bounded static transport probe."""

from __future__ import annotations

import math
import time
import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from scripts.download.company.output_paths import RequireIsolatedReplay
from trader.download.domain.company_source_records import CompanyOfficialDirectoryPage, CompanyOfficialDirectoryQuery
from trader.download.infra.company_official_directory import DecodeCompanyOfficialDirectory
from trader.infra.regular_file import OpenRegularFile
from trader.infra.atomic_files import AtomicWriteJson
from trader.infra.http_contracts import HttpAttempt, HttpError, HttpTransport

_CINFO_URL = "https://english.sse.com.cn/markets/dataservice/cpd/cinfo/"


class _ScriptLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "script":
            return
        for name, value in attrs:
            if name == "src" and value is not None and len(value) <= 1024:
                url = urljoin(_CINFO_URL, value)
                if urlsplit(url).scheme == "https" and urlsplit(url).hostname in {"english.sse.com.cn", "www.sse.com.cn"}:
                    self.links.add(url)


def ProbeCompanyOfficialCinfo(
    session: HttpTransport, directory: Path, attempts: list[HttpAttempt], *,
    observed_at: datetime, deadline: float, protected_roots: Sequence[Path],
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, object]:
    """One fixed public URL, no retry, fresh isolated output and actual transport observations."""
    if (observed_at.utcoffset() is None or attempts or not math.isfinite(deadline)
        or not monotonic() < deadline <= monotonic() + 15):
        raise ValueError("company_official_probe_boundary_invalid")
    RequireIsolatedReplay(directory, protected_roots)
    directory.mkdir(parents=True, exist_ok=False)
    AtomicWriteJson(directory / "request-intent.json", {
        "url": _CINFO_URL, "observed_at": observed_at.isoformat(), "timeout_seconds": 15,
        "maximum_bytes": 2 * 1024 * 1024, "attempt_limit": 1, "redirects": False,
    }, indent=2)
    report: dict[str, object] = {
        "scope": "single_official_sse_static_transport", "url": _CINFO_URL,
        "observed_at": observed_at.isoformat(), "status": "transport_failed", "http_status": None,
        "body_sha256": None, "body_bytes": 0, "script_urls": [], "failure": None,
        "source_data_writes": 0, "pdf_requests": 0, "production_adopted": False,
        "field_correctness_verified": False, "full_market_verified": False,
    }
    try:
        response = session.get(_CINFO_URL, timeout=15, deadline=deadline, max_response_bytes=2 * 1024 * 1024,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://english.sse.com.cn/markets/dataservice/cpd/"})
        with (directory / "page.html").open("xb") as stream:
            stream.write(response.content)
        links = _ScriptLinks()
        links.feed(response.content.decode("utf-8", errors="replace"))
        report.update(status="observed_static" if response.status_code == 200 else "http_rejected",
            http_status=response.status_code, body_sha256=hashlib.sha256(response.content).hexdigest(),
            body_bytes=len(response.content), script_urls=sorted(links.links),
            content_type=response.headers.get("Content-Type", ""), server_date=response.headers.get("Date", ""))
    except HttpError as exc:
        report["failure"] = type(exc).__name__
    report.update(transport_attempts=len(attempts), attempt_count_complete=len(attempts) == 1,
        attempts=[{"host": attempt.host, "method": attempt.method, "elapsed_seconds": attempt.elapsed_seconds,
                   "response_bytes": attempt.response_bytes, "status_code": attempt.status_code,
                   "failure": attempt.failure} for attempt in attempts])
    AtomicWriteJson(directory / "transport-receipt.json", report, indent=2)
    return report


@dataclass(frozen=True, slots=True)
class CompanyOfficialDirectorySample:
    path: Path
    query: CompanyOfficialDirectoryQuery
    observed_at: datetime


def ReadCompanyOfficialDirectorySamples(
    samples: tuple[CompanyOfficialDirectorySample, ...], *, budget_seconds: float,
) -> tuple[CompanyOfficialDirectoryPage, ...]:
    """Return the actual typed observations to diagnostics; never produce formal source values."""
    if not 1 <= len(samples) <= 8 or not math.isfinite(budget_seconds) or not 0 < budget_seconds <= 120:
        raise ValueError("company_official_directory_sample_budget_invalid")
    deadline = time.monotonic() + budget_seconds

    def check() -> None:
        if time.monotonic() >= deadline:
            raise TimeoutError("company_official_directory_sample_deadline_exceeded")

    pages: list[CompanyOfficialDirectoryPage] = []
    for sample in samples:
        with OpenRegularFile(sample.path, maximum_bytes=1024 * 1024, check=check) as stream:
            content = stream.read()
        check()
        pages.append(DecodeCompanyOfficialDirectory(content, sample.query, observed_at=sample.observed_at))
    check()
    return tuple(pages)


def AuditCompanyOfficialDirectoryPages(pages: tuple[CompanyOfficialDirectoryPage, ...]) -> dict[str, object]:
    """Project this protocol's real identities, with each missing source qualification explicit."""
    if not 1 <= len(pages) <= 8:
        raise ValueError("company_official_directory_sample_count_invalid")
    return {
        "scope": "selected_official_szse_directory_metadata", "status": "observed_metadata_only",
        "pages": [{
            "start_day": page.query.start_day.isoformat(), "end_day": page.query.end_day.isoformat(),
            "category": page.query.category, "page": page.query.page, "page_size": page.query.page_size,
            "total": page.total, "observations": len(page.observations), "has_more": page.has_more,
            "response_sha256": page.response_hash,
            "items": [{
                "identity": item.identity, "announcement_id": item.announcement_id, "codes": item.codes,
                "title": item.title, "published_at": item.published_at.isoformat(),
                "attachment_path": item.attachment_path, "attachment_format": item.attachment_format,
            } for item in page.observations],
        } for page in pages],
        "unqualified_families": [
            {"family": "financial", "reason": "directory_has_no_eight_field_values_or_report_period_contract"},
            {"family": "latest_per_issuer", "reason": "selected_date_category_pages_have_no_global_latest_marker"},
            {"family": "original_and_revision", "reason": "directory_identities_have_no_issuer_revision_relation_or_original_body"},
            {"family": "persistent_risk", "reason": "periodic_directory_has_no_full_case_population_status_or_resolution_contract"},
        ],
        "source_data_writes": 0, "network_requests": 0, "documents_downloaded": 0,
        "field_correctness_verified": False, "full_market_verified": False, "production_adopted": False,
    }


__all__ = ["CompanyOfficialDirectorySample", "ReadCompanyOfficialDirectorySamples", "AuditCompanyOfficialDirectoryPages", "ProbeCompanyOfficialCinfo"]
