"""Bounded paired daily windows and immutable update results."""

from __future__ import annotations

import re
from bisect import bisect_left
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from trader.download.domain.published_history import PublishedHistoryCell, PublishedHistoryWindow

DAILY_READY_AT = time(15, 15)
MAX_QFQ_WINDOW_SESSIONS = 2000
_SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True, slots=True)
class QfqWindowConfiguration:
    """Configured profile windows used by the writer; persisted calendars own reads."""

    v2_sessions: int
    v3_sessions: int

    def __post_init__(self) -> None:
        if (
            type(self.v2_sessions) is not int
            or type(self.v3_sessions) is not int
            or self.v3_sessions < 1
            or self.v2_sessions < self.v3_sessions
            or self.v2_sessions > MAX_QFQ_WINDOW_SESSIONS
        ):
            raise ValueError("qfq window configuration is invalid")

    @property
    def maximum_sessions(self) -> int:
        return self.v2_sessions

    def sessions_for(self, profile: Literal["v2", "v3"]) -> int:
        return self.v2_sessions if profile == "v2" else self.v3_sessions


class QfqWindowIncompleteError(RuntimeError):
    """A requested calendar window contains an unpaired supplier gap."""

    def __init__(self, raw_missing: tuple[date, ...], qfq_missing: tuple[date, ...]) -> None:
        self.raw_missing = raw_missing
        self.qfq_missing = qfq_missing
        super().__init__(f"qfq_incomplete_raw_{len(raw_missing)}_qfq_{len(qfq_missing)}")


@dataclass(frozen=True, slots=True)
class QfqWindowState:
    code: str
    source: str
    dates: tuple[date, ...]
    verified: bool


@dataclass(frozen=True, slots=True)
class QfqWindowSnapshot:
    states: tuple[QfqWindowState, ...]

    def __post_init__(self) -> None:
        codes = tuple(state.code for state in self.states)
        if codes != tuple(sorted(set(codes))):
            raise ValueError("qfq window snapshot must contain ordered unique codes")

    def state_for(self, code: str) -> QfqWindowState | None:
        index = bisect_left(self.states, code, key=lambda state: state.code)
        return self.states[index] if index < len(self.states) and self.states[index].code == code else None


@dataclass(frozen=True, slots=True)
class QfqPendingReason:
    reason: str
    count: int

    def __post_init__(self) -> None:
        if re.fullmatch(r"[a-zA-Z0-9_]{1,64}", self.reason) is None or type(self.count) is not int or self.count < 1:
            raise ValueError("invalid qfq pending reason")


@dataclass(frozen=True, slots=True)
class QfqUpdateResult:
    target_date: date | None
    completed_codes: int = 0
    pending_codes: int = 0
    changed_files: tuple[str, ...] = ()
    changed_rows: int = 0
    skipped_codes: int = 0
    failure_reason: str | None = None
    pending_reasons: tuple[QfqPendingReason, ...] = ()
    no_trade_data_codes: int = 0
    latest_missing_codes: int = 0

    @property
    def needs_retry(self) -> bool:
        widespread_missing = self.latest_missing_codes >= max(20, (self.completed_codes + 99) // 100)
        return self.pending_codes > 0 or self.failure_reason is not None or widespread_missing


def CompletedDailyCutoff(observed_at: datetime) -> date:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("qfq update requires an aware business clock")
    local = observed_at.astimezone(_SHANGHAI)
    return local.date() if local.time() >= DAILY_READY_AT else local.date() - timedelta(days=1)


def MergeDailyTail(
    previous: PublishedHistoryWindow, incoming: PublishedHistoryWindow, dates: tuple[date, ...]
) -> PublishedHistoryWindow | None:
    """Exact overlap establishes one adjustment basis; conflicts require a full fetch."""
    existing = {cell.trade_date: cell for cell in previous.cells}
    overlap = tuple(cell for cell in incoming.cells if cell.trade_date in existing)
    if not overlap or any(existing[cell.trade_date] != cell for cell in overlap):
        return None
    combined: dict[date, PublishedHistoryCell] = existing | {cell.trade_date: cell for cell in incoming.cells}
    return PublishedHistoryWindow(previous.code, tuple(combined[day] for day in dates if day in combined))
