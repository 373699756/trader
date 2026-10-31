"""Tencent full-market and targeted quote adapter."""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import as_completed
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import TypedDict
from zoneinfo import ZoneInfo

from typing_extensions import NotRequired, Unpack

from trader.download.infra.tencent_daily import FetchTencentDailyRows, TencentDailyQuery, TencentDailyRow
from trader.infra.http_contracts import HttpConnectionError, HttpError, HttpStatusError, HttpTimeout, HttpTransport
from trader.infra.market_data.history.history import DailyBar, PriceAdjustment
from trader.infra.workers import (
    BoundedExecutor,
    InjectedExecutor,
    SubmitOrReject,
)
from trader.recommendation.domain.market.models import MarketQuote
from trader.recommendation.infra.market_data.outcome_history import PairOutcomeHistory
from trader.recommendation.infra.normalization.quote import (
    BuildMarketQuote,
    MarketQuoteInput,
    NormalizeQuotes,
    ParseVolume,
    ToFloat,
)
from trader.recommendation.domain.outcomes.models import OutcomeBar

SessionFactory = Callable[[], HttpTransport]
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_QUOTE_SHARD_SIZE = 120
_QUOTE_MAX_CONCURRENCY = 3


class _TencentClientOptions(TypedDict):
    timeout_seconds: float
    session_factory: SessionFactory
    worker_pool: BoundedExecutor | None
    cancel_requested: NotRequired[Callable[[], bool]]
    wall_clock: NotRequired[Callable[[], datetime]]
    monotonic: NotRequired[Callable[[], float]]


class TencentClient:
    def __init__(self, **options: Unpack[_TencentClientOptions]) -> None:
        self._timeout_seconds = options["timeout_seconds"]
        self._session_factory = options["session_factory"]
        self._cancel_requested = options.get("cancel_requested", lambda: False)
        self._wall_clock = options.get("wall_clock", lambda: datetime.now(timezone.utc))
        self._monotonic = options.get("monotonic", time.monotonic)
        self._worker_pool = options["worker_pool"]

    def fetch_quotes(
        self,
        codes: Sequence[str],
        now: datetime | None = None,
        *,
        timeout_seconds: float | None = None,
        deadline: datetime | None = None,
        cancel_event: threading.Event | None = None,
        request_attempts: int = 1,
    ) -> tuple[MarketQuote, ...]:
        if type(request_attempts) is not int or not 1 <= request_attempts <= 5:
            raise ValueError("Tencent quote attempts must be an integer from 1 to 5")
        normalized = tuple(sorted({code for code in codes if len(code) == 6 and code.isdigit()}))
        if not normalized:
            return ()
        monotonic_deadline = (
            self._monotonic() + max(0.0, (deadline - self._wall_clock()).total_seconds())
            if deadline is not None
            else None
        )
        self._ensure_running(monotonic_deadline, cancel_event)
        shards = tuple(
            normalized[offset : offset + _QUOTE_SHARD_SIZE] for offset in range(0, len(normalized), _QUOTE_SHARD_SIZE)
        )
        quotes: list[MarketQuote] = []
        failures: list[BaseException] = []
        pool = InjectedExecutor(self._worker_pool)
        for offset in range(0, len(shards), _QUOTE_MAX_CONCURRENCY):
            self._ensure_running(monotonic_deadline, cancel_event)
            wave = shards[offset : offset + _QUOTE_MAX_CONCURRENCY]
            futures = {
                SubmitOrReject(
                    pool,
                    self._fetch_quote_shard,
                    shard,
                    now,
                    timeout_seconds,
                    monotonic_deadline,
                    cancel_event,
                    request_attempts,
                ): shard
                for shard in wave
            }
            for future in as_completed(futures):
                try:
                    quotes.extend(future.result())
                except (OSError, RuntimeError, HttpError) as exc:
                    failures.append(exc)
        self._ensure_running(monotonic_deadline, cancel_event)
        if not quotes:
            if failures:
                raise RuntimeError("all Tencent quote shards failed") from failures[0]
            raise RuntimeError("tencent returned no usable candidate quotes")
        return tuple(sorted(quotes, key=lambda quote: quote.code))

    def fetch_market(
        self,
        codes: Sequence[str],
        *,
        deadline: datetime | None = None,
        cancel_event: threading.Event | None = None,
        request_attempts: int = 1,
    ) -> tuple[MarketQuote, ...]:
        requested = tuple(sorted(set(codes)))
        if not requested:
            raise RuntimeError("official market universe is unavailable")
        quotes = self.fetch_quotes(
            requested, deadline=deadline, cancel_event=cancel_event, request_attempts=request_attempts
        )
        if len({quote.code for quote in quotes}) < len(requested) * 0.99:
            raise RuntimeError("tencent full-market quote coverage is incomplete")
        completed_at = self._wall_clock()
        return tuple(
            replace(
                quote,
                data_version=f"tencent-market:{int(completed_at.timestamp())}",
            )
            for quote in quotes
        )

    def _fetch_quote_shard(
        self,
        codes: tuple[str, ...],
        received_at: datetime | None,
        timeout_seconds: float | None,
        monotonic_deadline: float | None,
        cancel_event: threading.Event | None,
        request_attempts: int,
    ) -> tuple[MarketQuote, ...]:
        for attempt in range(request_attempts):
            try:
                return self._request_quote_shard(
                    codes,
                    received_at,
                    timeout_seconds,
                    monotonic_deadline,
                    cancel_event,
                )
            except (HttpConnectionError, HttpTimeout, HttpStatusError) as exc:
                if attempt + 1 == request_attempts:
                    raise
                if isinstance(exc, HttpStatusError) and exc.status_code not in {408, 429, 500, 502, 503, 504}:
                    raise
        raise AssertionError("validated Tencent quote attempts exhausted without a result")

    def _request_quote_shard(
        self,
        codes: tuple[str, ...],
        received_at: datetime | None,
        timeout_seconds: float | None,
        monotonic_deadline: float | None,
        cancel_event: threading.Event | None,
    ) -> tuple[MarketQuote, ...]:
        self._ensure_running(monotonic_deadline, cancel_event)
        remaining = monotonic_deadline - self._monotonic() if monotonic_deadline is not None else None
        request_timeout = self._timeout_seconds
        if timeout_seconds is not None:
            request_timeout = min(request_timeout, max(0.05, timeout_seconds))
        if remaining is not None:
            if remaining <= 0:
                raise RuntimeError("tencent deadline exceeded")
            request_timeout = min(request_timeout, remaining)
        with self._session_factory() as session:
            response = session.get(
                "https://qt.gtimg.cn/q=" + ",".join(_symbol(code) for code in codes),
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"},
                timeout=request_timeout,
                deadline=monotonic_deadline,
                cancel_requested=lambda: (
                    self._cancel_requested() or (cancel_event is not None and cancel_event.is_set())
                ),
            )
            response.raise_for_status()
            text = response.content.decode("gb18030", errors="replace")
        self._ensure_running(monotonic_deadline, cancel_event)
        received_at = received_at or self._wall_clock()
        return NormalizeQuotes(
            (
                {str(index): value for index, value in enumerate(payload.split("~"))}
                for payload in re.findall(r'v_[^=]+="([^"]*)";', text)
            ),
            received_at,
            normalizer=lambda row, now: _parse_payload(row, now, set(codes)),
        )

    def fetch_history(self, code: str, *, days: int = 90, history_host: str = "proxy") -> tuple[DailyBar, ...]:
        rows = self._fetch_history_rows(
            code,
            days=days,
            history_host=history_host,
            adjustment=PriceAdjustment.QFQ,
        )
        return _history_bars(rows, days=days, adjustment=PriceAdjustment.QFQ)

    def fetch_outcome_history(
        self,
        code: str,
        *,
        days: int = 61,
        history_host: str = "proxy",
    ) -> tuple[OutcomeBar, ...]:
        qfq_rows = self._fetch_history_rows(
            code,
            days=days,
            history_host=history_host,
            adjustment=PriceAdjustment.QFQ,
        )
        raw_rows = self._fetch_history_rows(
            code,
            days=days,
            history_host=history_host,
            adjustment=PriceAdjustment.RAW,
        )
        return PairOutcomeHistory(
            _history_bars(qfq_rows, days=days, adjustment=PriceAdjustment.QFQ),
            _history_bars(raw_rows, days=days, adjustment=PriceAdjustment.RAW),
        )

    def _fetch_history_rows(
        self,
        code: str,
        *,
        days: int,
        history_host: str,
        adjustment: PriceAdjustment,
    ) -> tuple[TencentDailyRow, ...]:
        if len(code) != 6 or not code.isdigit() or not code.startswith(("0", "3", "6")):
            return ()
        if history_host not in {"proxy", "direct"}:
            raise ValueError("Tencent history host must be proxy or direct")
        self._ensure_running()
        end = self._wall_clock().astimezone(_SHANGHAI).date()
        query = TencentDailyQuery(
            _symbol(code),
            end - timedelta(days=max(days * 2, 180)),
            end,
            "qfq" if adjustment is PriceAdjustment.QFQ else "bfq",
            days,
        )
        try:
            with self._session_factory() as transport:
                rows = FetchTencentDailyRows(
                    transport,
                    query,
                    host=history_host,
                    timeout_seconds=self._timeout_seconds,
                    cancel_requested=self._cancel_requested,
                )
        except ValueError:
            return ()
        self._ensure_running()
        return rows

    def _ensure_running(
        self,
        monotonic_deadline: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        if self._worker_pool is not None and not self._worker_pool.is_running():
            raise RuntimeError("tencent worker executor stopped")
        if self._cancel_requested():
            raise RuntimeError("tencent source lane stopped")
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("tencent hedge cancelled")
        if monotonic_deadline is not None and self._monotonic() >= monotonic_deadline:
            raise RuntimeError("tencent deadline exceeded")


class TencentFullMarketSource:
    """Bind the official stock population to Tencent's code-based quote API."""

    def __init__(
        self, client: TencentClient, universe_codes: Callable[[], Sequence[str]], *, request_attempts: int
    ) -> None:
        self._client = client
        self._universe_codes = universe_codes
        self._request_attempts = request_attempts

    def fetch_market(
        self,
        *,
        deadline: datetime | None = None,
        cancel_event: threading.Event | None = None,
    ) -> tuple[MarketQuote, ...]:
        return self._client.fetch_market(
            self._universe_codes(),
            deadline=deadline,
            cancel_event=cancel_event,
            request_attempts=self._request_attempts,
        )

    def probe_market(self) -> None:
        codes = tuple(self._universe_codes()[:3])
        if not codes or not self._client.fetch_quotes(codes):
            raise RuntimeError("tencent recovery probe returned no quotes")


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
    amount = ToFloat(transaction[2]) if len(transaction) >= 3 else None
    price = ToFloat(fields.get("3"))
    return BuildMarketQuote(
        MarketQuoteInput(
            code=code,
            name=str(fields.get("1") or "").strip(),
            price=price,
            previous_close=ToFloat(fields.get("4")),
            open_price=ToFloat(fields.get("5")),
            high=ToFloat(fields.get("33")),
            low=ToFloat(fields.get("34")),
            pct_change=ToFloat(fields.get("32")),
            change_5m=None,
            speed=None,
            volume_ratio=ToFloat(fields.get("49")),
            turnover_rate=ToFloat(fields.get("38")),
            amount=amount,
            volume=ParseVolume(fields.get("6"), shares_per_unit=100.0),
            amplitude=ToFloat(fields.get("43")),
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
    value = ToFloat(raw)
    return value * 100_000_000.0 if value is not None and value > 0 else None


def _timestamp(raw: str, fallback: datetime) -> datetime:
    try:
        parsed = datetime.strptime(raw.strip(), "%Y%m%d%H%M%S")
    except ValueError:
        return fallback.astimezone(_SHANGHAI)
    return parsed.replace(tzinfo=_SHANGHAI)


def _history_bars(
    rows: Sequence[TencentDailyRow],
    *,
    days: int,
    adjustment: PriceAdjustment,
) -> tuple[DailyBar, ...]:
    parsed: list[tuple[date, float, float, float, float, float, float, float | None]] = []
    for raw in rows:
        trade_date = raw.day
        open_price, close, high, low = raw.open_price, raw.close, raw.high, raw.low
        volume, amount, turnover_rate = raw.volume, raw.amount, raw.turnover_rate
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
                volume=volume,
                amount=amount,
                pct_change=pct_change,
                turnover_rate=turnover_rate,
                adjustment=adjustment,
                source="tencent",
            )
        )
        previous_close = close
    return tuple(bars[-max(1, days) :])


__all__ = ["TencentClient", "TencentFullMarketSource"]
