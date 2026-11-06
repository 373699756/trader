"""Collect immutable official source candidates outside product data; never publish industry facts."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import monotonic
from zoneinfo import ZoneInfo

from scripts.core.paths import ExternalPath
from trader.download.infra.official_industry_pdf import ParseOfficialIndustryPdf
from trader.download.infra.official_industry_release import ParseOfficialIndustryRelease, ValidateOfficialIndustryUrl
from trader.infra.atomic_files.json import AtomicWriteJson, SyncDirectory
from trader.infra.http_contracts import HttpCancelled, HttpError, HttpStatusError, HttpTimeout, HttpTransport
from trader.infra.process_lock import ProcessLock

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_MAX_DOCUMENT_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class OfficialIndustryAuditRequest:
    announcement_urls: tuple[str, ...]
    work_root: Path
    timeout_seconds: float = 15.0
    budget_seconds: float = 180.0


@dataclass(frozen=True)
class _Document:
    content: bytes
    sha256: str
    received_at: datetime
    reused: bool


def _read(path: Path, limit: int) -> bytes:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError("official_industry_cache_invalid")
    with path.open("rb") as handle:
        content = handle.read(limit + 1)
    if len(content) > limit:
        raise ValueError("official_industry_cache_invalid")
    return content


def _save_document(path: Path, content: bytes) -> None:
    if path.exists() or path.is_symlink():
        if _read(path, _MAX_DOCUMENT_BYTES) != content:
            raise ValueError("official_industry_cache_invalid")
        return
    descriptor, temporary = tempfile.mkstemp(prefix=".document-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        SyncDirectory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


@dataclass
class _DocumentCache:
    root: Path
    transport: HttpTransport
    timeout_seconds: float
    deadline: float
    cancel_requested: Callable[[], bool]
    clock: Callable[[], datetime]
    monotonic: Callable[[], float]
    network_requests: int = 0
    cache_hits: int = 0

    def check(self) -> None:
        if self.cancel_requested():
            raise HttpCancelled("official_industry_cancelled")
        if self.monotonic() >= self.deadline:
            raise HttpTimeout("official_industry_budget_exhausted")

    def get(self, url: str, *, attachment: bool = False) -> _Document:
        self.check()
        ValidateOfficialIndustryUrl(url, attachment=attachment)
        limit = _MAX_DOCUMENT_BYTES if attachment else 2 * 1024 * 1024
        receipt_path = self.root / (hashlib.sha256(url.encode("utf-8")).hexdigest() + ".json")
        if receipt_path.exists() or receipt_path.is_symlink():
            payload: object = json.loads(_read(receipt_path, 4096))
            if (
                not isinstance(payload, dict) or set(payload) != {"url", "sha256", "received_at"}
                or payload["url"] != url or not isinstance(payload["sha256"], str)
                or re.fullmatch(r"[0-9a-f]{64}", payload["sha256"]) is None
                or not isinstance(payload["received_at"], str)
            ):
                raise ValueError("official_industry_cache_invalid")
            received_at = datetime.fromisoformat(payload["received_at"])
            if received_at.utcoffset() is None or received_at > self.clock():
                raise ValueError("official_industry_cache_invalid")
            content = _read(self.root / (payload["sha256"] + ".blob"), limit)
            if hashlib.sha256(content).hexdigest() != payload["sha256"]:
                raise ValueError("official_industry_cache_invalid")
            self.cache_hits += 1
            return _Document(content, payload["sha256"], received_at, True)
        self.network_requests += 1
        response = self.transport.get(
            url,
            timeout=min(self.timeout_seconds, self.deadline - self.monotonic()),
            headers={"User-Agent": "TraderResearch/0.2 (official industry reference audit)"},
            deadline=self.deadline,
            max_response_bytes=limit,
            cancel_requested=self.cancel_requested,
        )
        response.raise_for_status()
        self.check()
        content = response.content
        if not content or len(content) > limit or attachment and not content.startswith(b"%PDF-"):
            raise ValueError("official_industry_document_invalid")
        received_at = self.clock()
        if received_at.utcoffset() is None:
            raise ValueError("official_industry_clock_invalid")
        digest = hashlib.sha256(content).hexdigest()
        _save_document(self.root / (digest + ".blob"), content)
        AtomicWriteJson(receipt_path, {"url": url, "sha256": digest, "received_at": received_at.isoformat()})
        return _Document(content, digest, received_at, False)


def AuditOfficialIndustrySources(
    request: OfficialIndustryAuditRequest,
    transport: HttpTransport,
    *,
    cancel_requested: Callable[[], bool] = lambda: False,
    clock: Callable[[], datetime] = lambda: datetime.now(_SHANGHAI),
    monotonic_clock: Callable[[], float] = monotonic,
) -> dict[str, object]:
    urls = request.announcement_urls
    if not urls or len(urls) > 64 or len(set(urls)) != len(urls):
        raise ValueError("official_industry_announcements_invalid")
    for url in urls:
        ValidateOfficialIndustryUrl(url)
    if (
        not math.isfinite(request.timeout_seconds) or not 1 <= request.timeout_seconds <= 45
        or not math.isfinite(request.budget_seconds) or not 1 <= request.budget_seconds <= 7200
    ):
        raise ValueError("official_industry_budget_invalid")
    root = ExternalPath(request.work_root, "--industry-work-root")
    if root is None:
        raise ValueError("official_industry_work_root_required")
    root.mkdir(parents=True, exist_ok=True)
    if (root / ".official-industry.lock").is_symlink():
        raise ValueError("official_industry_cache_invalid")
    cache = _DocumentCache(
        root,
        transport,
        request.timeout_seconds,
        monotonic_clock() + request.budget_seconds,
        cancel_requested,
        clock,
        monotonic_clock,
    )
    sources: list[dict[str, object]] = []
    with ProcessLock(root / ".official-industry.lock", label="official-industry-audit"):
        for url in urls:
            source: dict[str, object] = {"announcement_url": url}
            try:
                announcement = cache.get(url)
                release = ParseOfficialIndustryRelease(url, announcement.content.decode("utf-8"))
                if release.disclosed_on > announcement.received_at.astimezone(_SHANGHAI).date():
                    raise ValueError("official_industry_future_disclosure")
                source.update(
                    period_end=release.period_end.isoformat(),
                    disclosed_on=release.disclosed_on.isoformat(),
                    disclosure_precision="date",
                    conservative_available_on=release.conservative_available_on.isoformat(),
                    native_classification=release.native_classification,
                    announcement_sha256=announcement.sha256,
                    attachment_url=release.attachment_url,
                )
                document = cache.get(release.attachment_url, attachment=True)
                source.update(attachment_sha256=document.sha256, received_at=document.received_at.isoformat())
                snapshot = ParseOfficialIndustryPdf(
                    document.content,
                    release.period_end,
                    cancel_requested=lambda: _cancel_check(cache),
                )
                cache.check()
                rows_path = root / (document.sha256 + ".rows.json")
                if rows_path.is_symlink():
                    raise ValueError("official_industry_cache_invalid")
                AtomicWriteJson(rows_path, [
                    {"code": row.code, "company_name": row.company_name, "native_category": row.native_category,
                     "native_name": row.native_name, "page": row.page,
                     "section_code": row.section_code, "section_name": row.section_name,
                     "subcategory_code": row.subcategory_code, "subcategory_name": row.subcategory_name}
                    for row in snapshot.rows
                ])
                source.update(
                    status="parsed",
                    attachment_sha256=document.sha256,
                    received_at=document.received_at.isoformat(),
                    attachment_cache_hit=document.reused,
                    row_count=len(snapshot.rows),
                    b_share_rows=sum(row.code.startswith(("200", "900")) for row in snapshot.rows),
                    native_categories=len({row.native_category for row in snapshot.rows}),
                    page_count=snapshot.page_count,
                    rows_file=rows_path.name,
                )
            except (HttpError, ValueError, OSError) as exc:
                source.update(status="failed", failure=type(exc).__name__)
                if isinstance(exc, HttpStatusError):
                    source["http_status"] = exc.status_code
                if re.fullmatch(r"official_industry_[a-z_]+", str(exc)):
                    source["reason"] = str(exc)
                sources.append(source)
                if isinstance(exc, (HttpCancelled, HttpTimeout)) and (
                    cache.cancel_requested() or cache.monotonic() >= cache.deadline
                ):
                    break
                continue
            sources.append(source)
    failures = sum(source["status"] == "failed" for source in sources)
    return {
        "status": "failed" if failures or len(sources) != len(urls) else "degraded",
        "summary": {
            "scope": "official_source_candidates",
            "production_eligible": False,
            "window_coverage_verified": False,
            "source_data_writes": 0,
            "requested_sources": len(urls),
            "parsed_sources": len(sources) - failures,
            "network_requests": cache.network_requests,
            "cache_hits": cache.cache_hits,
            "sources": sources,
        },
    }


def _cancel_check(cache: _DocumentCache) -> bool:
    cache.check()
    return False


__all__ = [
    "AuditOfficialIndustrySources",
    "OfficialIndustryAuditRequest",
]
