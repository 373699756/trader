"""Explicit offline published inputs for download and recommendation contracts."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.history_reference import HistoryStEvidence
from trader.download.domain.prepared_reference import PreparedReference


def PreparedFixture(
    observed_at: datetime,
    securities: tuple[BaoStockSecurity, ...] | None = None,
    *,
    open_dates: tuple[date, ...] | None = None,
    industry_version: str | None = None,
) -> PreparedReference:
    at = observed_at.astimezone(ZoneInfo("Asia/Shanghai"))
    population = (
        securities
        if securities is not None
        else (BaoStockSecurity("600001", "测试股份", "main", date(1999, 1, 1), None, "fixture-official"),)
    )
    dates = (
        open_dates
        if open_dates is not None
        else tuple(day for offset in range(-3100, 367) if (day := at.date() + timedelta(days=offset)).weekday() < 5)
    )
    return PreparedReference(
        tuple(sorted(population, key=lambda item: item.code)),
        tuple(
            HistoryStEvidence(item.code, at.date(), "clear") for item in sorted(population, key=lambda item: item.code)
        ),
        at,
        dates,
        industry_version,
    )


__all__ = ["PreparedFixture"]
