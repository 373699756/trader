"""Cached A-share trading calendar with fail-closed behavior."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

CalendarFetcher = Callable[[], Iterable[date]]


class TradingCalendarUnavailable(RuntimeError):
    pass


class ChinaTradingCalendar:
    def __init__(
        self,
        cache_path: Path,
        *,
        max_cache_age_days: int = 30,
        fetcher: CalendarFetcher | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._cache_path = cache_path
        self._max_cache_age = timedelta(days=max_cache_age_days)
        self._fetcher = fetcher or _fetch_akshare_calendar
        self._now = now
        self._dates: frozenset[date] = frozenset()
        self._fetched_at: datetime | None = None

    def is_trading_day(self, day: date) -> bool:
        self._ensure_loaded()
        if not self._dates:
            raise TradingCalendarUnavailable("trading calendar is unavailable")
        return day in self._dates

    def _ensure_loaded(self) -> None:
        if not self._dates:
            self._load_cache()
        if self._fetched_at is not None and self._now() - self._fetched_at <= self._max_cache_age:
            return
        try:
            dates = frozenset(self._fetcher())
        except Exception as exc:
            raise TradingCalendarUnavailable(f"cannot refresh trading calendar: {exc}") from exc
        if not dates:
            raise TradingCalendarUnavailable("calendar provider returned no dates")
        self._dates = dates
        self._fetched_at = self._now()
        self._save_cache()

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


def _fetch_akshare_calendar() -> Iterable[date]:
    import akshare as ak

    frame = ak.tool_trade_date_hist_sina()
    if "trade_date" not in frame.columns:
        return ()
    return tuple(
        value.date() if hasattr(value, "date") else date.fromisoformat(str(value)[:10]) for value in frame["trade_date"]
    )


__all__ = ["ChinaTradingCalendar", "TradingCalendarUnavailable"]
