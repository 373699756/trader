"""Eastmoney daily-history and targeted minute adapters."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Protocol, TypedDict, cast
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from typing_extensions import Unpack

import requests

from trader.infra.market_data.history.history import DailyBar, PriceAdjustment
from trader.infra.workers import BoundedExecutor
from trader.recommendation.domain.market.tail import MinuteBar
from trader.recommendation.infra.market_data.outcome_history import PairOutcomeHistory
from trader.recommendation.infra.normalization.quote import (
    ToFloat,
)
from trader.training.domain.evaluation.models import OutcomeBar


class JsonResponse(Protocol):
    def raise_for_status(self) -> None: ...

    def json(self) -> object: ...


SessionFactory = Callable[[], requests.Session]

_DIRECT_PROXIES = {"http": "", "https": "", "all": ""}
_REQUEST_ROUNDS = 2
_SHANGHAI = ZoneInfo("Asia/Shanghai")


class _EastmoneyRequiredOptions(TypedDict):
    timeout_seconds: float
    worker_pool: BoundedExecutor | None
    session_factory: SessionFactory


class _EastmoneyOptionalOptions(TypedDict, total=False):
    cancel_requested: Callable[[], bool]
    wall_clock: Callable[[], datetime]


class _EastmoneyOptions(_EastmoneyRequiredOptions, _EastmoneyOptionalOptions):
    pass


class EastmoneyClient:
    def __init__(
        self,
        **options: Unpack[_EastmoneyOptions],
    ) -> None:
        self._timeout_seconds = options["timeout_seconds"]
        self._worker_pool = options["worker_pool"]
        self._session_factory = options["session_factory"]
        self._cancel_requested = options.get("cancel_requested", lambda: False)
        self._wall_clock = options.get("wall_clock", lambda: datetime.now(timezone.utc))

    def fetch_history(self, code: str, *, days: int = 90, now: datetime | None = None) -> tuple[DailyBar, ...]:
        return self._fetch_daily_history(code, days=days, now=now, adjustment=PriceAdjustment.QFQ)

    def fetch_outcome_history(
        self,
        code: str,
        *,
        days: int = 61,
        now: datetime | None = None,
    ) -> tuple[OutcomeBar, ...]:
        qfq = self._fetch_daily_history(code, days=days, now=now, adjustment=PriceAdjustment.QFQ)
        raw = self._fetch_daily_history(code, days=days, now=now, adjustment=PriceAdjustment.RAW)
        return PairOutcomeHistory(qfq, raw)

    def _fetch_daily_history(
        self,
        code: str,
        *,
        days: int,
        now: datetime | None,
        adjustment: PriceAdjustment,
    ) -> tuple[DailyBar, ...]:
        end = (now or self._wall_clock()).date()
        start = end - timedelta(days=max(days * 2, 180))
        payload = self._get(
            ("push2his.eastmoney.com", "82.push2his.eastmoney.com", "7.push2his.eastmoney.com"),
            "/api/qt/stock/kline/get",
            {
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                "ut": "7eea3edcaed734bea9cbfc24409ed989",
                "klt": "101",
                "fqt": "1" if adjustment is PriceAdjustment.QFQ else "0",
                "secid": _secid(code),
                "beg": start.strftime("%Y%m%d"),
                "end": end.strftime("%Y%m%d"),
            },
            request_rounds=1,
        )
        klines = _object_mapping(payload.get("data")).get("klines")
        if not isinstance(klines, list):
            return ()
        bars: list[DailyBar] = []
        for raw in klines:
            parts = str(raw).split(",")
            if len(parts) < 9:
                continue
            values = [ToFloat(value) for value in parts[1:9]]
            if any(value is None for value in values):
                continue
            open_price, close, high, low, volume, amount, _amplitude, pct_change = cast(list[float], values)
            turnover_rate = ToFloat(parts[10]) if len(parts) > 10 else None
            bars.append(
                DailyBar(
                    trade_date=parts[0],
                    open_price=open_price,
                    close=close,
                    high=high,
                    low=low,
                    volume=volume,
                    amount=amount,
                    pct_change=pct_change,
                    turnover_rate=turnover_rate,
                    adjustment=adjustment,
                    source="eastmoney",
                )
            )
        return tuple(bars[-days:])

    def fetch_intraday_minutes(self, code: str, *, now: datetime | None = None) -> tuple[MinuteBar, ...]:
        observed_at = now or self._wall_clock()
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("intraday observation time must be timezone-aware")
        observed_local = observed_at.astimezone(_SHANGHAI)
        payload = self._get(
            ("push2his.eastmoney.com",),
            "/api/qt/stock/trends2/get",
            {
                "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
                "ut": "7eea3edcaed734bea9cbfc24409ed989",
                "ndays": "1",
                "iscr": "0",
                "secid": _secid(code),
            },
            request_rounds=1,
        )
        trends = _object_mapping(payload.get("data")).get("trends")
        if not isinstance(trends, list):
            return ()
        bars: list[MinuteBar] = []
        for raw in trends:
            parts = str(raw).split(",")
            if len(parts) < 8:
                continue
            try:
                source_time = datetime.strptime(parts[0], "%Y-%m-%d %H:%M").replace(tzinfo=_SHANGHAI)
            except ValueError:
                continue
            close = ToFloat(parts[2])
            volume = ToFloat(parts[5])
            if (
                source_time.date() != observed_local.date()
                or source_time > observed_local
                or close is None
                or close <= 0.0
            ):
                continue
            bars.append(
                MinuteBar(
                    source_time=source_time,
                    close=close,
                    volume=volume if volume is not None and volume >= 0.0 else None,
                    source="eastmoney_intraday",
                    received_time=observed_at,
                    data_version=f"eastmoney-intraday:{int(observed_at.timestamp())}",
                )
            )
        return tuple(bars)

    def _get(
        self,
        hosts: Sequence[str],
        path: str,
        params: Mapping[str, str],
        *,
        request_rounds: int = _REQUEST_ROUNDS,
    ) -> Mapping[str, object]:
        last_error: Exception | None = None
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
        for _round in range(max(1, request_rounds)):
            for host in hosts:
                self._ensure_running()
                try:
                    with self._session_factory() as request_session:
                        response = request_session.get(
                            f"https://{host}{path}",
                            params=dict(params),
                            headers=headers,
                            timeout=self._timeout_seconds,
                            proxies=_DIRECT_PROXIES,
                        )
                        response.raise_for_status()
                        payload = response.json()
                except (requests.RequestException, ValueError, OSError) as exc:
                    last_error = exc
                    continue
                self._ensure_running()
                if isinstance(payload, dict) and payload.get("data"):
                    return payload
                last_error = RuntimeError("eastmoney returned empty data")
        raise RuntimeError(f"eastmoney request failed: {last_error}") from last_error

    def _ensure_running(self) -> None:
        if self._worker_pool is not None and not self._worker_pool.is_running():
            raise RuntimeError("eastmoney worker executor stopped")
        if self._cancel_requested():
            raise RuntimeError("eastmoney source lane stopped")


def _secid(code: str) -> str:
    return f"1.{code}" if code.startswith(("5", "6", "9")) else f"0.{code}"


def _object_mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, dict) else {}


def _object_rows(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


__all__ = ["EastmoneyClient"]
