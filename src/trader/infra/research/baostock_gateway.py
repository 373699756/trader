"""BaoStock SDK row translation for the offline daily-history archive."""

from __future__ import annotations

import platform
from collections.abc import Sequence
from datetime import date, timedelta
from typing import Protocol

from trader.domain.research.baostock_daily import (
    BaoStockAdjustment,
    BaoStockBoard,
    BaoStockCalendar,
    BaoStockCodeBatch,
    BaoStockCodeDownload,
    BaoStockDailyFact,
    BaoStockDailySide,
    BaoStockDailySpec,
    BaoStockIndustryInterval,
    BaoStockSecurity,
    BaoStockSourceVersions,
    BaoStockTradingStatus,
    join_baostock_daily_sides,
)

_DAILY_FIELDS = "date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,tradestatus,pctChg,isST"


class BaoStockRowResult(Protocol):
    error_code: str
    error_msg: str
    fields: Sequence[str]

    def next(self) -> bool: ...

    def get_row_data(self) -> Sequence[str]: ...


class BaoStockSdkPort(Protocol):
    __version__: str

    def query_trade_dates(self, *, start_date: str, end_date: str) -> BaoStockRowResult: ...

    def query_stock_basic(self) -> BaoStockRowResult: ...

    def query_stock_industry(self, *, code: str = "", date: str = "") -> BaoStockRowResult: ...

    def query_history_k_data_plus(  # noqa: PLR0913 - exact third-party SDK signature
        self,
        code: str,
        fields: str,
        start_date: str,
        end_date: str,
        *,
        frequency: str,
        adjustflag: str,
    ) -> BaoStockRowResult: ...


class BaoStockRowGateway:
    """Translate an already-owned SDK session using only next/get_row_data."""

    def __init__(
        self,
        sdk: BaoStockSdkPort,
        *,
        python_version: str | None = None,
        dependency_versions: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self._sdk = sdk
        self._versions = BaoStockSourceVersions(
            sdk.__version__,
            python_version or platform.python_version(),
            dependency_versions,
        )

    def source_versions(self) -> BaoStockSourceVersions:
        return self._versions

    def fetch_calendar(self, spec: BaoStockDailySpec) -> BaoStockCalendar:
        lookback = spec.source_cutoff - timedelta(days=max(730, spec.sessions * 2))
        result = self._sdk.query_trade_dates(
            start_date=lookback.isoformat(),
            end_date=spec.source_cutoff.isoformat(),
        )
        rows = _result_rows(result, "calendar_query_failed")
        dates = tuple(
            date.fromisoformat(row["calendar_date"])
            for row in rows
            if row.get("is_trading_day") == "1" and row.get("calendar_date")
        )
        if len(dates) < spec.sessions:
            raise ValueError("BaoStock calendar returned fewer sessions than requested")
        return BaoStockCalendar(tuple(sorted(set(dates)))[-spec.sessions :])

    def fetch_universe(self, spec: BaoStockDailySpec) -> tuple[BaoStockSecurity, ...]:
        rows = _result_rows(self._sdk.query_stock_basic(), "security_query_failed")
        securities: list[BaoStockSecurity] = []
        for row in rows:
            if row.get("type") != "1":
                continue
            source_code = row.get("code", "")
            board = _board(source_code)
            if board is None:
                continue
            listed = _date(row.get("ipoDate"), "BaoStock listing date is missing")
            delisted = _optional_date(row.get("outDate"))
            if listed > spec.source_cutoff:
                continue
            securities.append(
                BaoStockSecurity(
                    source_code.split(".")[-1],
                    row.get("code_name", ""),
                    board,
                    listed,
                    delisted,
                    self._versions.sdk_version,
                )
            )
        ordered = tuple(sorted(securities, key=lambda item: item.code))
        if not ordered or len({item.code for item in ordered}) != len(ordered):
            raise ValueError("BaoStock security master is empty or duplicated")
        return ordered

    def fetch_code_batch(
        self,
        spec: BaoStockDailySpec,
        security: BaoStockSecurity,
        calendar: BaoStockCalendar,
    ) -> BaoStockCodeBatch:
        return self.fetch_code_download(spec, security, calendar).batch

    def fetch_code_download(
        self,
        spec: BaoStockDailySpec,
        security: BaoStockSecurity,
        calendar: BaoStockCalendar,
    ) -> BaoStockCodeDownload:
        expected = calendar.expected_dates(security)
        if not expected:
            return BaoStockCodeDownload(BaoStockCodeBatch(security.code, ()), ())
        raw, facts, raw_nulls, raw_future = self._daily_sides(spec, security, "unadjusted", "3", expected)
        qfq, _, qfq_nulls, qfq_future = self._daily_sides(spec, security, "qfq", "2", expected)
        batch = join_baostock_daily_sides(
            security.code,
            expected,
            raw,
            qfq,
            null_rows=raw_nulls + qfq_nulls,
        )
        if raw_future + qfq_future == 0:
            return BaoStockCodeDownload(batch, facts)
        return BaoStockCodeDownload(
            BaoStockCodeBatch(
                batch.code,
                batch.cells,
                batch.duplicate_rows,
                batch.null_rows,
                batch.out_of_window_rows,
                raw_future + qfq_future,
                batch.failure_reasons,
            ),
            facts,
        )

    def fetch_industry_intervals(
        self,
        spec: BaoStockDailySpec,
        calendar: BaoStockCalendar,
        universe: tuple[BaoStockSecurity, ...],
    ) -> tuple[BaoStockIndustryInterval, ...]:
        del spec
        snapshots = _industry_snapshot_dates(calendar.open_dates)
        allowed = {item.source_code: item.code for item in universe}
        observations: dict[str, list[tuple[date, str, str]]] = {item.code: [] for item in universe}
        for snapshot_date in snapshots:
            rows = _result_rows(
                self._sdk.query_stock_industry(code="", date=snapshot_date.isoformat()),
                "industry_query_failed",
            )
            for row in rows:
                code = allowed.get(row.get("code", ""))
                industry = row.get("industry", "").strip()
                classification = row.get("industryClassification", "").strip()
                if code is None or not industry or not classification:
                    continue
                observations[code].append((snapshot_date, industry, classification))
        intervals: list[BaoStockIndustryInterval] = []
        for code, values in observations.items():
            compressed: list[tuple[date, str, str]] = []
            for value in values:
                if not compressed or value[1:] != compressed[-1][1:]:
                    compressed.append(value)
            for index, (effective_from, industry, classification) in enumerate(compressed):
                effective_to = compressed[index + 1][0] if index + 1 < len(compressed) else None
                intervals.append(BaoStockIndustryInterval(code, effective_from, effective_to, industry, classification))
        return tuple(sorted(intervals, key=lambda item: (item.code, item.effective_from)))

    def _daily_sides(
        self,
        spec: BaoStockDailySpec,
        security: BaoStockSecurity,
        adjustment: BaoStockAdjustment,
        adjustflag: str,
        expected: tuple[date, ...],
    ) -> tuple[tuple[BaoStockDailySide, ...], tuple[BaoStockDailyFact, ...], int, int]:
        result = self._sdk.query_history_k_data_plus(
            security.source_code,
            _DAILY_FIELDS,
            expected[0].isoformat(),
            expected[-1].isoformat(),
            frequency="d",
            adjustflag=adjustflag,
        )
        rows = _result_rows(result, f"{adjustment}_daily_query_failed")
        sides: list[BaoStockDailySide] = []
        facts: list[BaoStockDailyFact] = []
        null_rows = future_rows = 0
        for row in rows:
            try:
                trade_date = _date(row.get("date"), "BaoStock daily date is missing")
                if trade_date > spec.source_cutoff:
                    future_rows += 1
                    continue
                if row.get("code") != security.source_code or row.get("adjustflag") != adjustflag:
                    raise ValueError("BaoStock daily row identity is invalid")
                side = _daily_side(security.code, trade_date, adjustment, row)
                if adjustment == "unadjusted":
                    facts.append(BaoStockDailyFact(security.code, trade_date, _is_st(row.get("isST"))))
            except (TypeError, ValueError):
                null_rows += 1
                continue
            sides.append(side)
        return tuple(sides), tuple(facts), null_rows, future_rows


def _industry_snapshot_dates(open_dates: tuple[date, ...]) -> tuple[date, ...]:
    if not open_dates:
        return ()
    values = [open_dates[0]]
    for day in open_dates[1:-1]:
        previous = values[-1]
        if (
            day.year != previous.year
            and day.month >= 1
            or day.year == previous.year
            and day.month >= previous.month + 6
        ):
            values.append(day)
    if open_dates[-1] != values[-1]:
        values.append(open_dates[-1])
    return tuple(values)


def _result_rows(result: BaoStockRowResult, error_code: str) -> tuple[dict[str, str], ...]:
    if result.error_code != "0":
        raise RuntimeError(_supplier_failure_code(result.error_code, error_code))
    fields = tuple(result.fields)
    if not fields or len(set(fields)) != len(fields):
        raise ValueError("BaoStock result fields are empty or duplicated")
    rows: list[dict[str, str]] = []
    while result.next():
        values = tuple(result.get_row_data())
        if len(values) != len(fields):
            raise ValueError("BaoStock result row width is invalid")
        rows.append(dict(zip(fields, values, strict=True)))
    if result.error_code != "0":
        raise RuntimeError(_supplier_failure_code(result.error_code, error_code))
    return tuple(rows)


def _supplier_failure_code(vendor_code: str, fallback: str) -> str:
    if vendor_code == "10001011":
        return "supplier_query_failed_blacklisted"
    return fallback


def _daily_side(
    code: str,
    trade_date: date,
    adjustment: BaoStockAdjustment,
    row: dict[str, str],
) -> BaoStockDailySide:
    status = _trading_status(row.get("tradestatus"))
    return BaoStockDailySide(
        code=code,
        trade_date=trade_date,
        adjustment=adjustment,
        open_price=_optional_float(row.get("open")),
        high_price=_optional_float(row.get("high")),
        low_price=_optional_float(row.get("low")),
        close_price=_optional_float(row.get("close")),
        volume=_optional_float(row.get("volume")),
        amount=_optional_float(row.get("amount")),
        preclose=_optional_float(row.get("preclose")) if adjustment == "unadjusted" else None,
        pct_change=_optional_ratio(row.get("pctChg")) if adjustment == "unadjusted" else None,
        turnover=_optional_ratio(row.get("turn")) if adjustment == "unadjusted" else None,
        trading_status=status,
    )


def _trading_status(value: str | None) -> BaoStockTradingStatus:
    if value == "1":
        return "trading"
    if value == "0":
        return "suspended"
    raise ValueError("BaoStock trading status is invalid")


def _is_st(value: str | None) -> bool:
    if value == "1":
        return True
    if value == "0":
        return False
    raise ValueError("BaoStock ST status is invalid")


def _optional_float(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    return float(value)


def _optional_ratio(value: str | None) -> float | None:
    number = _optional_float(value)
    return None if number is None else number / 100.0


def _date(value: str | None, message: str) -> date:
    if value is None or not value.strip():
        raise ValueError(message)
    return date.fromisoformat(value)


def _optional_date(value: str | None) -> date | None:
    return None if value is None or not value.strip() else date.fromisoformat(value)


def _board(source_code: str) -> BaoStockBoard | None:
    if source_code.startswith("sh.688") or source_code.startswith("sh.689"):
        return "star"
    if source_code.startswith("sz.300") or source_code.startswith("sz.301"):
        return "chinext"
    main_prefixes = ("sh.600", "sh.601", "sh.603", "sh.605", "sz.000", "sz.001", "sz.002", "sz.003")
    return "main" if source_code.startswith(main_prefixes) else None


__all__ = ["BaoStockRowGateway", "BaoStockRowResult", "BaoStockSdkPort"]
