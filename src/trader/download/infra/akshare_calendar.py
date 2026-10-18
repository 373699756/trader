"""AkShare calendar SDK and DataFrame decoding have one owner."""

from __future__ import annotations

from datetime import date

from trader.infra.isolated_call import IsolatedCall


def FetchAkshareCalendar(timeout_seconds: float, *, execute: IsolatedCall) -> tuple[date, ...]:
    return execute(_read_calendar, timeout_seconds)


def _read_calendar() -> tuple[date, ...]:
    import akshare

    frame = akshare.tool_trade_date_hist_sina()
    if "trade_date" not in frame.columns:
        raise ValueError("calendar_schema_invalid")
    return tuple(sorted({date.fromisoformat(str(value)[:10]) for value in frame["trade_date"]}))


__all__ = ["FetchAkshareCalendar"]
