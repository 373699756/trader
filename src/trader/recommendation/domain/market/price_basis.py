"""Verify an observed raw-price coordinate against a frozen paired history day."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from trader.recommendation.domain.market.models import MarketQuote

if TYPE_CHECKING:
    from trader.recommendation.domain.publication.preview import PreviewContext

_SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True, slots=True)
class HistoryPriceAnchor:
    trade_date: date
    raw_close: float
    qfq_close: float

    def __post_init__(self) -> None:
        if type(self.trade_date) is not date or not all(_positive(value) for value in (self.raw_close, self.qfq_close)):
            raise ValueError("history_price_anchor_invalid")


@dataclass(frozen=True, slots=True)
class HistoryPriceBasis:
    market_trade_date: date
    previous_trade_date: date
    anchors: tuple[HistoryPriceAnchor, ...]
    source_identity: str

    def __post_init__(self) -> None:
        dates = tuple(anchor.trade_date for anchor in self.anchors)
        if (
            type(self.market_trade_date) is not date
            or type(self.previous_trade_date) is not date
            or self.previous_trade_date >= self.market_trade_date
            or not self.source_identity.strip()
            or dates != tuple(sorted(set(dates)))
            or any(day not in (self.previous_trade_date, self.market_trade_date) for day in dates)
        ):
            raise ValueError("history_price_basis_invalid")


def ResolveHistoryPriceScale(
    quote: MarketQuote,
    basis: HistoryPriceBasis | None,
    *,
    observed_at: datetime,
    historical_closes: Mapping[date, float],
    preview_context: PreviewContext | None = None,
) -> float | None:
    """Return verified raw/qfq scale; an unexplained ex-rights reference stays pending."""
    if (
        basis is None or quote.is_suspended
        or quote.source_time.utcoffset() is None or observed_at.utcoffset() is None
    ):
        return None
    source = quote.source_time.astimezone(_SHANGHAI)
    if source > observed_at or source.date() != basis.market_trade_date:
        return None
    if preview_context is not None and (
        preview_context.market_trade_date != basis.market_trade_date
        or not preview_context.accepts_quote(source, observed_at)
    ):
        return None
    anchors = {anchor.trade_date: anchor for anchor in basis.anchors}
    # A completed D close can establish the new coordinate without inventing
    # an adjustment factor from a changed previous-close reference.
    current = anchors.get(basis.market_trade_date)
    if current is not None and source.time() >= time(15, 0) and _same_price(quote.price, current.raw_close):
        return _scale(current, historical_closes)
    previous = anchors.get(basis.previous_trade_date)
    if previous is None or not _same_price(quote.previous_close, previous.raw_close):
        return None
    return _scale(previous, historical_closes)


def _scale(anchor: HistoryPriceAnchor, historical_closes: Mapping[date, float]) -> float | None:
    close = historical_closes.get(anchor.trade_date)
    if close is None or not math.isclose(close, anchor.qfq_close, rel_tol=1e-10, abs_tol=1e-10):
        return None
    scale = anchor.raw_close / anchor.qfq_close
    return scale if math.isfinite(scale) and scale > 0 else None


def _positive(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def _same_price(value: float | None, expected: float) -> bool:
    # A-share quotes have a one-cent tick. Permit only its rounding interval.
    return _positive(value) and value is not None and abs(value - expected) <= 0.005000001
