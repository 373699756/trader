"""Recommendation-owned CNInfo announcement persistence and incremental sync."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, wait
from dataclasses import dataclass
from datetime import date, datetime, timezone
from threading import Lock
from typing import Protocol
from zoneinfo import ZoneInfo

import requests

from trader.infra.failures import AdapterFailureCode, ClassifyAdapterFailure
from trader.infra.workers import SubmitOrReject, WorkerExecutor
from trader.recommendation.application.ports.data_plane_records import (
    DataPlaneUnavailableError,
    RiskEvidenceRecord,
    SourceCursorRecord,
)
from trader.recommendation.domain.market.research import (
    CorporateRiskCategory,
    CorporateRiskFactsFromAnnouncements,
    ResearchAnnouncement,
)

CNINFO_SOURCE = "cninfo"
CNINFO_CURSOR_PREFIX = "cninfo.announcements:"
CNINFO_ANNOUNCEMENT_PREFIX = "cninfo-announcement:"
CNINFO_COMPONENT_PREFIX = "cninfo-risk-component:"
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class CninfoAnnouncementQuery:
    code: str
    page: int
    page_size: int
    observed_at: datetime
    cursor_value: str
    deadline: datetime | None


class CninfoAnnouncementClient(Protocol):
    def fetch_announcements(self, query: CninfoAnnouncementQuery) -> Mapping[str, object]: ...


class CninfoDataPlane(Protocol):
    def save_risk_evidence_recent_records(self, records: Sequence[RiskEvidenceRecord]) -> None: ...

    def save_source_cursor_recent(self, record: SourceCursorRecord) -> None: ...

    def load_source_cursor_recent(self, cursor_name: str) -> SourceCursorRecord | None: ...

    def load_source_cursor_recent_records(
        self, cursor_names: Sequence[str] | None = None
    ) -> tuple[SourceCursorRecord, ...]: ...


@dataclass(frozen=True)
class CninfoAnnouncementRow:
    code: str
    announcement_id: str
    title: str
    published_at: datetime
    source_payload_hash: str
    id_derived: bool = False


@dataclass(frozen=True)
class CninfoSyncResult:
    code: str
    pages_fetched: int
    saved_announcements: int
    duplicate_rows: int
    invalid_rows: int
    history_complete: bool
    cursor_value: str
    source_errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class _CninfoPersistBatch:
    code: str
    observed_at: datetime
    source_time: datetime
    data_version: str
    announcements: tuple[CninfoAnnouncementRow, ...]
    duplicate_rows: int
    invalid_rows: int
    history_complete: bool


@dataclass(frozen=True)
class _CninfoCursorState:
    value: str = ""
    issuer_history_complete: bool = False


class CninfoAnnouncementIncrementalSync:
    """Persist CNInfo announcements and source cursors without changing runtime routing."""

    def __init__(
        self,
        client: CninfoAnnouncementClient,
        data_plane: CninfoDataPlane,
        *,
        page_size: int = 30,
        max_pages: int = 400,
    ) -> None:
        self._client = client
        self._data_plane = data_plane
        self._page_size = max(1, page_size)
        self._max_pages = max(1, max_pages)

    def sync_code(
        self,
        code: str,
        observed_at: datetime,
        *,
        deadline: datetime | None = None,
    ) -> CninfoSyncResult:
        _validate_code(code)
        _validate_time(observed_at, "observed_at")
        cursor_name = f"{CNINFO_CURSOR_PREFIX}{code}"
        cursor = self._load_cursor(cursor_name)
        cursor_value = cursor.value if cursor.issuer_history_complete else ""
        rows: list[CninfoAnnouncementRow] = []
        seen: set[str] = set()
        duplicate_rows = 0
        invalid_rows = 0
        pages_fetched = 0
        history_complete = False

        for page in range(1, self._max_pages + 1):
            payload = self._client.fetch_announcements(
                CninfoAnnouncementQuery(
                    code=code,
                    page=page,
                    page_size=self._page_size,
                    observed_at=observed_at,
                    cursor_value=cursor_value,
                    deadline=deadline,
                )
            )
            pages_fetched += 1
            parsed, page_invalid = ParseCninfoAnnouncementRows(code, payload, observed_at)
            invalid_rows += page_invalid
            for row in parsed:
                if row.announcement_id in seen:
                    duplicate_rows += 1
                    continue
                seen.add(row.announcement_id)
                rows.append(row)
            if not _payload_has_more(payload, page=page, page_size=self._page_size):
                history_complete = True
                break

        history_complete = history_complete and invalid_rows == 0
        data_version = _batch_version(rows, cursor_value, observed_at)
        saved = 0
        source_errors: list[str] = []
        records = tuple(
            RiskEvidenceRecord(
                code=code,
                observed_at=observed_at,
                source=CNINFO_SOURCE,
                source_time=row.published_at,
                data_version=data_version,
                evidence_id=f"{CNINFO_ANNOUNCEMENT_PREFIX}{row.announcement_id}",
                payload={
                    "announcement_id": row.announcement_id,
                    "title": row.title,
                    "published_at": row.published_at.isoformat(),
                    "source_payload_hash": row.source_payload_hash,
                    "id_derived": row.id_derived,
                    "exchange_cross_check_status": "pending",
                },
            )
            for row in rows
        )
        if records:
            try:
                self._data_plane.save_risk_evidence_recent_records(records)
                saved = len(records)
            except DataPlaneUnavailableError as exc:
                source_errors.append(f"cninfo_persist_unavailable:{type(exc).__name__}")

        next_cursor = _next_cursor(cursor_value, rows, observed_at)
        if not source_errors:
            batch = _CninfoPersistBatch(
                code=code,
                observed_at=observed_at,
                source_time=_latest_source_time(rows, observed_at),
                data_version=data_version,
                announcements=tuple(rows),
                duplicate_rows=duplicate_rows,
                invalid_rows=invalid_rows,
                history_complete=history_complete,
            )
            if not cursor_value:
                source_errors.extend(_save_component_statuses(self._data_plane, batch))
            if not source_errors:
                source_errors.extend(
                    _save_cursor(
                        self._data_plane,
                        batch,
                        cursor_name=cursor_name,
                        cursor_value=next_cursor,
                    )
                )

        return CninfoSyncResult(
            code=code,
            pages_fetched=pages_fetched,
            saved_announcements=saved,
            duplicate_rows=duplicate_rows,
            invalid_rows=invalid_rows,
            history_complete=history_complete,
            cursor_value=next_cursor,
            source_errors=tuple(source_errors),
        )

    def _load_cursor(self, cursor_name: str) -> _CninfoCursorState:
        try:
            cursor = self._data_plane.load_source_cursor_recent(cursor_name)
        except DataPlaneUnavailableError:
            return _CninfoCursorState()
        if cursor is None:
            return _CninfoCursorState()
        payload = cursor.payload
        issuer_history_complete = (
            bool(payload.get("issuer_history_complete")) if isinstance(payload, Mapping) else False
        )
        return _CninfoCursorState(cursor.cursor_value, issuer_history_complete)


CninfoPostFunction = Callable[..., requests.Response]


class CninfoAnnouncementHttpClient:
    """Fetch issuer announcements from CNInfo's public web query endpoints."""

    _SEARCH_URL = "https://www.cninfo.com.cn/new/information/topSearch/query"
    _ANNOUNCEMENT_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"

    def __init__(
        self,
        *,
        post: CninfoPostFunction,
        timeout_seconds: float,
        wall_clock: Callable[[], datetime],
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if timeout_seconds <= 0.0:
            raise ValueError("CNInfo timeout must be positive")
        self._post = post
        self._timeout_seconds = timeout_seconds
        self._wall_clock = wall_clock
        self._sleeper = sleeper
        self._organizations: dict[str, str] = {}
        self._request_lock = Lock()

    def fetch_announcements(self, query: CninfoAnnouncementQuery) -> Mapping[str, object]:
        _validate_code(query.code)
        _validate_time(query.observed_at, "observed_at")
        if query.page < 1 or query.page_size < 1:
            raise ValueError("CNInfo page coordinates must be positive")
        organization = self._organization_id(query.code, query.deadline)
        column, plate = _cninfo_market_scope(query.code)
        start_date = _cursor_start_date(query.cursor_value)
        payload = self._post_json(
            self._ANNOUNCEMENT_URL,
            data={
                "pageNum": str(query.page),
                "pageSize": str(min(query.page_size, 30)),
                "column": column,
                "tabName": "fulltext",
                "plate": plate,
                "stock": f"{query.code},{organization}",
                "searchkey": "",
                "secid": "",
                "category": "",
                "trade": "",
                "seDate": f"{start_date.isoformat()}~{query.observed_at.date().isoformat()}",
                "sortName": "time",
                "sortType": "desc",
                "isHLtitle": "true",
            },
            deadline=query.deadline,
        )
        if not isinstance(payload, Mapping):
            raise TypeError("CNInfo announcement response schema is invalid")
        return payload

    def _organization_id(self, code: str, deadline: datetime | None) -> str:
        cached = self._organizations.get(code)
        if cached is not None:
            return cached
        payload = self._post_json(
            self._SEARCH_URL,
            data={"keyWord": code, "maxNum": "10"},
            deadline=deadline,
        )
        if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes)):
            raise TypeError("CNInfo security search response schema is invalid")
        matches = tuple(
            item
            for item in payload
            if isinstance(item, Mapping)
            and str(item.get("code") or "") == code
            and str(item.get("category") or "") == "A股"
            and str(item.get("orgId") or "").strip()
        )
        if not matches:
            raise RuntimeError("no_data")
        organization = str(matches[0]["orgId"]).strip()
        self._organizations[code] = organization
        return organization

    def _post_json(
        self,
        url: str,
        *,
        data: Mapping[str, str],
        deadline: datetime | None,
    ) -> object:
        last_error: BaseException | None = None
        for attempt, delay in enumerate((0.0, 1.0, 3.0)):
            if delay:
                self._sleep_before_retry(delay, deadline)
            try:
                with self._request_lock:
                    timeout = self._remaining_timeout(deadline)
                    response = self._post(
                        url,
                        data=data,
                        headers={
                            "Accept": "application/json, text/plain, */*",
                            "Referer": "https://www.cninfo.com.cn/",
                            "User-Agent": "Mozilla/5.0",
                            "X-Requested-With": "XMLHttpRequest",
                        },
                        timeout=timeout,
                    )
                if response.status_code == 429:
                    raise RuntimeError("http_429")
                response.raise_for_status()
                if deadline is not None and self._wall_clock() >= deadline:
                    raise TimeoutError("CNInfo response completed after deadline")
                return response.json()
            except (requests.ConnectionError, requests.Timeout, requests.HTTPError, RuntimeError) as exc:
                last_error = exc
                retryable_http = not isinstance(exc, requests.HTTPError) or (
                    exc.response is not None and exc.response.status_code >= 500
                )
                if not retryable_http or attempt == 2:
                    raise
        assert last_error is not None
        raise last_error

    def _remaining_timeout(self, deadline: datetime | None) -> float:
        if deadline is None:
            return self._timeout_seconds
        remaining = (deadline - self._wall_clock()).total_seconds()
        if remaining <= 0.0:
            raise TimeoutError("CNInfo request deadline exceeded")
        return min(self._timeout_seconds, remaining)

    def _sleep_before_retry(self, delay: float, deadline: datetime | None) -> None:
        if deadline is not None and (deadline - self._wall_clock()).total_seconds() <= delay:
            raise TimeoutError("CNInfo retry would exceed deadline")
        self._sleeper(delay)


@dataclass(frozen=True)
class CninfoBatchSyncReport:
    changed_codes: tuple[str, ...]
    completed_codes: tuple[str, ...]
    deferred_codes: tuple[str, ...]
    failed_codes: tuple[str, ...]
    pages_fetched: int
    saved_announcements: int
    deadline_reached: bool


class CninfoAnnouncementBatchSynchronizer:
    """Run bounded CNInfo code syncs and expose resumable aggregate progress."""

    def __init__(
        self,
        syncer: CninfoAnnouncementIncrementalSync,
        data_plane: CninfoDataPlane,
        worker_pool: WorkerExecutor,
        *,
        wall_clock: Callable[[], datetime],
    ) -> None:
        self._syncer = syncer
        self._data_plane = data_plane
        self._worker_pool = worker_pool
        self._wall_clock = wall_clock

    def pending_codes(self, codes: Sequence[str], observed_at: datetime) -> tuple[str, ...]:
        try:
            records = self._data_plane.load_source_cursor_recent_records()
        except DataPlaneUnavailableError:
            return tuple(codes)
        completed = {
            record.cursor_name.removeprefix(CNINFO_CURSOR_PREFIX)
            for record in records
            if record.source == CNINFO_SOURCE
            and record.cursor_name.startswith(CNINFO_CURSOR_PREFIX)
            and record.observed_at.date() >= observed_at.date()
            and isinstance(record.payload, Mapping)
            and bool(record.payload.get("issuer_history_complete"))
        }
        return tuple(code for code in codes if code not in completed)

    def sync_report(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        deadline: datetime,
    ) -> CninfoBatchSyncReport:
        futures = self._submit_sync_futures(codes, observed_at, deadline)
        results, failed, pending = self._collect_sync_results(futures, codes, deadline)
        completed_codes = tuple(sorted(code for code, result in results.items() if result.history_complete))
        changed_codes = tuple(sorted(code for code, result in results.items() if result.saved_announcements > 0))
        deferred = tuple(sorted(set(codes) - set(completed_codes) - failed))
        return CninfoBatchSyncReport(
            changed_codes=changed_codes,
            completed_codes=completed_codes,
            deferred_codes=deferred,
            failed_codes=tuple(sorted(failed)),
            pages_fetched=sum(result.pages_fetched for result in results.values()),
            saved_announcements=sum(result.saved_announcements for result in results.values()),
            deadline_reached=bool(pending) or self._wall_clock() >= deadline,
        )

    def _submit_sync_futures(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        deadline: datetime,
    ) -> dict[Future[CninfoSyncResult], str]:
        futures: dict[Future[CninfoSyncResult], str] = {}
        for code in codes:
            if self._wall_clock() >= deadline:
                break
            future = SubmitOrReject(
                self._worker_pool,
                self._syncer.sync_code,
                code,
                observed_at,
                deadline=deadline,
            )
            futures[future] = code
        return futures

    def _collect_sync_results(
        self,
        futures: Mapping[Future[CninfoSyncResult], str],
        codes: Sequence[str],
        deadline: datetime,
    ) -> tuple[dict[str, CninfoSyncResult], set[str], set[Future[CninfoSyncResult]]]:
        pending = set(futures)
        results: dict[str, CninfoSyncResult] = {}
        failed: set[str] = set()
        while pending:
            timeout = max(0.0, (deadline - self._wall_clock()).total_seconds())
            if timeout <= 0.0:
                break
            completed, pending = wait(pending, timeout=timeout, return_when=FIRST_COMPLETED)
            if not completed:
                break
            if self._consume_sync_results(completed, futures, codes, results, failed):
                break
        for future in pending:
            future.cancel()
        return results, failed, pending

    @staticmethod
    def _consume_sync_results(
        completed: set[Future[CninfoSyncResult]],
        futures: Mapping[Future[CninfoSyncResult], str],
        codes: Sequence[str],
        results: dict[str, CninfoSyncResult],
        failed: set[str],
    ) -> bool:
        for future in completed:
            code = futures[future]
            try:
                results[code] = future.result()
            except (OSError, RuntimeError, TypeError, ValueError, requests.RequestException) as exc:
                failure = ClassifyAdapterFailure(exc, provider=CNINFO_SOURCE, operation="announcements")
                if failure.code is AdapterFailureCode.RATE_LIMITED:
                    failed.update(codes)
                    return True
                if not failure.retryable:
                    failed.add(code)
        return False


def ParseCninfoAnnouncementRows(
    code: str,
    payload: Mapping[str, object],
    observed_at: datetime,
) -> tuple[tuple[CninfoAnnouncementRow, ...], int]:
    rows = _extract_rows(payload)
    parsed: list[CninfoAnnouncementRow] = []
    invalid_rows = 0
    for raw in rows:
        title = _clean_text(_first_string(raw, ("announcementTitle", "announcement_title", "title")))
        published_at = _parse_cninfo_time(
            raw.get("announcementTime")
            or raw.get("announcement_time")
            or raw.get("announcementDate")
            or raw.get("published_at")
        )
        if not title or published_at is None or published_at > observed_at:
            invalid_rows += 1
            continue
        raw_id = _first_string(raw, ("announcementId", "announcement_id", "id", "art_code"))
        id_derived = not bool(raw_id.strip())
        source_payload_hash = _mapping_hash(raw)
        announcement_id = raw_id.strip() or f"derived:{code}:{source_payload_hash}"
        parsed.append(
            CninfoAnnouncementRow(
                code=code,
                announcement_id=announcement_id,
                title=title[:240],
                published_at=published_at,
                source_payload_hash=source_payload_hash,
                id_derived=id_derived,
            )
        )
    parsed.sort(key=lambda item: (item.published_at, item.announcement_id))
    return tuple(parsed), invalid_rows


def _extract_rows(payload: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    candidates = (
        payload.get("announcements"),
        payload.get("classifiedAnnouncements"),
        payload.get("rows"),
        payload.get("data"),
    )
    flattened: list[Mapping[str, object]] = []
    for candidate in candidates:
        if isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes)):
            for item in candidate:
                if isinstance(item, Mapping):
                    flattened.append(item)
                elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
                    flattened.extend(inner for inner in item if isinstance(inner, Mapping))
            if flattened:
                return tuple(flattened)
        if isinstance(candidate, Mapping):
            nested = _extract_rows(candidate)
            if nested:
                return nested
    return ()


def _payload_has_more(payload: Mapping[str, object], *, page: int, page_size: int) -> bool:
    for key in ("hasMore", "has_more"):
        value = payload.get(key)
        if isinstance(value, bool):
            return value
    total = _int_value(payload.get("totalRecordNum") or payload.get("total") or payload.get("total_hits"))
    return total is not None and page * page_size < total


def _save_component_statuses(
    data_plane: CninfoDataPlane,
    batch: _CninfoPersistBatch,
) -> tuple[str, ...]:
    statuses = _component_statuses(batch.announcements, history_complete=batch.history_complete)
    records = tuple(
        RiskEvidenceRecord(
            code=batch.code,
            observed_at=batch.observed_at,
            source=CNINFO_SOURCE,
            source_time=batch.source_time,
            data_version=batch.data_version,
            evidence_id=f"{CNINFO_COMPONENT_PREFIX}{component}",
            payload={
                "status": status,
                "history_complete": batch.history_complete,
                "announcement_count": len(batch.announcements),
                "source": CNINFO_SOURCE,
            },
        )
        for component, status in statuses.items()
    )
    try:
        data_plane.save_risk_evidence_recent_records(records)
    except DataPlaneUnavailableError as exc:
        return (f"cninfo_component_persist_unavailable:{type(exc).__name__}",)
    return ()


def _component_statuses(
    announcements: tuple[CninfoAnnouncementRow, ...],
    *,
    history_complete: bool,
) -> dict[str, str]:
    research_announcements = tuple(
        ResearchAnnouncement(
            title=item.title,
            published_at=item.published_at,
            announcement_id=f"{CNINFO_ANNOUNCEMENT_PREFIX}{item.announcement_id}",
            source="issuer_disclosure",
        )
        for item in announcements
    )
    categories = {fact.category for fact in CorporateRiskFactsFromAnnouncements(research_announcements)}
    announcement_clear = "known_clear" if history_complete else "stale"
    regulatory_clear = "stale"
    return {
        "announcements": "known_risk" if categories else announcement_clear,
        "penalty": "known_risk" if categories & _PENALTY_CATEGORIES else regulatory_clear,
        "lawsuit_restructuring": "known_risk"
        if CorporateRiskCategory.MAJOR_SHAREHOLDER_REDUCTION in categories
        else regulatory_clear,
        "forced_delisting": "known_risk" if CorporateRiskCategory.FORCED_DELISTING in categories else regulatory_clear,
        "suspension": "unknown",
    }


def _save_cursor(
    data_plane: CninfoDataPlane,
    batch: _CninfoPersistBatch,
    *,
    cursor_name: str,
    cursor_value: str,
) -> tuple[str, ...]:
    try:
        data_plane.save_source_cursor_recent(
            SourceCursorRecord(
                cursor_name=cursor_name,
                observed_at=batch.observed_at,
                source=CNINFO_SOURCE,
                source_time=batch.source_time,
                data_version=batch.data_version,
                cursor_value=cursor_value,
                payload={
                    "history_complete": False,
                    "issuer_history_complete": batch.history_complete,
                    "last_announcement_id": batch.announcements[-1].announcement_id if batch.announcements else "",
                    "saved_rows": len(batch.announcements),
                    "duplicate_rows": batch.duplicate_rows,
                    "invalid_rows": batch.invalid_rows,
                    "exchange_cross_check_status": "pending",
                },
            )
        )
    except DataPlaneUnavailableError as exc:
        return (f"cninfo_cursor_persist_unavailable:{type(exc).__name__}",)
    return ()


def _next_cursor(previous: str, rows: list[CninfoAnnouncementRow], observed_at: datetime) -> str:
    if rows:
        latest = max(rows, key=lambda item: (item.published_at, item.announcement_id))
        return f"{latest.published_at.isoformat()}|{latest.announcement_id}"
    return previous or f"empty:{observed_at.date().isoformat()}"


def _cursor_start_date(cursor_value: str) -> date:
    raw = cursor_value.partition("|")[0]
    if raw.startswith("empty:"):
        raw = raw.removeprefix("empty:")
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        return date(1990, 1, 1)


def _cninfo_market_scope(code: str) -> tuple[str, str]:
    if code.startswith(("4", "8", "92")):
        return "third", "bj"
    if code.startswith(("5", "6", "9")):
        return "sse", "sh"
    return "szse", "sz"


def _latest_source_time(rows: list[CninfoAnnouncementRow], observed_at: datetime) -> datetime:
    if not rows:
        return observed_at
    return max(row.published_at for row in rows)


def _parse_cninfo_time(value: object) -> datetime | None:
    if isinstance(value, (int, float)):
        timestamp = float(value) / 1000.0 if value > 10_000_000_000 else float(value)
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(SHANGHAI_TZ)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("/", "-")
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    parsed_value: datetime | None
    try:
        parsed_value = datetime.fromisoformat(text)
    except ValueError:
        parsed_value = None
        for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                parsed_value = datetime.strptime(text, pattern)
                break
            except ValueError:
                continue
        if parsed_value is None:
            return None
    if parsed_value.tzinfo is None or parsed_value.utcoffset() is None:
        return parsed_value.replace(tzinfo=SHANGHAI_TZ)
    return parsed_value


def _first_string(row: Mapping[str, object], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float)):
            return str(value)
    return ""


def _clean_text(value: str) -> str:
    return " ".join(value.split())


def _int_value(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _mapping_hash(row: Mapping[str, object]) -> str:
    return hashlib.sha256(repr(sorted((str(key), str(value)) for key, value in row.items())).encode()).hexdigest()


def _batch_version(rows: list[CninfoAnnouncementRow], cursor_value: str, observed_at: datetime) -> str:
    return f"cninfo-announcements:{observed_at.isoformat()}:{cursor_value}"


def _validate_code(code: str) -> None:
    if len(code) != 6 or not code.isdigit():
        raise ValueError("CNInfo code must be six digits")


def _validate_time(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


_PENALTY_CATEGORIES = frozenset(
    {
        CorporateRiskCategory.FINANCIAL_FRAUD,
        CorporateRiskCategory.OFFICIAL_INVESTIGATION,
        CorporateRiskCategory.MAJOR_ILLEGAL,
        CorporateRiskCategory.FUND_OCCUPATION,
        CorporateRiskCategory.ILLEGAL_GUARANTEE,
    }
)


__all__ = [
    "CNINFO_ANNOUNCEMENT_PREFIX",
    "CNINFO_COMPONENT_PREFIX",
    "CNINFO_CURSOR_PREFIX",
    "CNINFO_SOURCE",
    "CninfoAnnouncementBatchSynchronizer",
    "CninfoAnnouncementHttpClient",
    "CninfoAnnouncementIncrementalSync",
    "CninfoAnnouncementQuery",
    "CninfoAnnouncementRow",
    "CninfoBatchSyncReport",
    "CninfoSyncResult",
    "ParseCninfoAnnouncementRows",
]
