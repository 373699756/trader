"""AkShare calendar SDK and DataFrame decoding have one owner."""

from __future__ import annotations

from datetime import date

from trader.infra.isolated_call import IsolatedCall


class AkshareCalendarTimeoutError(TimeoutError):
    """Identify the bounded calendar preparation call without exposing SDK details."""

    def __init__(self, timeout_seconds: float) -> None:
        super().__init__("akshare_sina_calendar_timeout")
        self.timeout_seconds = timeout_seconds


def FetchAkshareCalendar(timeout_seconds: float, *, execute: IsolatedCall) -> tuple[date, ...]:
    try:
        return execute(_read_calendar, timeout_seconds)
    except TimeoutError as exc:
        raise AkshareCalendarTimeoutError(timeout_seconds) from exc


def _read_calendar() -> tuple[date, ...]:
    import akshare

    frame = akshare.tool_trade_date_hist_sina()
    if "trade_date" not in frame.columns:
        raise ValueError("calendar_schema_invalid")
    return tuple(sorted({date.fromisoformat(str(value)[:10]) for value in frame["trade_date"]}))


__all__ = ["AkshareCalendarTimeoutError", "FetchAkshareCalendar"]
