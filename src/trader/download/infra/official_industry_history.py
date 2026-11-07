"""Prepare dated CAPCO industry evidence from cached official release originals."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.historical_industry import HistoricalIndustryPreparation
from trader.download.domain.history_sync import HistoryIndustryReadiness, HistorySyncProgress, HistorySyncProgressPort
from trader.download.domain.industry_evidence import OfficialIndustryEvidence
from trader.download.infra.history_industry_preparation import PrepareHistoricalIndustries
from trader.download.infra.industry_query_index import IndustrySourceDocument, SQLiteIndustryQueryIndex
from trader.download.infra.official_industry_pdf import OfficialIndustryRow, OfficialIndustrySnapshot, ParseOfficialIndustryPdf
from trader.download.infra.official_industry_release import (
    OfficialIndustryReleaseLink,
    ParseOfficialIndustryBundleUrl,
    ParseOfficialIndustryRelease,
    ParseOfficialIndustryReleaseLinks,
)
from trader.infra.http_contracts import HttpCancelled, HttpError, HttpTransport

_ROOT_URL = "https://www.capco.org.cn/"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_CATALOG_AGE = timedelta(days=7)
_TEXT_BYTES = 2 * 1024 * 1024
_BUNDLE_BYTES = 8 * 1024 * 1024
_PDF_BYTES = 32 * 1024 * 1024
_HASH = re.compile(r"[0-9a-f]{64}")
_ROW_FIELDS = frozenset((
    "code", "company_name", "native_category", "native_name", "page", "section_code", "section_name",
    "subcategory_code", "subcategory_name",
))


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("official_industry_time_invalid")
    return value.astimezone(_SHANGHAI)


def _source_key(kind: str, url: str) -> str:
    return f"capco:{kind}:{hashlib.sha256(url.encode('utf-8')).hexdigest()}"


def _failure_reason(exc: BaseException) -> str:
    reason = str(exc)
    if (
        re.fullmatch(r"[a-z][a-z0-9_]{0,159}", reason) is not None
        and reason.startswith(("official_industry_", "industry_source_", "industry_evidence_", "http_"))
    ):
        return reason
    return type(exc).__name__


def _text(value: object, maximum: int, *, empty: bool = False) -> str:
    if (
        not isinstance(value, str) or len(value) > maximum or value != value.strip()
        or not empty and not value
    ):
        raise ValueError("official_industry_parsed_row_invalid")
    return value


def _decode_snapshot(
    content: bytes,
    pdf_hash: str,
    period_end: date,
    parser_hash: str,
    checkpoint: Callable[[], None],
) -> OfficialIndustrySnapshot:
    checkpoint()
    if not content or len(content) > _PDF_BYTES:
        raise ValueError("official_industry_parsed_cache_size_invalid")
    try:
        value: object = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("official_industry_parsed_cache_invalid") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"pdf_sha256", "period_end", "parser_implementation_sha256", "page_count", "rows"}
        or value["pdf_sha256"] != pdf_hash or _HASH.fullmatch(pdf_hash) is None
        or value["period_end"] != period_end.isoformat()
        or value["parser_implementation_sha256"] != parser_hash or _HASH.fullmatch(parser_hash) is None
        or type(value["page_count"]) is not int or not 1 <= value["page_count"] <= 300
        or not isinstance(value["rows"], list) or not 1 <= len(value["rows"]) <= 20_000
    ):
        raise ValueError("official_industry_parsed_cache_invalid")
    page_count = value["page_count"]
    rows: list[OfficialIndustryRow] = []
    for raw in value["rows"]:
        checkpoint()
        if not isinstance(raw, dict) or set(raw) != _ROW_FIELDS:
            raise ValueError("official_industry_parsed_row_invalid")
        code = _text(raw["code"], 6)
        category = _text(raw["native_category"], 3)
        section = _text(raw["section_code"], 1, empty=True)
        subcategory = _text(raw["subcategory_code"], 2, empty=True)
        subcategory_name = _text(raw["subcategory_name"], 100, empty=True)
        page = raw["page"]
        if type(page) is not int:
            raise ValueError("official_industry_parsed_row_invalid")
        rows.append(OfficialIndustryRow(
            code, _text(raw["company_name"], 40), category, _text(raw["native_name"], 100), page,
            section, _text(raw["section_name"], 100, empty=True), subcategory, subcategory_name,
        ))
    return OfficialIndustrySnapshot(tuple(rows), page_count)


def _encode_snapshot(snapshot: OfficialIndustrySnapshot, pdf_hash: str, period_end: date, parser_hash: str) -> bytes:
    payload = {
        "pdf_sha256": pdf_hash,
        "period_end": period_end.isoformat(),
        "parser_implementation_sha256": parser_hash,
        "page_count": snapshot.page_count,
        "rows": [{
            "code": row.code, "company_name": row.company_name, "native_category": row.native_category,
            "native_name": row.native_name, "page": row.page, "section_code": row.section_code,
            "section_name": row.section_name, "subcategory_code": row.subcategory_code,
            "subcategory_name": row.subcategory_name,
        } for row in snapshot.rows],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class OfficialIndustryHistory:
    index: SQLiteIndustryQueryIndex
    transport: HttpTransport
    clock: Callable[[], datetime]
    monotonic: Callable[[], float]
    request_timeout_seconds: float = 45.0
    budget_seconds: float = 180.0
    cancel_requested: Callable[[], bool] = lambda: False

    def __post_init__(self) -> None:
        if (
            type(self.request_timeout_seconds) not in (int, float)
            or not math.isfinite(self.request_timeout_seconds)
            or not 0 < self.request_timeout_seconds <= 45
            or type(self.budget_seconds) not in (int, float)
            or not math.isfinite(self.budget_seconds)
            or not 0 < self.budget_seconds <= 180
        ):
            raise ValueError("official_industry_budget_invalid")

    def prepare(
        self,
        universe: tuple[BaoStockSecurity, ...],
        calendar_dates: tuple[date, ...],
        observed_at: datetime,
        *,
        progress: HistorySyncProgressPort | None = None,
    ) -> HistoricalIndustryPreparation:
        codes = tuple(item.code for item in universe)
        observed_at = _aware(observed_at)
        if (
            not universe or len(universe) > 12_000 or codes != tuple(sorted(set(codes)))
            or not calendar_dates or len(calendar_dates) > 20_000
            or any(type(day) is not date for day in calendar_dates)
            or calendar_dates != tuple(sorted(set(calendar_dates)))
            or calendar_dates[-1] > observed_at.date()
        ):
            raise ValueError("official_industry_population_invalid")
        started = self.monotonic()
        if not math.isfinite(started):
            raise ValueError("official_industry_monotonic_invalid")
        operation = _Preparation(self, universe, calendar_dates, observed_at, started + self.budget_seconds)
        if progress is not None:
            progress.publish(HistorySyncProgress(
                "supplier_industry", "started", 0, len(universe), "capco", data_source="中上协",
            ))
        failure: str | None = None
        try:
            operation.download()
        except HttpCancelled:
            raise
        except (HttpError, OSError, ValueError) as exc:
            # Originals already committed remain available for a later resume.
            failure = "official_industry_catalog_failed:" + _failure_reason(exc)
        try:
            operation.check()
            result = PrepareHistoricalIndustries(
                self.index, universe, calendar_dates, operation.observed_at,
                cancel_requested=operation.cancelled, progress=progress,
            )
            operation.check()
        except TimeoutError:
            return HistoricalIndustryPreparation((), HistoryIndustryReadiness(
                len(universe), 0, 0, source="capco", failure_category="official_industry_budget_exhausted",
            ))
        failures = (*operation.failures, *((failure,) if failure is not None else ()))
        if failures:
            result = replace(result, readiness=replace(result.readiness, failure_category=";".join(failures)))
        return result


@dataclass
class _Preparation:
    owner: OfficialIndustryHistory
    universe: tuple[BaoStockSecurity, ...]
    calendar: tuple[date, ...]
    observed_at: datetime
    deadline: float
    fetched_keys: set[str] = field(default_factory=set)
    failures: list[str] = field(default_factory=list)

    def check(self) -> None:
        if self.owner.cancel_requested():
            raise HttpCancelled("official_industry_cancelled")
        now = self.owner.monotonic()
        if not math.isfinite(now):
            raise ValueError("official_industry_monotonic_invalid")
        if now >= self.deadline:
            raise TimeoutError("official_industry_budget_exhausted")
        self.observed_at = max(self.observed_at, _aware(self.owner.clock()))

    def cancelled(self) -> bool:
        self.check()
        return False

    def document(
        self, url: str, key: str, maximum_bytes: int, *, refresh: bool = False, force: bool = False,
    ) -> IndustrySourceDocument:
        self.check()
        existing = self.owner.index.read_source_document(key, self.observed_at)
        if existing is not None:
            if len(existing.content) > maximum_bytes:
                raise ValueError("official_industry_cached_response_too_large")
            if not force and (not refresh or self.observed_at - existing.received_at < _CATALOG_AGE):
                return existing
        remaining = self.deadline - self.owner.monotonic()
        if remaining <= 0:
            raise TimeoutError("official_industry_budget_exhausted")
        if key in self.fetched_keys:
            raise ValueError("official_industry_repeat_request_blocked")
        self.fetched_keys.add(key)
        response = self.owner.transport.get(
            url,
            timeout=min(self.owner.request_timeout_seconds, remaining),
            deadline=self.deadline,
            max_response_bytes=maximum_bytes,
            cancel_requested=self.cancelled,
        )
        response.raise_for_status()
        self.check()
        if not response.content or len(response.content) > maximum_bytes:
            raise ValueError("official_industry_response_size_invalid")
        start = min(self.calendar[0], existing.requested_from) if existing is not None else self.calendar[0]
        end = max(self.observed_at.date(), existing.requested_to) if existing is not None else self.observed_at.date()
        document = IndustrySourceDocument(key, start, end, self.observed_at, response.content)
        self.owner.index.save_source_documents((document,))
        return document

    def download(self) -> None:
        self.check()
        root = self.document(_ROOT_URL, "capco:catalog:root", _TEXT_BYTES, refresh=True)
        bundle_url = ParseOfficialIndustryBundleUrl(root.content.decode("utf-8"))
        bundle = self.document(bundle_url, _source_key("catalog", bundle_url), _BUNDLE_BYTES, refresh=True)
        links = ParseOfficialIndustryReleaseLinks(bundle_url, bundle.content.decode("utf-8"))
        available = tuple(sorted(
            (link for link in links if link.disclosed_on + timedelta(days=1) <= self.observed_at.date()),
            key=lambda item: item.period_end,
        ))
        if tuple(item.disclosed_on for item in available) != tuple(sorted({item.disclosed_on for item in available})):
            raise ValueError("official_industry_release_time_conflict")
        for position, link in enumerate(available):
            self.check()
            start = link.disclosed_on + timedelta(days=1)
            end = (
                available[position + 1].disclosed_on + timedelta(days=1)
                if position + 1 < len(available) else self.observed_at.date() + timedelta(days=1)
            )
            if start > self.calendar[-1] or end <= self.calendar[0]:
                continue
            try:
                self.release(link, start, end)
            except HttpCancelled:
                raise
            except (HttpError, OSError, ValueError) as exc:
                self.check()
                self.failures.append(
                    f"official_industry_release_failed:{link.period_end.isoformat()}:{_failure_reason(exc)}",
                )

    def release(self, link: OfficialIndustryReleaseLink, start: date, end: date) -> None:
        announcement_key = _source_key("announcement", link.announcement_url)
        announcement = self.document(link.announcement_url, announcement_key, _TEXT_BYTES, refresh=True)
        release = ParseOfficialIndustryRelease(link.announcement_url, announcement.content.decode("utf-8"))
        if (
            (release.disclosed_on != link.disclosed_on or release.period_end != link.period_end)
            and announcement_key not in self.fetched_keys
        ):
            announcement = self.document(link.announcement_url, announcement_key, _TEXT_BYTES, force=True)
            release = ParseOfficialIndustryRelease(link.announcement_url, announcement.content.decode("utf-8"))
        if release.disclosed_on != link.disclosed_on or release.period_end != link.period_end:
            raise ValueError("official_industry_release_identity_conflict")
        self.require_revision_boundary(announcement, release.disclosed_on)
        attachment = self.document(
            release.attachment_url, _source_key("pdf", release.attachment_url), _PDF_BYTES, refresh=True,
        )
        self.require_revision_boundary(attachment, release.disclosed_on)
        scope = frozenset(item.code for item in self.universe)
        snapshot = self.snapshot(attachment, release.period_end)
        pending: list[OfficialIndustryEvidence] = []
        batch_rows = min(1000, self.owner.index.sqlite_policy.transaction_batch_rows)
        received_at = max(announcement.received_at, attachment.received_at)
        for row in snapshot.rows:
            self.check()
            if row.code not in scope:
                continue
            pending.append(OfficialIndustryEvidence(
                code=row.code,
                source_url=release.attachment_url,
                document_sha256=attachment.content_hash,
                disclosed_on=release.disclosed_on,
                effective_from=start,
                effective_to=end,
                received_at=received_at,
                page=row.page,
                excerpt=f"{row.code} {row.company_name} {row.native_category} {row.native_name}",
                native_category=row.native_category,
                native_name=row.native_name,
                announcement_url=release.announcement_url,
                announcement_sha256=announcement.content_hash,
            ))
            if len(pending) >= batch_rows:
                self.owner.index.import_evidence(tuple(pending))
                pending.clear()
        if pending:
            self.check()
            self.owner.index.import_evidence(tuple(pending))

    def require_revision_boundary(self, document: IndustrySourceDocument, disclosed_on: date) -> None:
        self.check()
        previous = self.owner.index.read_previous_source_document(
            document.request_key, self.observed_at, differing_from=document.content_hash,
        )
        self.check()
        if previous is not None and disclosed_on <= previous.received_at.date():
            raise ValueError("official_industry_source_revision_unqualified")

    def snapshot(self, attachment: IndustrySourceDocument, period_end: date) -> OfficialIndustrySnapshot:
        self.check()
        parser_hash = hashlib.sha256(Path(__file__).with_name("official_industry_pdf.py").read_bytes()).hexdigest()
        pdf_hash = attachment.content_hash
        identity = hashlib.sha256(bytes.fromhex(pdf_hash) + bytes.fromhex(parser_hash)).hexdigest()
        key = "capco:parsed:" + identity
        cached = self.owner.index.read_source_document(key, self.observed_at)
        if cached is not None:
            return _decode_snapshot(cached.content, pdf_hash, period_end, parser_hash, self.check)
        snapshot = ParseOfficialIndustryPdf(attachment.content, period_end, cancel_requested=self.cancelled)
        self.check()
        content = _encode_snapshot(snapshot, pdf_hash, period_end, parser_hash)
        decoded = _decode_snapshot(content, pdf_hash, period_end, parser_hash, self.check)
        self.owner.index.save_source_documents((IndustrySourceDocument(
            key, self.calendar[0], self.observed_at.date(), self.observed_at, content,
        ),))
        self.check()
        return decoded


__all__ = ["OfficialIndustryHistory"]
