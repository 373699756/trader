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
        payload = execute(_read_calendar, timeout_seconds)
        return tuple(date.fromisoformat(value) for value in payload.decode("ascii").splitlines())
    except TimeoutError as exc:
        raise AkshareCalendarTimeoutError(timeout_seconds) from exc


def _read_calendar() -> bytes:
    import akshare

    frame = akshare.tool_trade_date_hist_sina()
    if "trade_date" not in frame.columns:
        raise ValueError("calendar_schema_invalid")
    days = sorted({date.fromisoformat(str(value)[:10]) for value in frame["trade_date"]})
    return "\n".join(day.isoformat() for day in days).encode("ascii")


__all__ = ["AkshareCalendarTimeoutError", "FetchAkshareCalendar"]
