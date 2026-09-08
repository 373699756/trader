"""Tencent targeted quote adapter for candidates and displayed TopK rows."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import as_completed
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from trader.application.runtime.workers import (
    BorrowExecutorOptions,
    BoundedExecutor,
    borrow_executor,
    submit_or_run_inline,
)
from trader.domain.market.models import MarketQuote
from trader.domain.outcome.models import OutcomeBar
from trader.infra.market_data.history.history import DailyBar, PriceAdjustment
from trader.infra.market_data.history.outcome_history import pair_outcome_history
from trader.infra.market_data.normalization.normalize import (
    MarketQuoteInput,
    build_market_quote,
    normalize_quotes,
    to_float,
)

SessionFactory = Callable[[], requests.Session]
_DIRECT_PROXIES = {"http": "", "https": "", "all": ""}
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_QUOTE_SHARD_SIZE = 120
_QUOTE_MAX_CONCURRENCY = 3
_HISTORY_ENDPOINTS = {
    "proxy": "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
    "direct": "https://web.ifzq.gtimg.cn/appstock/app/newfqkline/get",
}


class TencentClient:
    def __init__(
        self,
        *,
        timeout_seconds: float,
        session_factory: SessionFactory = requests.Session,
        cancel_requested: Callable[[], bool] = lambda: False,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        worker_pool: BoundedExecutor | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._session_factory = session_factory
        self._cancel_requested = cancel_requested
        self._wall_clock = wall_clock
        self._worker_pool = worker_pool

    def fetch_quotes(
        self,
        codes: Sequence[str],
        now: datetime | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> tuple[MarketQuote, ...]:
        normalized = tuple(sorted({code for code in codes if len(code) == 6 and code.isdigit()}))
        if not normalized:
            return ()
        self._ensure_running()
        received_at = now or self._wall_clock()
        shards = tuple(
            normalized[offset : offset + _QUOTE_SHARD_SIZE] for offset in range(0, len(normalized), _QUOTE_SHARD_SIZE)
        )
        quotes: list[MarketQuote] = []
        failures: list[BaseException] = []
        with borrow_executor(
            self._worker_pool,
            BorrowExecutorOptions(
                worker_count=min(_QUOTE_MAX_CONCURRENCY, len(shards)),
                queue_capacity=min(_QUOTE_MAX_CONCURRENCY, len(shards)),
                thread_name_prefix="tencent-quotes",
            ),
        ) as pool:
            for offset in range(0, len(shards), _QUOTE_MAX_CONCURRENCY):
                wave = shards[offset : offset + _QUOTE_MAX_CONCURRENCY]
                futures = {
                    submit_or_run_inline(
                        pool,
                        self._fetch_quote_shard,
                        shard,
                        received_at,
                        timeout_seconds,
                    ): shard
                    for shard in wave
                }
                for future in as_completed(futures):
                    try:
                        quotes.extend(future.result())
                    except (OSError, RuntimeError, requests.RequestException) as exc:
                        failures.append(exc)
        self._ensure_running()
        if not quotes:
            if failures:
                raise RuntimeError("all Tencent quote shards failed") from failures[0]
            raise RuntimeError("tencent returned no usable candidate quotes")
        return tuple(sorted(quotes, key=lambda quote: quote.code))

    def _fetch_quote_shard(
        self,
        codes: tuple[str, ...],
        received_at: datetime,
        timeout_seconds: float | None,
    ) -> tuple[MarketQuote, ...]:
        self._ensure_running()
        with self._session_factory() as session:
            response = session.get(
                "https://qt.gtimg.cn/q=" + ",".join(_symbol(code) for code in codes),
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"},
                timeout=(
                    self._timeout_seconds
                    if timeout_seconds is None
                    else min(self._timeout_seconds, max(0.05, timeout_seconds))
                ),
                proxies=_DIRECT_PROXIES,
            )
            response.raise_for_status()
            text = response.content.decode("gb18030", errors="replace")
        self._ensure_running()
        return normalize_quotes(
            (
                {str(index): value for index, value in enumerate(payload.split("~"))}
                for payload in re.findall(r'v_[^=]+="([^"]*)";', text)
            ),
            received_at,
            normalizer=lambda row, now: _parse_payload(row, now, set(codes)),
        )

    def fetch_history(self, code: str, *, days: int = 90, history_host: str = "proxy") -> tuple[DailyBar, ...]:
        stock = self._fetch_history_stock(
            code,
            days=days,
            history_host=history_host,
            adjustment=PriceAdjustment.QFQ,
        )
        rows = _qfq_rows(stock)
        if not isinstance(rows, list):
            return ()
        return _history_bars(rows, days=days, adjustment=PriceAdjustment.QFQ)

    def fetch_outcome_history(
        self,
        code: str,
        *,
        days: int = 61,
        history_host: str = "proxy",
    ) -> tuple[OutcomeBar, ...]:
        qfq_stock = self._fetch_history_stock(
            code,
            days=days,
            history_host=history_host,
            adjustment=PriceAdjustment.QFQ,
        )
        raw_stock = self._fetch_history_stock(
            code,
            days=days,
            history_host=history_host,
            adjustment=PriceAdjustment.RAW,
        )
        qfq_rows = _qfq_rows(qfq_stock)
        raw_rows = _raw_rows(raw_stock)
        if not isinstance(qfq_rows, list) or not isinstance(raw_rows, list):
            return ()
        return pair_outcome_history(
            _history_bars(qfq_rows, days=days, adjustment=PriceAdjustment.QFQ),
            _history_bars(raw_rows, days=days, adjustment=PriceAdjustment.RAW),
        )

    def _fetch_history_stock(
        self,
        code: str,
        *,
        days: int,
        history_host: str,
        adjustment: PriceAdjustment,
    ) -> object:
        if len(code) != 6 or not code.isdigit() or not code.startswith(("0", "3", "6")):
            return None
        self._ensure_running()
        try:
            history_endpoint = _HISTORY_ENDPOINTS[history_host]
        except KeyError as exc:
            raise ValueError("Tencent history host must be proxy or direct") from exc
        end = self._wall_clock().astimezone(_SHANGHAI).date()
        start = end - timedelta(days=max(days * 2, 180))
        symbol = _symbol(code)
        adjustment_mode = "qfq" if adjustment is PriceAdjustment.QFQ else "bfq"
        with self._session_factory() as session:
            response = session.get(
                history_endpoint,
                params={
                    "_var": f"kline_day{adjustment_mode}{end.year}",
                    "param": f"{symbol},day,{start.isoformat()},{end.isoformat()},640,{adjustment_mode}",
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
            return None
        try:
            payload = json.loads(text[marker + 1 :])
        except (TypeError, ValueError):
            return None
        data = payload.get("data") if isinstance(payload, Mapping) else None
        return data.get(symbol) if isinstance(data, Mapping) else None

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
            market_cap=_market_cap(fields.get("45")),
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


def _market_cap(raw: object) -> float | None:
    value = to_float(raw)
    return value * 100_000_000.0 if value is not None and value > 0 else None


def _timestamp(raw: str, fallback: datetime) -> datetime:
    try:
        parsed = datetime.strptime(raw.strip(), "%Y%m%d%H%M%S")
    except ValueError:
        return fallback
    return parsed.replace(tzinfo=_SHANGHAI)


def _history_bars(
    rows: Sequence[object],
    *,
    days: int,
    adjustment: PriceAdjustment,
) -> tuple[DailyBar, ...]:
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
                adjustment=adjustment,
                source="tencent",
            )
        )
        previous_close = close
    return tuple(bars[-max(1, days) :])


def _qfq_rows(stock: object) -> list[object] | None:
    if not isinstance(stock, Mapping):
        return None
    adjusted = stock.get("qfqday")
    if isinstance(adjusted, list):
        return adjusted
    raw = stock.get("day")
    if not isinstance(raw, list) or not raw:
        return None
    if all(_day_row_is_qfq_equivalent(item) for item in raw):
        return raw
    return None


def _raw_rows(stock: object) -> list[object] | None:
    if not isinstance(stock, Mapping):
        return None
    raw = stock.get("day")
    return raw if isinstance(raw, list) else None


def _day_row_is_qfq_equivalent(raw: object) -> bool:
    if not isinstance(raw, list) or len(raw) < 11:
        return False
    corporate_action = raw[6]
    first_adjustment = to_float(raw[9])
    second_adjustment = to_float(raw[10])
    return (
        isinstance(corporate_action, Mapping)
        and not corporate_action
        and first_adjustment == 0.0
        and second_adjustment == 0.0
    )


__all__ = ["TencentClient"]
