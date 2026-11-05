"""Read the provider's dated suspension list; zero turnover is never evidence."""

import json
from collections.abc import Callable, Mapping
from datetime import date, datetime, time, timedelta
from functools import partial
from typing import Protocol

from trader.download.domain.price_suspension import PriceSuspension, PriceSuspensionSnapshot
from trader.infra.http_contracts import HttpResponse

_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"


class SuspensionGet(Protocol):
    def __call__(
        self,
        url: str,
        *,
        params: Mapping[str, str],
        headers: Mapping[str, str],
        timeout: float,
    ) -> HttpResponse: ...


class SuspensionExecutor(Protocol):
    def __call__(
        self,
        operation: Callable[[], PriceSuspensionSnapshot],
        timeout_seconds: float,
        *,
        cancel_requested: Callable[[], bool],
    ) -> PriceSuspensionSnapshot: ...


class PriceSuspensionSource:
    def __init__(
        self,
        get: SuspensionGet,
        execute: SuspensionExecutor,
        cancel_requested: Callable[[], bool],
    ) -> None:
        self._get = get
        self._execute = execute
        self._cancel_requested = cancel_requested

    def fetch(self, target_date: date) -> PriceSuspensionSnapshot:
        return self._execute(
            partial(_fetch_price_suspensions, target_date, get=self._get, timeout_seconds=3.0),
            5.0,
            cancel_requested=self._cancel_requested,
        )


def _fetch_price_suspensions(
    target_date: date, *, get: SuspensionGet, timeout_seconds: float
) -> PriceSuspensionSnapshot:
    response = get(
        _URL,
        params={
            "reportName": "RPT_CUSTOM_SUSPEND_DATA_INTERFACE",
            "columns": "ALL",
            "pageSize": "500",
            "pageNumber": "1",
            "filter": f"(MARKET=\"全部\")(DATETIME='{target_date.isoformat()}')",
        },
        headers={"Referer": "https://data.eastmoney.com/tfpxx/"},
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    payload = json.loads(response.content)
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise ValueError("price_suspension_response_invalid")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise ValueError("price_suspension_response_invalid")
    rows = result.get("data")
    if (
        not isinstance(rows, list)
        or len(rows) > 500
        or type(result.get("count")) is not int
        or result["count"] != len(rows)
        or result.get("pages") not in (0, 1)
    ):
        raise ValueError("price_suspension_list_incomplete")
    intervals: list[PriceSuspension] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("price_suspension_row_invalid")
        code = row.get("SECURITY_CODE")
        if not isinstance(code, str) or len(code) != 6 or not code.isdigit():
            raise ValueError("price_suspension_row_invalid")
        # Partial-session halts must still be downloaded for their actual daily bar.
        if row.get("SUSPEND_EXPIRE") not in ("连续停牌", "停牌一天"):
            continue
        start = _timestamp(row.get("SUSPEND_START_TIME"))
        end = _timestamp(row.get("SUSPEND_END_TIME"))
        resume = _timestamp(row.get("PREDICT_RESUME_DATE"))
        if start is None or start.date() > target_date or start.time() > time(9, 30):
            continue
        last = target_date
        if end is not None:
            last = min(last, end.date() if end.time() >= time(15) else end.date() - timedelta(days=1))
        if resume is not None:
            last = min(last, resume.date() - timedelta(days=1))
        if start.date() <= last:
            intervals.append(PriceSuspension(code, start.date(), last))
    return PriceSuspensionSnapshot(target_date, tuple(sorted(intervals, key=lambda item: item.code)))


def _timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("price_suspension_date_invalid")
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise ValueError("price_suspension_date_invalid") from exc


__all__ = [
    "PriceSuspensionSource",
    "SuspensionExecutor",
    "SuspensionGet",
]
