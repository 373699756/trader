"""Hard candidate filters with auditable failure details."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from trader.domain.models import Board, FeatureSnapshot, FilterAudit

FilterReason = FilterAudit


@dataclass(frozen=True)
class FilterResult:
    allowed: bool
    board: Board
    reasons: tuple[FilterAudit, ...]


def board_for_code(code: str) -> Board:
    normalized = code.strip()
    if len(normalized) != 6 or not normalized.isdigit():
        return Board.UNSUPPORTED
    if normalized.startswith(("000", "001", "002", "003", "600", "601", "603", "605")):
        return Board.MAIN
    if normalized.startswith(("300", "301")):
        return Board.CHINEXT
    if normalized.startswith(("688", "689")):
        return Board.STAR
    return Board.UNSUPPORTED


def hard_filter(snapshot: FeatureSnapshot, now: datetime, *, max_age_seconds: float) -> FilterResult:
    if not math.isfinite(max_age_seconds) or max_age_seconds < 0:
        raise ValueError("max_age_seconds must be finite and non-negative")
    quote = snapshot.quote
    board = board_for_code(quote.code)
    reasons: list[FilterAudit] = []

    def reject(code: str, threshold: str, actual: object) -> None:
        reasons.append(
            FilterAudit(
                stock_code=quote.code,
                filter_code=code,
                threshold=threshold,
                actual=_json_scalar(actual),
                source=quote.source,
                observed_at=quote.source_time,
            )
        )

    if board is Board.UNSUPPORTED:
        reject("unsupported_code", "supported Shanghai/Shenzhen board code", quote.code)
    if quote.is_st or "ST" in quote.name.upper() or "退" in quote.name:
        reject("st_or_delisting", "false", quote.name)
    if quote.is_suspended:
        reject("suspended", "false", True)
    if quote.price is None or not math.isfinite(quote.price) or quote.price <= 0:
        reject("invalid_price", "> 0", quote.price)
    if quote.amount is None or not math.isfinite(quote.amount) or quote.amount <= 0:
        reject("invalid_amount", "> 0", quote.amount)
    if quote.source_time.tzinfo is None or quote.source_time.utcoffset() is None:
        reject("invalid_quote_time", "timezone-aware", quote.source_time.isoformat())
    elif quote.source_time > now:
        reject("future_quote", "<= evaluation time", quote.source_time.isoformat())
    elif quote.age_seconds(now) > max_age_seconds:
        reject("stale_quote", f"<= {max_age_seconds}s", round(quote.age_seconds(now), 3))
    deviation = quote.cross_source_deviation_pct
    if deviation is not None and (not math.isfinite(deviation) or deviation < 0):
        reject("invalid_cross_source_deviation", "finite and >= 0", deviation)
    elif deviation is not None and deviation > 0.5 and not quote.cross_source_verified:
        reject("cross_source_deviation", "<= 0.5% or verified", deviation)
    median_amount = snapshot.values.get("amount_median_20d")
    if median_amount is None:
        reject("missing_liquidity_history", ">= 50000000", None)
    elif not math.isfinite(median_amount):
        reject("invalid_liquidity_history", ">= 50000000", median_amount)
    elif median_amount < 50_000_000:
        reject("insufficient_liquidity", ">= 50000000", median_amount)
    if quote.is_one_price_limit:
        reject("one_price_limit", "false", True)
    if quote.is_blacklisted:
        reject("blacklisted", "false", True)
    if quote.has_major_regulatory_risk:
        reject("major_regulatory_risk", "false", True)
    _reject_invalid_quote_structure(snapshot, reject)
    if quote.pct_change is None or not math.isfinite(quote.pct_change):
        reject("invalid_pct_change", "finite percentage points", quote.pct_change)
    else:
        if board is Board.MAIN and quote.pct_change > 8.0:
            reject("main_board_too_hot", "<= 8.00", quote.pct_change)
        if board in {Board.CHINEXT, Board.STAR} and quote.pct_change > 16.0:
            reject("growth_board_too_hot", "<= 16.00", quote.pct_change)

    return FilterResult(allowed=not reasons, board=board, reasons=tuple(reasons))


def _reject_invalid_quote_structure(
    snapshot: FeatureSnapshot,
    reject: Callable[[str, str, object], None],
) -> None:
    quote = snapshot.quote
    fields = {
        "previous_close": quote.previous_close,
        "open_price": quote.open_price,
        "high": quote.high,
        "low": quote.low,
    }
    for name, value in fields.items():
        if value is not None and (not math.isfinite(value) or value <= 0):
            reject("invalid_quote_structure", f"{name} finite and > 0", value)
            return
    if quote.high is not None and quote.low is not None and quote.high < quote.low:
        reject("invalid_quote_structure", "high >= low", f"high={quote.high},low={quote.low}")
        return
    if quote.price is not None and math.isfinite(quote.price):
        if quote.high is not None and math.isfinite(quote.high) and quote.price > quote.high + 1e-9:
            reject("invalid_quote_structure", "price <= high", f"price={quote.price},high={quote.high}")
            return
        if quote.low is not None and math.isfinite(quote.low) and quote.price < quote.low - 1e-9:
            reject("invalid_quote_structure", "price >= low", f"price={quote.price},low={quote.low}")
            return
    if quote.open_price is not None and math.isfinite(quote.open_price):
        if quote.high is not None and math.isfinite(quote.high) and quote.open_price > quote.high + 1e-9:
            reject("invalid_quote_structure", "open_price <= high", quote.open_price)
            return
        if quote.low is not None and math.isfinite(quote.low) and quote.open_price < quote.low - 1e-9:
            reject("invalid_quote_structure", "open_price >= low", quote.open_price)


def _json_scalar(value: object) -> str | float | bool | None:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        return number if math.isfinite(number) else str(value).lower()
    return str(value)


__all__ = ["FilterReason", "FilterResult", "board_for_code", "hard_filter"]
