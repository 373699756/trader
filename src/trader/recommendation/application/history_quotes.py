"""Demand-driven monthly history quotes, isolated from scoring and Long."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Protocol

from trader.infra.shutdown import ShutdownDeadline, ShutdownStep
from trader.recommendation.application.pipeline.freeze_publish.read_only_queries import FreezeHistoryReader
from trader.recommendation.domain.market.models import MarketQuote
from trader.recommendation.domain.market.quote_freshness import QuoteFreshnessAgeSeconds, QuoteSessionTimes
from trader.recommendation.domain.publication.freeze_identity import VisibleScoredSeats
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.application.runtime.schedule import SHANGHAI


@dataclass(frozen=True, order=True)
class HistoryQuoteMonth:
    year: int
    month: int

    def __post_init__(self) -> None:
        date(self.year, self.month, 1)


@dataclass(frozen=True)
class HistoryQuoteSettings:
    interval_seconds: float = 60
    max_age_seconds: float = 120
    timeout_seconds: float = 10
    lease_seconds: float = 120
    maximum_viewers: int = 32

    def __post_init__(self) -> None:
        if any(not math.isfinite(v) or v <= 0 for v in (
            self.interval_seconds, self.max_age_seconds, self.timeout_seconds, self.lease_seconds,
        )) or self.maximum_viewers < 1:
            raise ValueError("invalid history quote settings")


class HistoryQuoteSource(Protocol):
    def __call__(self, codes: Sequence[str], now: datetime, deadline: datetime,
                 cancelled: threading.Event) -> Sequence[MarketQuote]: ...


@dataclass(frozen=True)
class _Watch:
    months: tuple[HistoryQuoteMonth, ...]
    expires: float


@dataclass
class _MonthScan:
    pending: tuple[tuple[int, Strategy, date], ...]
    position: int
    codes: set[str]


class HistoryQuotes:
    def __init__(self, records: tuple[FreezeHistoryReader, ...], source: HistoryQuoteSource,
                 now: Callable[[], datetime], open_dates: Callable[[], tuple[date, ...]],
                 sessions: QuoteSessionTimes, settings: HistoryQuoteSettings | None = None,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self._records, self._source, self._now = records, source, now
        self._open_dates, self._sessions = open_dates, sessions
        self.settings = settings if settings is not None else HistoryQuoteSettings()
        self._monotonic = monotonic
        self._lock = threading.RLock()
        self._watches: dict[str, _Watch] = {}
        self._quotes: dict[str, MarketQuote] = {}
        self._due: dict[str, float] = {}
        self._month_codes: dict[HistoryQuoteMonth, tuple[float, frozenset[str]]] = {}
        self._month_scans: dict[HistoryQuoteMonth, _MonthScan] = {}
        self._wake, self._stop = threading.Event(), threading.Event()
        self._thread: threading.Thread | None = None
        self._error = ""

    def watch(self, viewer: str, months: tuple[HistoryQuoteMonth, ...]) -> None:
        if not viewer or len(viewer) > 64 or not 1 <= len(months) <= 2:
            raise ValueError("invalid history watch")
        with self._lock:
            self._expire()
            if viewer not in self._watches and len(self._watches) >= self.settings.maximum_viewers:
                raise OverflowError("history viewer limit reached")
            self._watches[viewer] = _Watch(tuple(sorted(set(months))), self._monotonic() + self.settings.lease_seconds)
        self._wake.set()

    def release(self, viewer: str) -> None:
        with self._lock:
            self._watches.pop(viewer, None)
        self._wake.set()

    def _expire(self) -> None:
        now = self._monotonic()
        self._watches = {key: value for key, value in self._watches.items() if value.expires > now}

    def cached_quotes(self, codes: Sequence[str]) -> Mapping[str, MarketQuote]:
        with self._lock:
            return {code: self._quotes[code] for code in codes if code in self._quotes}

    def diagnostic(self) -> tuple[str, int, int]:
        with self._lock:
            return self._error, len(self._watches), len(self._quotes)

    def start(self) -> bool:
        with self._lock:
            if self._thread is not None:
                return False
            self._thread = threading.Thread(target=self._run, name="trader-history-quotes", daemon=True)
            self._thread.start()
        return True

    def stop(self, *, wait: bool = True, deadline: ShutdownDeadline | None = None) -> ShutdownStep:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if wait and thread is not None:
            thread.join((deadline or ShutdownDeadline.start(self.settings.timeout_seconds + 1)).remaining_seconds())
        alive = thread is not None and thread.is_alive()
        return ShutdownStep("history_quotes", not alive, alive)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            try:
                self.refresh()
            except (OSError, RuntimeError, ValueError) as exc:
                with self._lock:
                    self._error = type(exc).__name__
            self._wake.wait(1)

    def refresh(self) -> None:
        """One bounded worker iteration; injectable clocks make cadence testable."""
        scan_deadline = self._monotonic() + self.settings.timeout_seconds
        with self._lock:
            self._expire()
            months = {month for watch in self._watches.values() for month in watch.months}
        self._month_scans = {month: scan for month, scan in self._month_scans.items() if month in months}
        codes: set[str] = set()
        for month in sorted(months):
            cached = self._month_codes.get(month)
            if cached is not None and (cached[0] > self._monotonic()
                                       or self._month_scans and month not in self._month_scans):
                codes.update(cached[1])
                continue
            monthly = self._scan_month(month, scan_deadline)
            if monthly is None:
                with self._lock:
                    self._error = "history_month_scan_pending"
                return
            self._month_codes[month] = (self._monotonic() + self.settings.interval_seconds, monthly)
            codes.update(monthly)
        self._month_codes = {month: value for month, value in self._month_codes.items() if month in months}
        with self._lock:
            if self._error == "history_month_scan_pending":
                self._error = ""
            self._expire()
            if {month for watch in self._watches.values() for month in watch.months} != months:
                return
            self._quotes = {code: quote for code, quote in self._quotes.items() if code in codes}
            self._due = {code: due for code, due in self._due.items() if code in codes}
        if not codes:
            return
        now = self._now().astimezone(SHANGHAI)
        calendar = self._open_dates()
        eligible_dates = [day for day in calendar if day <= now.date()]
        if not eligible_dates:
            raise ValueError("history trading calendar unavailable")
        minute = now.hour * 60 + now.minute
        trading = now.date() in calendar and (
            self._sessions.morning_open <= minute < self._sessions.morning_close
            or self._sessions.afternoon_open <= minute < self._sessions.afternoon_close
        )
        market_day = max(eligible_dates)
        if market_day == now.date() and minute < self._sessions.morning_open:
            market_day = max(day for day in calendar if day < now.date())
        if self._stop.is_set():
            return
        with self._lock:
            due_codes = tuple(sorted(code for code in codes if self._due.get(code, 0) <= self._monotonic()
                                     and (trading or not self._rest_valid(self._quotes.get(code), now, market_day))))
            for code in due_codes:
                self._due[code] = self._monotonic() + self.settings.interval_seconds
        if not due_codes:
            return
        quotes = self._source(due_codes, now, now + timedelta(seconds=self.settings.timeout_seconds), self._stop)
        completed = self._now().astimezone(SHANGHAI)
        with self._lock:
            self._expire()
            if {month for watch in self._watches.values() for month in watch.months} != months:
                return
            self._error = "partial" if len(quotes) < len(due_codes) else ""
            for quote in quotes:
                previous = self._quotes.get(quote.code)
                if (quote.code in codes and quote.price is not None and math.isfinite(quote.price) and quote.price > 0
                        and quote.source_time.utcoffset() is not None and quote.source_time <= completed
                        and quote.source_time.astimezone(SHANGHAI).date() == market_day
                        and (previous is None or quote.source_time >= previous.source_time)):
                    self._quotes[quote.code] = quote

    def _scan_month(self, month: HistoryQuoteMonth, deadline: float) -> frozenset[str] | None:
        scan = self._month_scans.get(month)
        if scan is None:
            pending = tuple(
                (index, strategy, day)
                for index, reader in enumerate(self._records)
                for strategy in (Strategy.TOMORROW, Strategy.D25)
                for day in reader.list_dates(strategy, year=month.year, month=month.month)
            )
            scan = _MonthScan(pending, 0, set())
            self._month_scans[month] = scan
        while scan.position < len(scan.pending):
            if self._stop.is_set() or self._monotonic() >= deadline:
                return None
            index, strategy, day = scan.pending[scan.position]
            record = self._records[index].load_history(strategy, day)
            if record is not None:
                scan.codes.update(item.code for item in VisibleScoredSeats(record.freeze))
            scan.position += 1
        self._month_scans.pop(month, None)
        return frozenset(scan.codes)

    def _rest_valid(self, quote: MarketQuote | None, now: datetime, market_day: date) -> bool:
        if quote is None or quote.source_time.astimezone(SHANGHAI).date() != market_day:
            return False
        if market_day != now.date():
            return quote.source_time.astimezone(SHANGHAI).hour >= 15
        return QuoteFreshnessAgeSeconds(quote.source_time, now, sessions=self._sessions) <= self.settings.max_age_seconds

    def quote_status(self, quote: MarketQuote, now: datetime) -> str:
        try:
            calendar = self._open_dates()
        except (OSError, RuntimeError, ValueError):
            return "retained"
        days = [day for day in calendar if day <= now.date()]
        if not days:
            return "retained"
        day = max(days)
        return "live" if self._rest_valid(quote, now, day) else "retained"
