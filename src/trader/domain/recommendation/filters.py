"""Recommendation candidate filters with explicit severity rules."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from types import MappingProxyType

from trader.domain.market.models import (
    Board,
    FeatureSnapshot,
)
from trader.domain.recommendation.models import FilterAudit


class FilterSeverity(str, Enum):
    REQUIRED = "required"
    OPTIONAL = "optional"


@dataclass(frozen=True)
class HardFilterPolicy:
    blacklist_codes: frozenset[str] = frozenset()
    structured_risk_thresholds: Mapping[str, float] = field(
        default_factory=lambda: MappingProxyType(
            {
                "major_shareholder_reduction": 0.0,
                "financial_fraud_history": 0.0,
                "official_investigation_history": 0.0,
                "major_illegal_history": 0.0,
                "fund_occupation_history": 0.0,
                "illegal_guarantee_history": 0.0,
                "forced_delisting_risk": 0.0,
                "unlock_risk": 0.0,
                "pledge_risk": 0.0,
                "financial_deterioration": 0.5,
            }
        )
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "blacklist_codes", frozenset(self.blacklist_codes))
        object.__setattr__(self, "structured_risk_thresholds", MappingProxyType(dict(self.structured_risk_thresholds)))


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


def board_for_snapshot(snapshot: FeatureSnapshot) -> Board:
    return (
        snapshot.quote.board if snapshot.quote.board is not Board.UNSUPPORTED else board_for_code(snapshot.quote.code)
    )


def hard_filter(
    snapshot: FeatureSnapshot,
    now: datetime,
    *,
    max_age_seconds: float,
    policy: HardFilterPolicy | None = None,
) -> FilterResult:
    return apply_filters(
        snapshot,
        default_filter_rules(max_age_seconds=max_age_seconds, policy=policy),
        now=now,
    )


def legacy_v14_hard_filter(
    snapshot: FeatureSnapshot,
    now: datetime,
    *,
    max_age_seconds: float,
    policy: HardFilterPolicy | None = None,
) -> FilterResult:
    """Reproduce the pre-v15 filter projection for old frozen replays."""

    current = hard_filter(snapshot, now, max_age_seconds=max_age_seconds, policy=policy)
    v15_required = {
        "new_listing_session",
        "relisted_first_session",
        "delisting_period_first_session",
    }
    v15_optional = {
        "board_classification_conflict",
        "board_identity_degraded",
        "missing_listing_date",
        "missing_listing_age_sessions",
    }
    reasons = [reason for reason in current.reasons if reason.filter_code not in v15_required]
    optional_flags: list[FilterAudit] = []
    for audit in current.optional_flags:
        if audit.filter_code == "cross_source_deviation":
            reasons.append(audit)
        elif audit.filter_code not in v15_optional:
            optional_flags.append(audit)
    reasons = [
        replace(reason, filter_code="growth_board_too_hot")
        if reason.filter_code in {"chinext_board_too_hot", "star_board_too_hot"}
        else reason
        for reason in reasons
    ]
    return FilterResult(
        allowed=not reasons,
        board=board_for_code(snapshot.quote.code),
        reasons=tuple(reasons),
        optional_flags=tuple(optional_flags),
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
    board = board_for_snapshot(snapshot)
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


class _DefaultFilterRules:
    def __init__(self, max_age_seconds: float, policy: HardFilterPolicy) -> None:
        self.max_age_seconds = max_age_seconds
        self.policy = policy

    def unsupported_code(self, snapshot: FeatureSnapshot, _now: datetime) -> FilterAudit | None:
        if board_for_code(snapshot.quote.code) is Board.UNSUPPORTED:
            return _make_audit(
                snapshot, "unsupported_code", "supported Shanghai/Shenzhen board code", snapshot.quote.code
            )
        return None

    def st_or_delisting(self, snapshot: FeatureSnapshot, _now: datetime) -> FilterAudit | None:
        quote = snapshot.quote
        if quote.is_st or "ST" in quote.name.upper() or "退" in quote.name:
            return _make_audit(snapshot, "st_or_delisting", "false", quote.name)
        return None

    def suspended(self, snapshot: FeatureSnapshot, _now: datetime) -> FilterAudit | None:
        if snapshot.quote.is_suspended:
            return _make_audit(snapshot, "suspended", "false", True)
        return None

    def invalid_price(self, snapshot: FeatureSnapshot, _now: datetime) -> FilterAudit | None:
        quote = snapshot.quote
        if quote.price is None or not math.isfinite(quote.price) or quote.price <= 0:
            return _make_audit(snapshot, "invalid_price", "> 0", quote.price)
        return None

    def invalid_amount(self, snapshot: FeatureSnapshot, _now: datetime) -> FilterAudit | None:
        quote = snapshot.quote
        if quote.amount is None or not math.isfinite(quote.amount) or quote.amount <= 0:
            return _make_audit(snapshot, "invalid_amount", "> 0", quote.amount)
        return None

    def invalid_quote_time(self, snapshot: FeatureSnapshot, now: datetime) -> FilterAudit | None:
        quote = snapshot.quote
        if quote.source_time.tzinfo is None or quote.source_time.utcoffset() is None:
            return _make_audit(snapshot, "invalid_quote_time", "timezone-aware", quote.source_time.isoformat())
        if quote.source_time > now:
            return _make_audit(snapshot, "future_quote", "<= evaluation time", quote.source_time.isoformat())
        if quote.age_seconds(now) > self.max_age_seconds:
            return _make_audit(
                snapshot,
                "stale_quote",
                f"<= {self.max_age_seconds}s",
                round(quote.age_seconds(now), 3),
            )
        return None

    def cross_source_deviation(self, snapshot: FeatureSnapshot, _now: datetime) -> FilterAudit | None:
        deviation = snapshot.quote.cross_source_deviation_pct
        if deviation is not None and (not math.isfinite(deviation) or deviation < 0):
            return _make_audit(snapshot, "invalid_cross_source_deviation", "finite and >= 0", deviation)
        return None

    def cross_source_deviation_optional(self, snapshot: FeatureSnapshot, _now: datetime) -> FilterAudit | None:
        deviation = snapshot.quote.cross_source_deviation_pct
        if (
            deviation is not None
            and math.isfinite(deviation)
            and deviation > 0.5
            and not snapshot.quote.cross_source_verified
        ):
            return _make_audit(snapshot, "cross_source_deviation", "<= 0.5% or verified", deviation)
        return None

    def new_listing_session(self, snapshot: FeatureSnapshot, _now: datetime) -> FilterAudit | None:
        age = snapshot.quote.listing_age_sessions
        if age is not None and age < 6:
            return _make_audit(snapshot, "new_listing_session", ">= 6 trading sessions", age)
        return None

    def relisted_first_session(self, snapshot: FeatureSnapshot, _now: datetime) -> FilterAudit | None:
        if snapshot.quote.is_relisted_first_session:
            return _make_audit(snapshot, "relisted_first_session", "false", True)
        return None

    def delisting_period_first_session(self, snapshot: FeatureSnapshot, _now: datetime) -> FilterAudit | None:
        if snapshot.quote.is_delisting_period_first_session:
            return _make_audit(snapshot, "delisting_period_first_session", "false", True)
        return None

    def board_classification_conflict(self, snapshot: FeatureSnapshot, _now: datetime) -> FilterAudit | None:
        if (
            "board_classification_conflict" in snapshot.quote.execution_restrictions
            or snapshot.quote.board_reliability == "conflict"
        ):
            return _make_audit(snapshot, "board_classification_conflict", "verified board identity", "conflict")
        return None

    def board_identity_degraded(self, snapshot: FeatureSnapshot, _now: datetime) -> FilterAudit | None:
        if (
            "board_identity_degraded" in snapshot.quote.execution_restrictions
            or snapshot.quote.board_source == "code_prefix_fallback"
        ):
            return _make_audit(
                snapshot,
                "board_identity_degraded",
                "security master, security list, or market field",
                snapshot.quote.board_source,
            )
        return None

    def missing_listing_date(self, snapshot: FeatureSnapshot, _now: datetime) -> FilterAudit | None:
        if "missing_listing_date" in snapshot.quote.execution_restrictions or (
            snapshot.quote.rule_version and snapshot.quote.listing_date is None
        ):
            return _make_audit(snapshot, "missing_listing_date", "effective listing date", None)
        return None

    def missing_listing_age_sessions(self, snapshot: FeatureSnapshot, _now: datetime) -> FilterAudit | None:
        if "missing_listing_age_sessions" in snapshot.quote.execution_restrictions or (
            snapshot.quote.listing_date is not None and snapshot.quote.listing_age_sessions is None
        ):
            return _make_audit(
                snapshot,
                "missing_listing_age_sessions",
                "verified trading-calendar listing age",
                None,
            )
        return None

    def missing_liquidity(self, snapshot: FeatureSnapshot, _now: datetime) -> FilterAudit | None:
        median_amount = snapshot.values.get("amount_median_20d")
        if median_amount is None:
            return _make_audit(snapshot, "missing_liquidity_history", ">= 50000000", None)
        if not math.isfinite(median_amount):
            return _make_audit(snapshot, "invalid_liquidity_history", ">= 50000000", median_amount)
        if median_amount < 50_000_000:
            return _make_audit(snapshot, "insufficient_liquidity", ">= 50000000", median_amount)
        return None

    def one_price_limit(self, snapshot: FeatureSnapshot, _now: datetime) -> FilterAudit | None:
        if snapshot.quote.is_one_price_limit:
            return _make_audit(snapshot, "one_price_limit", "false", True)
        return None

    def blacklisted(self, snapshot: FeatureSnapshot, _now: datetime) -> FilterAudit | None:
        if snapshot.quote.is_blacklisted or snapshot.quote.code in self.policy.blacklist_codes:
            return _make_audit(snapshot, "blacklisted", "false", True)
        return None

    def major_regulatory_risk(self, snapshot: FeatureSnapshot, _now: datetime) -> FilterAudit | None:
        if snapshot.quote.has_major_regulatory_risk:
            return _make_audit(snapshot, "major_regulatory_risk", "false", True)
        return None

    def invalid_quote_structure(self, snapshot: FeatureSnapshot, _now: datetime) -> FilterAudit | None:
        audit: FilterAudit | None = None

        def set_audit(code: str, threshold: str, actual: object) -> None:
            nonlocal audit
            if audit is None:
                audit = _make_audit(snapshot, code, threshold, actual)

        _reject_invalid_quote_structure(snapshot, set_audit)
        return audit

    def invalid_pct_change(self, snapshot: FeatureSnapshot, _now: datetime) -> FilterAudit | None:
        board = board_for_snapshot(snapshot)
        pct_change = snapshot.quote.pct_change
        if pct_change is None or not math.isfinite(pct_change):
            return _make_audit(snapshot, "invalid_pct_change", "finite percentage points", pct_change)
        if board is Board.MAIN and pct_change > 8.0:
            return _make_audit(snapshot, "main_board_too_hot", "<= 8.00", pct_change)
        if board is Board.CHINEXT and pct_change > 16.0:
            return _make_audit(snapshot, "chinext_board_too_hot", "<= 16.00", pct_change)
        if board is Board.STAR and pct_change > 16.0:
            return _make_audit(snapshot, "star_board_too_hot", "<= 16.00", pct_change)
        return None

    def structured_negative_risk(self, snapshot: FeatureSnapshot, _now: datetime) -> FilterAudit | None:
        code_by_field = {
            "major_shareholder_reduction": "major_shareholder_reduction",
            "financial_fraud_history": "financial_fraud_history",
            "official_investigation_history": "official_investigation_history",
            "major_illegal_history": "major_illegal_history",
            "fund_occupation_history": "fund_occupation_history",
            "illegal_guarantee_history": "illegal_guarantee_history",
            "forced_delisting_risk": "forced_delisting_risk",
            "unlock_risk": "unlock_risk",
            "reduction_or_unlock": "reduction_or_unlock",
            "pledge_risk": "pledge_risk",
            "financial_deterioration": "financial_deterioration",
        }
        for field_name, threshold in self.policy.structured_risk_thresholds.items():
            value = snapshot.values.get(field_name)
            if value is not None and math.isfinite(value) and value > threshold:
                return _make_audit(snapshot, code_by_field.get(field_name, field_name), f"<= {threshold:g}", value)
        return None

    def structured_risk_unavailable(self, snapshot: FeatureSnapshot, _now: datetime) -> FilterAudit | None:
        missing = tuple(
            field_name
            for field_name in self.policy.structured_risk_thresholds
            if (value := snapshot.values.get(field_name)) is None or not math.isfinite(value)
        )
        if missing:
            return _make_audit(
                snapshot,
                "structured_risk_unavailable",
                "all structured risks available",
                ",".join(missing),
            )
        return None

    def corporate_risk_history_unavailable(
        self,
        snapshot: FeatureSnapshot,
        _now: datetime,
    ) -> FilterAudit | None:
        value = snapshot.values.get("corporate_risk_history_unavailable")
        if value is not None and math.isfinite(value) and value > 0.0:
            return _make_audit(
                snapshot,
                "corporate_risk_history_unavailable",
                "complete official-history coverage",
                value,
            )
        return None


def default_filter_rules(*, max_age_seconds: float, policy: HardFilterPolicy | None = None) -> tuple[FilterRule, ...]:
    """Build default filter rule registry for all strategies.

    Required rules are preserved from ``hard_filter`` behavior. Optional rules
    are used to record soft warnings without rejecting candidates.
    """
    if not math.isfinite(max_age_seconds) or max_age_seconds < 0:
        raise ValueError("max_age_seconds must be finite and non-negative")
    policy = policy or HardFilterPolicy()
    registry = _DefaultFilterRules(max_age_seconds, policy)

    return (
        FilterRule("unsupported_code", FilterSeverity.REQUIRED, registry.unsupported_code),
        FilterRule("st_or_delisting", FilterSeverity.REQUIRED, registry.st_or_delisting),
        FilterRule("suspended", FilterSeverity.REQUIRED, registry.suspended),
        FilterRule("invalid_price", FilterSeverity.REQUIRED, registry.invalid_price),
        FilterRule("invalid_amount", FilterSeverity.REQUIRED, registry.invalid_amount),
        FilterRule("invalid_quote_time", FilterSeverity.REQUIRED, registry.invalid_quote_time),
        FilterRule("invalid_cross_source_deviation", FilterSeverity.REQUIRED, registry.cross_source_deviation),
        FilterRule("cross_source_deviation", FilterSeverity.OPTIONAL, registry.cross_source_deviation_optional),
        FilterRule("new_listing_session", FilterSeverity.REQUIRED, registry.new_listing_session),
        FilterRule("relisted_first_session", FilterSeverity.REQUIRED, registry.relisted_first_session),
        FilterRule("delisting_period_first_session", FilterSeverity.REQUIRED, registry.delisting_period_first_session),
        FilterRule("board_classification_conflict", FilterSeverity.OPTIONAL, registry.board_classification_conflict),
        FilterRule("board_identity_degraded", FilterSeverity.OPTIONAL, registry.board_identity_degraded),
        FilterRule("missing_listing_date", FilterSeverity.OPTIONAL, registry.missing_listing_date),
        FilterRule("missing_listing_age_sessions", FilterSeverity.OPTIONAL, registry.missing_listing_age_sessions),
        FilterRule("missing_liquidity_history", FilterSeverity.REQUIRED, registry.missing_liquidity),
        FilterRule("one_price_limit", FilterSeverity.REQUIRED, registry.one_price_limit),
        FilterRule("blacklisted", FilterSeverity.REQUIRED, registry.blacklisted),
        FilterRule("major_regulatory_risk", FilterSeverity.REQUIRED, registry.major_regulatory_risk),
        FilterRule("structured_negative_risk", FilterSeverity.REQUIRED, registry.structured_negative_risk),
        FilterRule("structured_risk_unavailable", FilterSeverity.OPTIONAL, registry.structured_risk_unavailable),
        FilterRule(
            "corporate_risk_history_unavailable",
            FilterSeverity.OPTIONAL,
            registry.corporate_risk_history_unavailable,
        ),
        FilterRule("invalid_quote_structure", FilterSeverity.REQUIRED, registry.invalid_quote_structure),
        FilterRule("invalid_pct_change", FilterSeverity.REQUIRED, registry.invalid_pct_change),
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
    "FilterResult",
    "HardFilterPolicy",
    "FilterRule",
    "FilterSeverity",
    "apply_filters",
    "board_for_code",
    "board_for_snapshot",
    "hard_filter",
    "legacy_v14_hard_filter",
]
