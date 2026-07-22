"""Tencent targeted quote adapter for candidates and displayed TopK rows."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from trader.domain.market.models import MarketQuote
from trader.infra.market_data.history import DailyBar
from trader.infra.market_data.normalize import MarketQuoteInput, build_market_quote, normalize_quotes, to_float

SessionFactory = Callable[[], requests.Session]
_DIRECT_PROXIES = {"http": "", "https": "", "all": ""}
_SHANGHAI = ZoneInfo("Asia/Shanghai")


class TencentClient:
    def __init__(
        self,
        *,
        timeout_seconds: float,
        session_factory: SessionFactory = requests.Session,
        cancel_requested: Callable[[], bool] = lambda: False,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._session_factory = session_factory
        self._cancel_requested = cancel_requested
        self._wall_clock = wall_clock

    def fetch_quotes(self, codes: Sequence[str], now: datetime | None = None) -> tuple[MarketQuote, ...]:
        normalized = tuple(sorted({code for code in codes if len(code) == 6 and code.isdigit()}))
        if not normalized:
            return ()
        self._ensure_running()
        with self._session_factory() as session:
            response = session.get(
                "https://qt.gtimg.cn/q=" + ",".join(_symbol(code) for code in normalized),
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"},
                timeout=self._timeout_seconds,
                proxies=_DIRECT_PROXIES,
            )
            response.raise_for_status()
            text = response.content.decode("gb18030", errors="replace")
        self._ensure_running()
        received_at = now or self._wall_clock()
        quotes = normalize_quotes(
            (
                {str(index): value for index, value in enumerate(payload.split("~"))}
                for payload in re.findall(r'v_[^=]+="([^"]*)";', text)
            ),
            received_at,
            normalizer=lambda row, now: _parse_payload(row, now, set(normalized)),
        )
        if not quotes:
            raise RuntimeError("tencent returned no usable candidate quotes")
        return quotes

    def fetch_history(self, code: str, *, days: int = 90) -> tuple[DailyBar, ...]:
        if len(code) != 6 or not code.isdigit() or not code.startswith(("0", "3", "6")):
            return ()
        self._ensure_running()
        end = self._wall_clock().astimezone(_SHANGHAI).date()
        start = end - timedelta(days=max(days * 2, 180))
        symbol = _symbol(code)
        with self._session_factory() as session:
            response = session.get(
                "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
                params={
                    "_var": f"kline_dayqfq{end.year}",
                    "param": f"{symbol},day,{start.isoformat()},{end.isoformat()},640,qfq",
                    "r": "0.8205512681390605",
                },
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"},
                timeout=self._timeout_seconds,
                proxies=_DIRECT_PROXIES,
            )
            response.raise_for_status()
            text = response.text
        self._ensure_running()
        marker = text.find("={")
        if marker < 0:
            return ()
        try:
            payload = json.loads(text[marker + 1 :])
        except (TypeError, ValueError):
            return ()
        data = payload.get("data") if isinstance(payload, Mapping) else None
        stock = data.get(symbol) if isinstance(data, Mapping) else None
        rows = (stock.get("qfqday") or stock.get("day")) if isinstance(stock, Mapping) else None
        if not isinstance(rows, list):
            return ()
        return _history_bars(rows, days=days)

    def _ensure_running(self) -> None:
        if self._cancel_requested():
            raise RuntimeError("tencent source lane stopped")


def _parse_payload(fields: Mapping[str, object], received_at: datetime, requested: set[str]) -> MarketQuote | None:
    if len(fields) < 50:
        return None
    code = str(fields.get("2") or "").strip()
    if len(fields) < 50:
        return None
    if code not in requested:
        return None
    source_time = _timestamp(str(fields.get("30") or ""), received_at)
    transaction = str(fields.get("35") or "").split("/") if len(fields) > 35 else []
    amount = to_float(transaction[2]) if len(transaction) >= 3 else None
    price = to_float(fields.get("3"))
    return build_market_quote(
        MarketQuoteInput(
            code=code,
            name=str(fields.get("1") or "").strip(),
            price=price,
            previous_close=to_float(fields.get("4")),
            open_price=to_float(fields.get("5")),
            high=to_float(fields.get("33")),
            low=to_float(fields.get("34")),
            pct_change=to_float(fields.get("32")),
            change_5m=None,
            speed=None,
            volume_ratio=to_float(fields.get("49")),
            turnover_rate=to_float(fields.get("38")),
            amount=amount,
            amplitude=to_float(fields.get("43")),
            market_cap=None,
            industry="",
            source="tencent",
            source_time=source_time,
            received_time=received_at,
            data_version=f"tencent:{int(source_time.timestamp())}",
            is_st="ST" in str(fields.get("1") or "").upper() or "退" in str(fields.get("1") or ""),
            is_suspended=price is None or price <= 0,
        )
    )


def _symbol(code: str) -> str:
    return ("sh" if code.startswith("6") else "sz") + code


def _timestamp(raw: str, fallback: datetime) -> datetime:
    try:
        parsed = datetime.strptime(raw.strip(), "%Y%m%d%H%M%S")
    except ValueError:
        return fallback
    return parsed.replace(tzinfo=_SHANGHAI)


def _history_bars(rows: Sequence[object], *, days: int) -> tuple[DailyBar, ...]:
    parsed: list[tuple[date, float, float, float, float, float, float, float | None]] = []
    for raw in rows:
        if not isinstance(raw, list) or len(raw) < 9:
            continue
        try:
            trade_date = date.fromisoformat(str(raw[0]))
        except ValueError:
            continue
        open_price, close, high, low, volume, turnover_rate, amount = (
            to_float(raw[index]) for index in (1, 2, 3, 4, 5, 7, 8)
        )
        if (
            open_price is None
            or close is None
            or high is None
            or low is None
            or volume is None
            or amount is None
            or min(open_price, close, high, low) <= 0.0
            or volume < 0.0
            or amount <= 0.0
        ):
            continue
        parsed.append((trade_date, open_price, close, high, low, volume, amount, turnover_rate))
    parsed.sort(key=lambda item: item[0])
    bars: list[DailyBar] = []
    previous_close: float | None = None
    for trade_date, open_price, close, high, low, volume, amount, turnover_rate in parsed:
        pct_change = (close / previous_close - 1.0) * 100.0 if previous_close is not None else 0.0
        bars.append(
            DailyBar(
                trade_date=trade_date.isoformat(),
                open_price=open_price,
                close=close,
                high=high,
                low=low,
                volume=volume * 100.0,
                amount=amount * 10_000.0,
                pct_change=pct_change,
                turnover_rate=turnover_rate,
            )
        )
        previous_close = close
    return tuple(bars[-max(1, days) :])


__all__ = ["TencentClient"]
