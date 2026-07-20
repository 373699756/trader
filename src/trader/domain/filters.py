"""Candidate filters with explicit required/optional severity rules."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from trader.domain.models import Board, FeatureSnapshot, FilterAudit

FilterReason = FilterAudit


class FilterSeverity(str, Enum):
    REQUIRED = "required"
    OPTIONAL = "optional"


@dataclass(frozen=True)
class FilterRule:
    """A single filtering predicate with severity.

    ``REQUIRED`` rules cause the candidate to be rejected immediately.
    ``OPTIONAL`` rules produce an audit entry but do not block the candidate.
    """

    name: str
    severity: FilterSeverity
    predicate: Callable[[FeatureSnapshot, datetime], FilterAudit | None]


@dataclass(frozen=True)
class FilterResult:
    allowed: bool
    board: Board
    reasons: tuple[FilterAudit, ...]
    optional_flags: tuple[FilterAudit, ...] = ()


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
    return apply_filters(
        snapshot,
        default_filter_rules(max_age_seconds=max_age_seconds),
        now=now,
    )


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


def apply_filters(
    snapshot: FeatureSnapshot,
    rules: Sequence[FilterRule],
    *,
    now: datetime,
) -> FilterResult:
    """Apply a sequence of ``FilterRule`` items to *snapshot*.

    ``REQUIRED`` rules that return an audit cause immediate rejection.
    ``OPTIONAL`` rules that return an audit are collected into
    ``optional_flags`` but do not block the candidate.
    """
    board = board_for_code(snapshot.quote.code)
    reasons: list[FilterAudit] = []
    optional_flags: list[FilterAudit] = []
    for rule in rules:
        audit = rule.predicate(snapshot, now)
        if audit is None:
            continue
        if rule.severity is FilterSeverity.REQUIRED:
            reasons.append(audit)
        else:
            optional_flags.append(audit)
    return FilterResult(
        allowed=not reasons,
        board=board,
        reasons=tuple(reasons),
        optional_flags=tuple(optional_flags),
    )


def default_filter_rules(*, max_age_seconds: float) -> tuple[FilterRule, ...]:
    """Build default filter rule registry for all strategies.

    Required rules are preserved from ``hard_filter`` behavior. Optional rules
    are used to record soft warnings without rejecting candidates.
    """
    if not math.isfinite(max_age_seconds) or max_age_seconds < 0:
        raise ValueError("max_age_seconds must be finite and non-negative")

    def unsupported_code(snapshot: FeatureSnapshot, _now: datetime) -> FilterAudit | None:
        if board_for_code(snapshot.quote.code) is Board.UNSUPPORTED:
            return _make_audit(
                snapshot, "unsupported_code", "supported Shanghai/Shenzhen board code", snapshot.quote.code
            )
        return None

    def st_or_delisting(snapshot: FeatureSnapshot, _now: datetime) -> FilterAudit | None:
        quote = snapshot.quote
        if quote.is_st or "ST" in quote.name.upper() or "退" in quote.name:
            return _make_audit(snapshot, "st_or_delisting", "false", quote.name)
        return None

    def suspended(snapshot: FeatureSnapshot, _now: datetime) -> FilterAudit | None:
        if snapshot.quote.is_suspended:
            return _make_audit(snapshot, "suspended", "false", True)
        return None

    def invalid_price(snapshot: FeatureSnapshot, _now: datetime) -> FilterAudit | None:
        quote = snapshot.quote
        if quote.price is None or not math.isfinite(quote.price) or quote.price <= 0:
            return _make_audit(snapshot, "invalid_price", "> 0", quote.price)
        return None

    def invalid_amount(snapshot: FeatureSnapshot, _now: datetime) -> FilterAudit | None:
        quote = snapshot.quote
        if quote.amount is None or not math.isfinite(quote.amount) or quote.amount <= 0:
            return _make_audit(snapshot, "invalid_amount", "> 0", quote.amount)
        return None

    def invalid_quote_time(snapshot: FeatureSnapshot, now: datetime) -> FilterAudit | None:
        quote = snapshot.quote
        if quote.source_time.tzinfo is None or quote.source_time.utcoffset() is None:
            return _make_audit(snapshot, "invalid_quote_time", "timezone-aware", quote.source_time.isoformat())
        if quote.source_time > now:
            return _make_audit(snapshot, "future_quote", "<= evaluation time", quote.source_time.isoformat())
        if quote.age_seconds(now) > max_age_seconds:
            return _make_audit(
                snapshot,
                "stale_quote",
                f"<= {max_age_seconds}s",
                round(quote.age_seconds(now), 3),
            )
        return None

    def cross_source_deviation(snapshot: FeatureSnapshot, _now: datetime) -> FilterAudit | None:
        deviation = snapshot.quote.cross_source_deviation_pct
        if deviation is not None and (not math.isfinite(deviation) or deviation < 0):
            return _make_audit(snapshot, "invalid_cross_source_deviation", "finite and >= 0", deviation)
        return None

    def cross_source_deviation_optional(snapshot: FeatureSnapshot, _now: datetime) -> FilterAudit | None:
        deviation = snapshot.quote.cross_source_deviation_pct
        if (
            deviation is not None
            and math.isfinite(deviation)
            and deviation > 0.5
            and not snapshot.quote.cross_source_verified
        ):
            return _make_audit(snapshot, "cross_source_deviation", "<= 0.5% or verified", deviation)
        return None

    def missing_liquidity(snapshot: FeatureSnapshot, _now: datetime) -> FilterAudit | None:
        median_amount = snapshot.values.get("amount_median_20d")
        if median_amount is None:
            return _make_audit(snapshot, "missing_liquidity_history", ">= 50000000", None)
        if not math.isfinite(median_amount):
            return _make_audit(snapshot, "invalid_liquidity_history", ">= 50000000", median_amount)
        if median_amount < 50_000_000:
            return _make_audit(snapshot, "insufficient_liquidity", ">= 50000000", median_amount)
        return None

    def one_price_limit(snapshot: FeatureSnapshot, _now: datetime) -> FilterAudit | None:
        if snapshot.quote.is_one_price_limit:
            return _make_audit(snapshot, "one_price_limit", "false", True)
        return None

    def blacklisted(snapshot: FeatureSnapshot, _now: datetime) -> FilterAudit | None:
        if snapshot.quote.is_blacklisted:
            return _make_audit(snapshot, "blacklisted", "false", True)
        return None

    def major_regulatory_risk(snapshot: FeatureSnapshot, _now: datetime) -> FilterAudit | None:
        if snapshot.quote.has_major_regulatory_risk:
            return _make_audit(snapshot, "major_regulatory_risk", "false", True)
        return None

    def invalid_quote_structure(snapshot: FeatureSnapshot, _now: datetime) -> FilterAudit | None:
        audit: FilterAudit | None = None

        def set_audit(code: str, threshold: str, actual: object) -> None:
            nonlocal audit
            if audit is None:
                audit = _make_audit(snapshot, code, threshold, actual)

        _reject_invalid_quote_structure(snapshot, set_audit)
        return audit

    def invalid_pct_change(snapshot: FeatureSnapshot, _now: datetime) -> FilterAudit | None:
        board = board_for_code(snapshot.quote.code)
        pct_change = snapshot.quote.pct_change
        if pct_change is None or not math.isfinite(pct_change):
            return _make_audit(snapshot, "invalid_pct_change", "finite percentage points", pct_change)
        if board is Board.MAIN and pct_change > 8.0:
            return _make_audit(snapshot, "main_board_too_hot", "<= 8.00", pct_change)
        if board in {Board.CHINEXT, Board.STAR} and pct_change > 16.0:
            return _make_audit(snapshot, "growth_board_too_hot", "<= 16.00", pct_change)
        return None

    return (
        FilterRule("unsupported_code", FilterSeverity.REQUIRED, unsupported_code),
        FilterRule("st_or_delisting", FilterSeverity.REQUIRED, st_or_delisting),
        FilterRule("suspended", FilterSeverity.REQUIRED, suspended),
        FilterRule("invalid_price", FilterSeverity.REQUIRED, invalid_price),
        FilterRule("invalid_amount", FilterSeverity.REQUIRED, invalid_amount),
        FilterRule("invalid_quote_time", FilterSeverity.REQUIRED, invalid_quote_time),
        FilterRule("invalid_cross_source_deviation", FilterSeverity.REQUIRED, cross_source_deviation),
        FilterRule("cross_source_deviation", FilterSeverity.REQUIRED, cross_source_deviation_optional),
        FilterRule("missing_liquidity_history", FilterSeverity.REQUIRED, missing_liquidity),
        FilterRule("one_price_limit", FilterSeverity.REQUIRED, one_price_limit),
        FilterRule("blacklisted", FilterSeverity.REQUIRED, blacklisted),
        FilterRule("major_regulatory_risk", FilterSeverity.REQUIRED, major_regulatory_risk),
        FilterRule("invalid_quote_structure", FilterSeverity.REQUIRED, invalid_quote_structure),
        FilterRule("invalid_pct_change", FilterSeverity.REQUIRED, invalid_pct_change),
    )


def _make_audit(snapshot: FeatureSnapshot, code: str, threshold: str, actual: object) -> FilterAudit:
    return FilterAudit(
        stock_code=snapshot.quote.code,
        filter_code=code,
        threshold=threshold,
        actual=_json_scalar(actual),
        source=snapshot.quote.source,
        observed_at=snapshot.quote.source_time,
    )


__all__ = [
    "FilterReason",
    "FilterResult",
    "FilterRule",
    "FilterSeverity",
    "apply_filters",
    "board_for_code",
    "hard_filter",
]
