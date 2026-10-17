"""Recommendation-owned CNInfo announcement persistence and incremental sync."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from threading import Lock, local
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

import requests

from trader.recommendation.application.ports.data_plane_records import (
    DataPlaneBusyError,
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
_LEGACY_HISTORY_START = date(1990, 1, 1)
_HISTORY_LOOKBACK_DAYS = 365
_CHECKPOINT_VERSION = 1


@dataclass(frozen=True)
class CninfoAnnouncementQuery:
    code: str
    page: int
    page_size: int
    observed_at: datetime
    cursor_value: str
    deadline: datetime | None
    range_start: date | None = None
    range_end: date | None = None


class CninfoAnnouncementClient(Protocol):
    def fetch_announcements(self, query: CninfoAnnouncementQuery) -> Mapping[str, object]: ...


class CninfoDataPlane(Protocol):
    def probe_writable(self, *, timeout_seconds: float = 8.0) -> None: ...

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
    pages_checkpointed: int = 0
    checkpoint_advanced: bool = False
    resumed: bool = False
    terminal_failure: bool = False
    request_seconds: float = 0.0
    parse_seconds: float = 0.0
    evidence_write_seconds: float = 0.0
    cursor_write_seconds: float = 0.0
    persistence_error: Literal["busy", "unavailable"] | None = None


@dataclass(frozen=True)
class _CninfoCursorState:
    value: str = ""
    issuer_history_complete: bool = False
    observed_at: datetime | None = None
    checkpoint_version: int = 0
    window_days: int = 0
    initial_backfill_complete: bool = False
    range_start: date | None = None
    range_end: date | None = None
    next_page: int = 1
    latest_cursor_value: str = ""
    total_pages_persisted: int = 0
    total_saved_rows: int = 0
    range_duplicate_rows: int = 0
    range_invalid_rows: int = 0
    risk_categories: tuple[CorporateRiskCategory, ...] = ()


@dataclass
class _CninfoSyncState:
    range_start: date
    range_end: date
    next_page: int
    latest_cursor_value: str
    initial_backfill_complete: bool
    total_pages_persisted: int
    total_saved_rows: int
    range_duplicate_rows: int
    range_invalid_rows: int
    risk_categories: set[CorporateRiskCategory]
    checkpoint_observed_at: datetime | None
    resumed: bool


@dataclass(frozen=True)
class _CninfoSyncContext:
    code: str
    observed_at: datetime
    deadline: datetime | None


@dataclass(frozen=True)
class _CninfoFetchedPage:
    page: int
    rows: tuple[CninfoAnnouncementRow, ...]
    duplicate_rows: int
    invalid_rows: int
    has_more: bool


@dataclass
class _CninfoRunCounters:
    pages_fetched: int = 0
    pages_checkpointed: int = 0
    saved_announcements: int = 0
    duplicate_rows: int = 0
    invalid_rows: int = 0
    checkpoint_advanced: bool = False
    request_seconds: float = 0.0
    parse_seconds: float = 0.0
    evidence_write_seconds: float = 0.0
    cursor_write_seconds: float = 0.0
    persistence_error: Literal["busy", "unavailable"] | None = None
    source_errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _CninfoPageOutcome:
    history_complete: bool = False
    terminal_failure: bool = False
    should_stop: bool = False


@dataclass(frozen=True)
class _CninfoComponentBatch:
    code: str
    observed_at: datetime
    source_time: datetime
    data_version: str
    statuses: Mapping[str, str]
    announcement_count: int


class CninfoAnnouncementIncrementalSync:
    """Persist CNInfo announcements and source cursors without changing runtime routing."""

    def __init__(
        self,
        client: CninfoAnnouncementClient,
        data_plane: CninfoDataPlane,
        *,
        page_size: int = 30,
        max_pages: int = 400,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._data_plane = data_plane
        self._page_size = max(1, page_size)
        self._max_pages = max(1, max_pages)
        self._monotonic = monotonic

    def sync_code(
        self,
        code: str,
        observed_at: datetime,
        *,
        deadline: datetime | None = None,
        progress: Callable[[tuple[int, int, int]], None] | None = None,
    ) -> CninfoSyncResult:
        _validate_code(code)
        _validate_time(observed_at, "observed_at")
        context = _CninfoSyncContext(code, observed_at, deadline)
        try:
            cursor = self._load_cursor(f"{CNINFO_CURSOR_PREFIX}{code}")
        except DataPlaneUnavailableError as exc:
            persistence_error: Literal["busy", "unavailable"] = (
                "busy" if isinstance(exc, DataPlaneBusyError) else "unavailable"
            )
            return CninfoSyncResult(
                code=code,
                pages_fetched=0,
                saved_announcements=0,
                duplicate_rows=0,
                invalid_rows=0,
                history_complete=False,
                cursor_value="",
                source_errors=(f"cninfo_cursor_read_{persistence_error}",),
                persistence_error=persistence_error,
            )
        state = _build_sync_state(cursor, observed_at)
        counters = _CninfoRunCounters()
        history_complete = False
        terminal_failure = False

        for _ in range(self._max_pages):
            checkpointed_before = counters.pages_checkpointed
            try:
                outcome = self._sync_next_page(context, state, counters)
            except (TimeoutError, requests.ConnectionError, requests.Timeout) as exc:
                if not counters.checkpoint_advanced:
                    raise
                counters.source_errors.append(f"cninfo_fetch_deferred:{type(exc).__name__}")
                break
            history_complete = outcome.history_complete
            terminal_failure = outcome.terminal_failure
            if progress is not None and counters.pages_checkpointed > checkpointed_before:
                progress((counters.pages_fetched, counters.pages_checkpointed, counters.saved_announcements))
            if outcome.should_stop:
                break

        return CninfoSyncResult(
            code=code,
            pages_fetched=counters.pages_fetched,
            saved_announcements=counters.saved_announcements,
            duplicate_rows=counters.duplicate_rows,
            invalid_rows=counters.invalid_rows,
            history_complete=history_complete,
            cursor_value=state.latest_cursor_value,
            source_errors=tuple(counters.source_errors),
            pages_checkpointed=counters.pages_checkpointed,
            checkpoint_advanced=counters.checkpoint_advanced,
            resumed=state.resumed,
            terminal_failure=terminal_failure,
            request_seconds=counters.request_seconds,
            parse_seconds=counters.parse_seconds,
            evidence_write_seconds=counters.evidence_write_seconds,
            cursor_write_seconds=counters.cursor_write_seconds,
            persistence_error=counters.persistence_error,
        )

    def _sync_next_page(
        self,
        context: _CninfoSyncContext,
        state: _CninfoSyncState,
        counters: _CninfoRunCounters,
    ) -> _CninfoPageOutcome:
        page = self._fetch_page(context, state, counters)
        counters.pages_fetched += 1
        persist_errors = self._persist_page(context, state, counters, page)
        counters.source_errors.extend(persist_errors)
        if persist_errors:
            counters.persistence_error = _persistence_error(persist_errors)
            return _CninfoPageOutcome(should_stop=True)
        history_complete, terminal_failure, completion_errors = _complete_range_if_needed(
            self._data_plane,
            context,
            state,
            page,
        )
        counters.source_errors.extend(completion_errors)
        if completion_errors:
            counters.persistence_error = _persistence_error(completion_errors)
            return _CninfoPageOutcome(should_stop=True)
        if terminal_failure:
            counters.source_errors.append("cninfo_schema_invalid")
        checkpoint_started = self._monotonic()
        try:
            checkpoint_errors = _save_cursor_checkpoint(
                self._data_plane,
                context=context,
                state=state,
                history_complete=history_complete,
            )
        finally:
            counters.cursor_write_seconds += max(0.0, self._monotonic() - checkpoint_started)
        counters.source_errors.extend(checkpoint_errors)
        if checkpoint_errors:
            counters.persistence_error = _persistence_error(checkpoint_errors)
            return _CninfoPageOutcome(should_stop=True)
        counters.pages_checkpointed += 1
        counters.checkpoint_advanced = True
        return _CninfoPageOutcome(
            history_complete=history_complete,
            terminal_failure=terminal_failure,
            should_stop=history_complete or terminal_failure,
        )

    def _fetch_page(
        self, context: _CninfoSyncContext, state: _CninfoSyncState, counters: _CninfoRunCounters
    ) -> _CninfoFetchedPage:
        page = state.next_page
        request_started = self._monotonic()
        try:
            payload = self._client.fetch_announcements(
                CninfoAnnouncementQuery(
                    code=context.code,
                    page=page,
                    page_size=self._page_size,
                    observed_at=context.observed_at,
                    cursor_value=state.latest_cursor_value,
                    deadline=context.deadline,
                    range_start=state.range_start,
                    range_end=state.range_end,
                )
            )
        finally:
            counters.request_seconds += max(0.0, self._monotonic() - request_started)
        parse_started = self._monotonic()
        try:
            parsed, invalid_rows = ParseCninfoAnnouncementRows(context.code, payload, context.observed_at)
            rows, duplicate_rows = _unique_announcement_rows(parsed)
            return _CninfoFetchedPage(
                page=page,
                rows=rows,
                duplicate_rows=duplicate_rows,
                invalid_rows=invalid_rows,
                has_more=_payload_has_more(payload, page=page, page_size=self._page_size),
            )
        finally:
            counters.parse_seconds += max(0.0, self._monotonic() - parse_started)

    def _persist_page(
        self,
        context: _CninfoSyncContext,
        state: _CninfoSyncState,
        counters: _CninfoRunCounters,
        page: _CninfoFetchedPage,
    ) -> tuple[str, ...]:
        records = _announcement_records(
            context.code,
            context.observed_at,
            page.rows,
            data_version=_range_version(state.range_start, state.range_end),
        )
        if records:
            write_started = self._monotonic()
            try:
                self._data_plane.save_risk_evidence_recent_records(records)
            except DataPlaneBusyError:
                return ("cninfo_persist_busy",)
            except DataPlaneUnavailableError as exc:
                return (f"cninfo_persist_unavailable:{type(exc).__name__}",)
            finally:
                counters.evidence_write_seconds += max(0.0, self._monotonic() - write_started)
        counters.saved_announcements += len(records)
        counters.duplicate_rows += page.duplicate_rows
        counters.invalid_rows += page.invalid_rows
        _apply_persisted_page(state, page, len(records))
        return ()

    def _load_cursor(self, cursor_name: str) -> _CninfoCursorState:
        cursor = self._data_plane.load_source_cursor_recent(cursor_name)
        if cursor is None:
            return _CninfoCursorState()
        payload = cursor.payload
        issuer_history_complete = (
            bool(payload.get("issuer_history_complete")) if isinstance(payload, Mapping) else False
        )
        if not isinstance(payload, Mapping):
            return _CninfoCursorState(cursor.cursor_value, issuer_history_complete, cursor.observed_at)
        categories = tuple(
            category
            for value in _string_sequence(payload.get("risk_categories"))
            if (category := _corporate_risk_category(value)) is not None
        )
        return _CninfoCursorState(
            value=cursor.cursor_value,
            issuer_history_complete=issuer_history_complete,
            observed_at=cursor.observed_at,
            checkpoint_version=_positive_int(payload.get("checkpoint_version"), default=0),
            window_days=_non_negative_int(payload.get("window_days")),
            initial_backfill_complete=bool(payload.get("initial_backfill_complete", issuer_history_complete)),
            range_start=_payload_date(payload.get("range_start")),
            range_end=_payload_date(payload.get("range_end")),
            next_page=_positive_int(payload.get("next_page"), default=1),
            latest_cursor_value=str(payload.get("latest_cursor_value") or cursor.cursor_value),
            total_pages_persisted=_non_negative_int(payload.get("total_pages_persisted")),
            total_saved_rows=_non_negative_int(payload.get("total_saved_rows") or payload.get("saved_rows")),
            range_duplicate_rows=_non_negative_int(payload.get("range_duplicate_rows")),
            range_invalid_rows=_non_negative_int(payload.get("range_invalid_rows")),
            risk_categories=categories,
        )


CninfoPostFunction = Callable[..., requests.Response]
CninfoSessionFactory = Callable[[], requests.Session]


class _CninfoThreadState(local):
    session: requests.Session | None = None


class CninfoAnnouncementHttpClient:
    """Fetch issuer announcements from CNInfo's public web query endpoints."""

    _SEARCH_URL = "https://www.cninfo.com.cn/new/information/topSearch/query"
    _ANNOUNCEMENT_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"

    def __init__(
        self,
        *,
        post: CninfoPostFunction | None = None,
        session_factory: CninfoSessionFactory | None = None,
        timeout_seconds: float,
        wall_clock: Callable[[], datetime],
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if timeout_seconds <= 0.0:
            raise ValueError("CNInfo timeout must be positive")
        if (post is None) == (session_factory is None):
            raise ValueError("CNInfo requires exactly one post function or session factory")
        self._post = post
        self._session_factory = session_factory
        self._timeout_seconds = timeout_seconds
        self._wall_clock = wall_clock
        self._sleeper = sleeper
        self._organizations: dict[str, str] = {}
        self._organization_lock = Lock()
        self._thread_state = _CninfoThreadState()
        self._sessions: list[requests.Session] = []
        self._sessions_lock = Lock()

    def fetch_announcements(self, query: CninfoAnnouncementQuery) -> Mapping[str, object]:
        _validate_code(query.code)
        _validate_time(query.observed_at, "observed_at")
        if query.page < 1 or query.page_size < 1:
            raise ValueError("CNInfo page coordinates must be positive")
        organization = self._organization_id(query.code, query.deadline)
        column, plate = _cninfo_market_scope(query.code)
        start_date = query.range_start or max(
            _cursor_start_date(query.cursor_value), query.observed_at.date() - timedelta(days=_HISTORY_LOOKBACK_DAYS)
        )
        end_date = query.range_end or query.observed_at.date()
        if end_date < start_date:
            raise ValueError("CNInfo query range end precedes start")
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
                "seDate": f"{start_date.isoformat()}~{end_date.isoformat()}",
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
        with self._organization_lock:
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
        with self._organization_lock:
            self._organizations[code] = organization
        return organization

    def close(self) -> None:
        with self._sessions_lock:
            sessions = tuple(self._sessions)
            self._sessions.clear()
        for session in sessions:
            session.close()

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
                timeout = self._remaining_timeout(deadline)
                response = self._post_request(
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

    def _post_request(
        self,
        url: str,
        *,
        data: Mapping[str, str],
        headers: Mapping[str, str],
        timeout: float,
    ) -> requests.Response:
        if self._post is not None:
            return self._post(url, data=data, headers=headers, timeout=timeout)
        session = self._thread_state.session
        if session is None:
            if self._session_factory is None:
                raise RuntimeError("CNInfo session factory is unavailable")
            session = self._session_factory()
            self._thread_state.session = session
            with self._sessions_lock:
                self._sessions.append(session)
        return session.post(url, data=data, headers=headers, timeout=timeout)

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


def _build_sync_state(cursor: _CninfoCursorState, observed_at: datetime) -> _CninfoSyncState:
    window_start = observed_at.date() - timedelta(days=_HISTORY_LOOKBACK_DAYS)
    is_resumable = (
        not cursor.issuer_history_complete
        and cursor.checkpoint_version == _CHECKPOINT_VERSION
        and cursor.window_days == _HISTORY_LOOKBACK_DAYS
        and cursor.range_start is not None
        and cursor.range_end is not None
        and cursor.range_start <= cursor.range_end
    )
    if is_resumable:
        assert cursor.range_start is not None
        assert cursor.range_end is not None
        return _CninfoSyncState(
            range_start=cursor.range_start,
            range_end=cursor.range_end,
            next_page=cursor.next_page,
            latest_cursor_value=cursor.latest_cursor_value,
            initial_backfill_complete=cursor.initial_backfill_complete,
            total_pages_persisted=cursor.total_pages_persisted,
            total_saved_rows=cursor.total_saved_rows,
            range_duplicate_rows=cursor.range_duplicate_rows,
            range_invalid_rows=cursor.range_invalid_rows,
            risk_categories=set(cursor.risk_categories),
            checkpoint_observed_at=cursor.observed_at,
            resumed=True,
        )
    initial_backfill_complete = cursor.issuer_history_complete
    range_start = max(window_start, _cursor_start_date(cursor.value)) if initial_backfill_complete else window_start
    return _CninfoSyncState(
        range_start=min(range_start, observed_at.date()),
        range_end=observed_at.date(),
        next_page=1,
        latest_cursor_value=cursor.value if initial_backfill_complete else "",
        initial_backfill_complete=initial_backfill_complete,
        total_pages_persisted=cursor.total_pages_persisted if initial_backfill_complete else 0,
        total_saved_rows=cursor.total_saved_rows if initial_backfill_complete else 0,
        range_duplicate_rows=0,
        range_invalid_rows=0,
        risk_categories=set(cursor.risk_categories) if initial_backfill_complete else set(),
        checkpoint_observed_at=cursor.observed_at,
        resumed=False,
    )


def _unique_announcement_rows(
    rows: Sequence[CninfoAnnouncementRow],
) -> tuple[tuple[CninfoAnnouncementRow, ...], int]:
    unique: list[CninfoAnnouncementRow] = []
    seen: set[str] = set()
    duplicates = 0
    for row in rows:
        if row.announcement_id in seen:
            duplicates += 1
            continue
        seen.add(row.announcement_id)
        unique.append(row)
    return tuple(unique), duplicates


def _announcement_records(
    code: str,
    observed_at: datetime,
    rows: Sequence[CninfoAnnouncementRow],
    *,
    data_version: str,
) -> tuple[RiskEvidenceRecord, ...]:
    return tuple(
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


def _risk_categories(rows: Sequence[CninfoAnnouncementRow]) -> set[CorporateRiskCategory]:
    announcements = tuple(
        ResearchAnnouncement(
            title=item.title,
            published_at=item.published_at,
            announcement_id=f"{CNINFO_ANNOUNCEMENT_PREFIX}{item.announcement_id}",
            source="issuer_disclosure",
        )
        for item in rows
    )
    return {fact.category for fact in CorporateRiskFactsFromAnnouncements(announcements)}


def _apply_persisted_page(state: _CninfoSyncState, page: _CninfoFetchedPage, saved_rows: int) -> None:
    state.latest_cursor_value = _next_cursor(state.latest_cursor_value, page.rows, state.range_end)
    state.total_pages_persisted += 1
    state.total_saved_rows += saved_rows
    state.range_duplicate_rows += page.duplicate_rows
    state.range_invalid_rows += page.invalid_rows
    state.risk_categories.update(_risk_categories(page.rows))
    state.next_page = page.page + 1


def _complete_range_if_needed(
    data_plane: CninfoDataPlane,
    context: _CninfoSyncContext,
    state: _CninfoSyncState,
    page: _CninfoFetchedPage,
) -> tuple[bool, bool, tuple[str, ...]]:
    if page.has_more:
        return False, False, ()
    if state.range_invalid_rows:
        errors = _save_confirmed_risk_statuses(
            data_plane,
            code=context.code,
            observed_at=context.observed_at,
            state=state,
        )
        if errors:
            return False, False, errors
        state.next_page = 1
        return False, True, ()
    errors = _finalize_completed_range(
        data_plane,
        code=context.code,
        observed_at=context.observed_at,
        state=state,
    )
    if errors:
        return False, False, errors
    if state.range_end < context.observed_at.date():
        _advance_to_current_range(state, context.observed_at.date())
        return False, False, ()
    return True, False, ()


def _finalize_completed_range(
    data_plane: CninfoDataPlane,
    *,
    code: str,
    observed_at: datetime,
    state: _CninfoSyncState,
) -> tuple[str, ...]:
    if not state.initial_backfill_complete:
        errors = _save_component_statuses(
            data_plane,
            _CninfoComponentBatch(
                code=code,
                observed_at=observed_at,
                source_time=_cursor_source_time(state.latest_cursor_value, observed_at),
                data_version=_range_version(state.range_start, state.range_end),
                statuses=_component_statuses(state.risk_categories, history_complete=True),
                announcement_count=state.total_saved_rows,
            ),
        )
        if not errors:
            state.initial_backfill_complete = True
        return errors
    return ()


def _save_confirmed_risk_statuses(
    data_plane: CninfoDataPlane,
    *,
    code: str,
    observed_at: datetime,
    state: _CninfoSyncState,
) -> tuple[str, ...]:
    statuses = {
        component: status
        for component, status in _component_statuses(state.risk_categories, history_complete=False).items()
        if status == "known_risk"
    }
    if not statuses:
        return ()
    return _save_component_statuses(
        data_plane,
        _CninfoComponentBatch(
            code=code,
            observed_at=observed_at,
            source_time=_cursor_source_time(state.latest_cursor_value, observed_at),
            data_version=_range_version(state.range_start, state.range_end),
            statuses=statuses,
            announcement_count=state.total_saved_rows,
        ),
    )


def _advance_to_current_range(state: _CninfoSyncState, range_end: date) -> None:
    state.range_start = _cursor_start_date(state.latest_cursor_value)
    state.range_end = range_end
    state.next_page = 1
    state.range_duplicate_rows = 0
    state.range_invalid_rows = 0
    state.risk_categories.clear()


def _save_component_statuses(
    data_plane: CninfoDataPlane,
    batch: _CninfoComponentBatch,
) -> tuple[str, ...]:
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
                "history_complete": False,
                "announcement_count": batch.announcement_count,
                "source": CNINFO_SOURCE,
            },
        )
        for component, status in batch.statuses.items()
    )
    try:
        data_plane.save_risk_evidence_recent_records(records)
    except DataPlaneBusyError:
        return ("cninfo_component_persist_busy",)
    except DataPlaneUnavailableError as exc:
        return (f"cninfo_component_persist_unavailable:{type(exc).__name__}",)
    return ()


def _component_statuses(
    categories: set[CorporateRiskCategory],
    *,
    history_complete: bool,
) -> dict[str, str]:
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


def _save_cursor_checkpoint(
    data_plane: CninfoDataPlane,
    *,
    context: _CninfoSyncContext,
    state: _CninfoSyncState,
    history_complete: bool,
) -> tuple[str, ...]:
    checkpoint_time = _next_checkpoint_time(context.observed_at, state.checkpoint_observed_at)
    cursor_value = state.latest_cursor_value or f"empty:{state.range_end.isoformat()}"
    try:
        data_plane.save_source_cursor_recent(
            SourceCursorRecord(
                cursor_name=f"{CNINFO_CURSOR_PREFIX}{context.code}",
                observed_at=checkpoint_time,
                source=CNINFO_SOURCE,
                source_time=_cursor_source_time(cursor_value, context.observed_at),
                data_version=f"cninfo-checkpoint:{context.code}:{state.range_end.isoformat()}:{state.next_page}",
                cursor_value=cursor_value,
                payload={
                    "history_complete": False,
                    "issuer_history_complete": history_complete,
                    "checkpoint_version": _CHECKPOINT_VERSION,
                    "window_days": _HISTORY_LOOKBACK_DAYS,
                    "initial_backfill_complete": state.initial_backfill_complete,
                    "range_start": state.range_start.isoformat(),
                    "range_end": state.range_end.isoformat(),
                    "next_page": state.next_page,
                    "latest_cursor_value": cursor_value,
                    "last_announcement_id": cursor_value.partition("|")[2],
                    "total_pages_persisted": state.total_pages_persisted,
                    "total_saved_rows": state.total_saved_rows,
                    "saved_rows": state.total_saved_rows,
                    "range_duplicate_rows": state.range_duplicate_rows,
                    "range_invalid_rows": state.range_invalid_rows,
                    "duplicate_rows": state.range_duplicate_rows,
                    "invalid_rows": state.range_invalid_rows,
                    "risk_categories": tuple(sorted(category.value for category in state.risk_categories)),
                    "exchange_cross_check_status": "pending",
                },
            )
        )
    except DataPlaneBusyError:
        return ("cninfo_cursor_persist_busy",)
    except DataPlaneUnavailableError as exc:
        return (f"cninfo_cursor_persist_unavailable:{type(exc).__name__}",)
    state.latest_cursor_value = cursor_value
    state.checkpoint_observed_at = checkpoint_time
    return ()


def _persistence_error(errors: tuple[str, ...]) -> Literal["busy", "unavailable"] | None:
    if not errors:
        return None
    return "busy" if any(error.endswith("_busy") for error in errors) else "unavailable"


def _next_cursor(previous: str, rows: Sequence[CninfoAnnouncementRow], range_end: date) -> str:
    if rows:
        latest = max(rows, key=lambda item: (item.published_at, item.announcement_id))
        candidate = f"{latest.published_at.isoformat()}|{latest.announcement_id}"
        if not previous or _cursor_sort_key(candidate) > _cursor_sort_key(previous):
            return candidate
    return previous or f"empty:{range_end.isoformat()}"


def _cursor_start_date(cursor_value: str) -> date:
    raw = cursor_value.partition("|")[0]
    if raw.startswith("empty:"):
        raw = raw.removeprefix("empty:")
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        return _LEGACY_HISTORY_START


def _cursor_sort_key(cursor_value: str) -> tuple[datetime, str]:
    raw_time, _separator, announcement_id = cursor_value.partition("|")
    try:
        parsed = datetime.fromisoformat(raw_time)
    except ValueError:
        parsed = datetime.min.replace(tzinfo=SHANGHAI_TZ)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=SHANGHAI_TZ)
    return parsed, announcement_id


def _cursor_source_time(cursor_value: str, fallback: datetime) -> datetime:
    parsed, _announcement_id = _cursor_sort_key(cursor_value)
    return fallback if parsed == datetime.min.replace(tzinfo=SHANGHAI_TZ) else parsed


def _next_checkpoint_time(observed_at: datetime, previous: datetime | None) -> datetime:
    if previous is None or observed_at > previous:
        return observed_at
    return previous + timedelta(microseconds=1)


def _range_version(range_start: date, range_end: date) -> str:
    return f"cninfo-announcements:{range_start.isoformat()}:{range_end.isoformat()}"


def _payload_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _positive_int(value: object, *, default: int) -> int:
    parsed = _int_value(value)
    return parsed if parsed is not None and parsed > 0 else default


def _non_negative_int(value: object) -> int:
    parsed = _int_value(value)
    return parsed if parsed is not None and parsed >= 0 else 0


def _string_sequence(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _corporate_risk_category(value: str) -> CorporateRiskCategory | None:
    try:
        return CorporateRiskCategory(value)
    except ValueError:
        return None


def _cninfo_market_scope(code: str) -> tuple[str, str]:
    if code.startswith(("4", "8", "92")):
        return "third", "bj"
    if code.startswith(("5", "6", "9")):
        return "sse", "sh"
    return "szse", "sz"


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
    "CninfoAnnouncementHttpClient",
    "CninfoAnnouncementIncrementalSync",
    "CninfoAnnouncementQuery",
    "CninfoAnnouncementRow",
    "CninfoDataPlane",
    "CninfoSyncResult",
    "ParseCninfoAnnouncementRows",
]
