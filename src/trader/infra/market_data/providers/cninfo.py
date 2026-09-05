"""CNInfo announcement incremental sync helpers for the risk registry."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

from trader.application.ports.data_plane import (
    DataPlaneUnavailableError,
    RiskEvidenceRecord,
    SourceCursorRecord,
)
from trader.domain.market.research import (
    CorporateRiskCategory,
    ResearchAnnouncement,
    corporate_risk_facts_from_announcements,
)

CNINFO_SOURCE = "cninfo"
CNINFO_CURSOR_PREFIX = "cninfo.announcements:"
CNINFO_ANNOUNCEMENT_PREFIX = "cninfo-announcement:"
CNINFO_COMPONENT_PREFIX = "cninfo-risk-component:"
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


class CninfoAnnouncementClient(Protocol):
    def fetch_announcements(
        self,
        code: str,
        *,
        page: int,
        page_size: int,
        observed_at: datetime,
        cursor_value: str,
    ) -> Mapping[str, object]: ...


class CninfoDataPlane(Protocol):
    def save_risk_evidence_recent(self, record: RiskEvidenceRecord) -> None: ...

    def save_source_cursor_recent(self, record: SourceCursorRecord) -> None: ...

    def load_source_cursor_recent(self, cursor_name: str) -> SourceCursorRecord | None: ...


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


class CninfoAnnouncementIncrementalSync:
    """Persist CNInfo announcements and source cursors without changing runtime routing."""

    def __init__(
        self,
        client: CninfoAnnouncementClient,
        data_plane: CninfoDataPlane,
        *,
        page_size: int = 100,
        max_pages: int = 20,
    ) -> None:
        self._client = client
        self._data_plane = data_plane
        self._page_size = max(1, page_size)
        self._max_pages = max(1, max_pages)

    def sync_code(self, code: str, observed_at: datetime) -> CninfoSyncResult:
        _validate_code(code)
        _validate_time(observed_at, "observed_at")
        cursor_name = f"{CNINFO_CURSOR_PREFIX}{code}"
        cursor_value = self._load_cursor(cursor_name)
        rows: list[CninfoAnnouncementRow] = []
        seen: set[str] = set()
        duplicate_rows = 0
        invalid_rows = 0
        pages_fetched = 0
        history_complete = False

        for page in range(1, self._max_pages + 1):
            payload = self._client.fetch_announcements(
                code,
                page=page,
                page_size=self._page_size,
                observed_at=observed_at,
                cursor_value=cursor_value,
            )
            pages_fetched += 1
            parsed, page_invalid = parse_cninfo_announcement_rows(code, payload, observed_at)
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

        data_version = _batch_version(rows, cursor_value, observed_at)
        saved = 0
        source_errors: list[str] = []
        for row in rows:
            try:
                self._data_plane.save_risk_evidence_recent(
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
                )
                saved += 1
            except DataPlaneUnavailableError as exc:
                source_errors.append(f"cninfo_persist_unavailable:{type(exc).__name__}")
                break

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
            if rows or not cursor_value:
                source_errors.extend(_save_component_statuses(self._data_plane, batch))
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

    def _load_cursor(self, cursor_name: str) -> str:
        try:
            cursor = self._data_plane.load_source_cursor_recent(cursor_name)
        except DataPlaneUnavailableError:
            return ""
        return "" if cursor is None else cursor.cursor_value


def parse_cninfo_announcement_rows(
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
        announcement_id = raw_id.strip() or _derived_id(code, title, published_at)
        parsed.append(
            CninfoAnnouncementRow(
                code=code,
                announcement_id=announcement_id,
                title=title[:240],
                published_at=published_at,
                source_payload_hash=_mapping_hash(raw),
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
    errors: list[str] = []
    for component, status in statuses.items():
        try:
            data_plane.save_risk_evidence_recent(
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
            )
        except DataPlaneUnavailableError as exc:
            errors.append(f"cninfo_component_persist_unavailable:{type(exc).__name__}")
            break
    return tuple(errors)


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
    categories = {fact.category for fact in corporate_risk_facts_from_announcements(research_announcements)}
    if not history_complete:
        base_clear = "stale"
    else:
        base_clear = "known_clear"
    return {
        "announcements": "known_risk" if categories else base_clear,
        "penalty": "known_risk" if categories & _PENALTY_CATEGORIES else base_clear,
        "lawsuit_restructuring": "known_risk"
        if CorporateRiskCategory.MAJOR_SHAREHOLDER_REDUCTION in categories
        else base_clear,
        "forced_delisting": "known_risk" if CorporateRiskCategory.FORCED_DELISTING in categories else base_clear,
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
                    "history_complete": batch.history_complete,
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
    material = "|".join(
        (
            cursor_value,
            observed_at.isoformat(),
            *(f"{row.announcement_id}:{row.published_at.isoformat()}:{row.source_payload_hash}" for row in rows),
        )
    )
    return f"cninfo-announcements:{hashlib.sha256(material.encode()).hexdigest()[:16]}"


def _derived_id(code: str, title: str, published_at: datetime) -> str:
    digest = hashlib.sha256(f"{code}|{published_at.isoformat()}|{title}".encode()).hexdigest()[:24]
    return f"derived-{digest}"


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
    "CninfoAnnouncementIncrementalSync",
    "CninfoAnnouncementRow",
    "CninfoSyncResult",
    "parse_cninfo_announcement_rows",
]
