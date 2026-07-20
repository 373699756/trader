"""Shared helpers for normalizing raw market-data payloads."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from trader.domain.models import MarketQuote


class QuoteNormalizer(Protocol):
    """Normalize a raw row-like payload into one immutable MarketQuote."""

    def __call__(self, raw: Mapping[str, object], received_at: datetime, /) -> MarketQuote | None: ...


def to_float(raw: object) -> float | None:
    """Parse an arbitrary object as finite float, returning None on invalid input."""

    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return value


def normalize_quotes(
    rows: Iterable[Mapping[str, object]],
    received_at: datetime,
    *,
    normalizer: QuoteNormalizer,
) -> tuple[MarketQuote, ...]:
    """Normalize each payload row with a normalizer and drop invalid rows."""

    normalized: list[MarketQuote] = []
    for row in rows:
        try:
            quote = normalizer(row, received_at)
        except (TypeError, ValueError):
            continue
        if quote is not None:
            normalized.append(quote)
    return tuple(normalized)


def _require_timezone(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


@dataclass(frozen=True)
class MarketQuoteInput:
    """Canonical transport shape for quoting normalized adapters."""

    code: str
    name: str
    price: float | None
    previous_close: float | None
    open_price: float | None
    high: float | None
    low: float | None
    pct_change: float | None
    change_5m: float | None
    speed: float | None
    volume_ratio: float | None
    turnover_rate: float | None
    amount: float | None
    amplitude: float | None
    market_cap: float | None
    industry: str
    source: str
    source_time: datetime
    received_time: datetime
    data_version: str
    is_st: bool = False
    is_suspended: bool = False
    is_one_price_limit: bool = False
    is_blacklisted: bool = False
    has_major_regulatory_risk: bool = False
    cross_source_deviation_pct: float | None = None
    cross_source_verified: bool = True

    def __post_init__(self) -> None:
        code = self.code.strip()
        if code != self.code:
            object.__setattr__(self, "code", code)
        source = self.source.strip()
        if source != self.source:
            object.__setattr__(self, "source", source)
        data_version = self.data_version.strip()
        if data_version != self.data_version:
            object.__setattr__(self, "data_version", data_version)

        if len(code) != 6 or not code.isdigit():
            raise ValueError("MarketQuoteInput.code must be a 6-digit code")
        if not source:
            raise ValueError("MarketQuoteInput.source must not be empty")
        if not data_version:
            raise ValueError("MarketQuoteInput.data_version must not be empty")
        _require_timezone(self.source_time, "MarketQuoteInput.source_time")
        _require_timezone(self.received_time, "MarketQuoteInput.received_time")


def build_market_quote(values: MarketQuoteInput) -> MarketQuote:
    """Build a MarketQuote from canonicalized values."""

    return MarketQuote(
        code=values.code,
        name=values.name,
        price=values.price,
        previous_close=values.previous_close,
        open_price=values.open_price,
        high=values.high,
        low=values.low,
        pct_change=values.pct_change,
        change_5m=values.change_5m,
        speed=values.speed,
        volume_ratio=values.volume_ratio,
        turnover_rate=values.turnover_rate,
        amount=values.amount,
        amplitude=values.amplitude,
        market_cap=values.market_cap,
        industry=values.industry,
        source=values.source,
        source_time=values.source_time,
        received_time=values.received_time,
        data_version=values.data_version,
        is_st=values.is_st,
        is_suspended=values.is_suspended,
        is_one_price_limit=values.is_one_price_limit,
        is_blacklisted=values.is_blacklisted,
        has_major_regulatory_risk=values.has_major_regulatory_risk,
        cross_source_deviation_pct=values.cross_source_deviation_pct,
        cross_source_verified=values.cross_source_verified,
    )


__all__ = ["MarketQuoteInput", "QuoteNormalizer", "build_market_quote", "normalize_quotes", "to_float"]
