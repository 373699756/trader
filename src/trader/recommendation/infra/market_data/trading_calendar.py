"""Recommendation A-share trading calendar with fail-closed behavior."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from trader.recommendation.application.ports.runtime import TradingCalendarUnavailableError

CalendarFetcher = Callable[[float], Iterable[date]]


class ChinaTradingCalendar:
    def __init__(
        self,
        cache_path: Path,
        *,
        max_cache_age_days: int = 30,
        fetcher: CalendarFetcher,
        fetch_timeout_seconds: float = 5.0,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if fetch_timeout_seconds <= 0.0:
            raise ValueError("calendar fetch timeout must be positive")
        self._cache_path = cache_path
        self._max_cache_age = timedelta(days=max_cache_age_days)
        self._fetcher = fetcher
        self._fetch_timeout_seconds = float(fetch_timeout_seconds)
        self._now = now
        self._dates: frozenset[date] = frozenset()
        self._fetched_at: datetime | None = None
        self._load_lock = threading.Lock()

    def is_trading_day(self, day: date) -> bool:
        self._ensure_loaded()
        if not self._dates:
            raise TradingCalendarUnavailableError("trading calendar is unavailable")
        return day in self._dates

    def open_dates(self) -> tuple[date, ...]:
        self._ensure_loaded()
        if not self._dates:
            raise TradingCalendarUnavailableError("trading calendar is unavailable")
        return tuple(sorted(self._dates))

    def session_distance(self, start: str, end: str) -> int | None:
        """Return trading sessions strictly after ``start`` through ``end``.

        Calendar failure is fail-closed for fallback selection: callers receive
        ``None`` and must treat the population age as unverifiable.
        """

        try:
            start_date = date.fromisoformat(start)
            end_date = date.fromisoformat(end)
            if end_date < start_date:
                return None
            self._ensure_loaded()
        except (OSError, TradingCalendarUnavailableError, ValueError):
            return None
        return sum(day > start_date and day <= end_date for day in self._dates)

    def _ensure_loaded(self) -> None:
        with self._load_lock:
            if not self._dates:
                self._load_cache()
            if self._fetched_at is not None and self._now() - self._fetched_at <= self._max_cache_age:
                return
            dates = self._fetch_with_timeout()
            if not dates:
                raise TradingCalendarUnavailableError("calendar provider returned no dates")
            self._dates = dates
            self._fetched_at = self._now()
            self._save_cache()

    def _fetch_with_timeout(self) -> frozenset[date]:
        try:
            return frozenset(self._fetcher(self._fetch_timeout_seconds))
        except (OSError, RuntimeError, ValueError) as exc:
            raise TradingCalendarUnavailableError(f"calendar_refresh_{type(exc).__name__}") from exc

    def _load_cache(self) -> None:
        try:
            raw = json.loads(self._cache_path.read_text(encoding="utf-8"))
            fetched_at = datetime.fromisoformat(str(raw["fetched_at"]))
            dates = frozenset(date.fromisoformat(str(value)) for value in raw["dates"])
        except (FileNotFoundError, OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        self._fetched_at = fetched_at
        self._dates = dates

    def _save_cache(self) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "fetched_at": self._fetched_at.isoformat() if self._fetched_at else "",
                "dates": sorted(value.isoformat() for value in self._dates),
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{self._cache_path.name}.", dir=self._cache_path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self._cache_path)
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise


__all__ = ["ChinaTradingCalendar", "TradingCalendarUnavailableError"]
