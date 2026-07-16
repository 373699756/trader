"""Hard candidate filters with auditable failure details."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from trader.domain.models import Board, FeatureSnapshot


@dataclass(frozen=True)
class FilterReason:
    code: str
    threshold: str
    actual: str
    source: str
    observed_at: datetime


@dataclass(frozen=True)
class FilterResult:
    allowed: bool
    board: Board
    reasons: tuple[FilterReason, ...]


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
    quote = snapshot.quote
    board = board_for_code(quote.code)
    reasons: list[FilterReason] = []

    def reject(code: str, threshold: str, actual: object) -> None:
        reasons.append(
            FilterReason(
                code=code,
                threshold=threshold,
                actual=str(actual),
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
    if quote.age_seconds(now) > max_age_seconds:
        reject("stale_quote", f"<= {max_age_seconds}s", round(quote.age_seconds(now), 3))
    if (
        quote.cross_source_deviation_pct is not None
        and quote.cross_source_deviation_pct > 0.5
        and not quote.cross_source_verified
    ):
        reject("cross_source_deviation", "<= 0.5% or verified", quote.cross_source_deviation_pct)
    median_amount = snapshot.optional_value("amount_median_20d")
    if median_amount is not None and median_amount < 50_000_000:
        reject("insufficient_liquidity", ">= 50000000", median_amount)
    if quote.is_one_price_limit:
        reject("one_price_limit", "false", True)
    if quote.is_blacklisted:
        reject("blacklisted", "false", True)
    if quote.has_major_regulatory_risk:
        reject("major_regulatory_risk", "false", True)
    if quote.pct_change is not None:
        if board is Board.MAIN and quote.pct_change > 8.0:
            reject("main_board_too_hot", "<= 8.00", quote.pct_change)
        if board in {Board.CHINEXT, Board.STAR} and quote.pct_change > 16.0:
            reject("growth_board_too_hot", "<= 16.00", quote.pct_change)

    return FilterResult(allowed=not reasons, board=board, reasons=tuple(reasons))


__all__ = ["FilterReason", "FilterResult", "board_for_code", "hard_filter"]
