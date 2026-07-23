"""Per-symbol field selection and market-rule projection for canonical merge."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache

from trader.domain.market.models import (
    Board,
    MarketQuote,
)
from trader.domain.recommendation.filters import board_for_code
from trader.infra.market_data.observations import JsonScalar, SourceObservation

_REALTIME_SOURCES = frozenset({"eastmoney", "sina", "tencent"})
_BOARD_SOURCES = frozenset({"tushare", "akshare", "eastmoney", "sina", "tencent"})
_REALTIME_FIELDS = frozenset(
    {
        "name",
        "price",
        "previous_close",
        "open_price",
        "high",
        "low",
        "pct_change",
        "change_5m",
        "speed",
        "volume_ratio",
        "turnover_rate",
        "amount",
        "amplitude",
        "market_cap",
        "is_st",
        "is_suspended",
        "is_one_price_limit",
        "is_blacklisted",
        "has_major_regulatory_risk",
    }
)
_BOARD_FIELDS = frozenset(
    {
        "board",
        "exchange",
        "listing_date",
        "listing_age_sessions",
        "is_relisted_first_session",
        "is_delisting_period_first_session",
        "has_price_limit",
        "exchange_limit_pct",
        "strategy_hot_cap_pct",
        "rule_version",
        "rule_effective_date",
    }
)
_SOURCE_PRIORITY = {"sina": 1, "eastmoney": 2, "tencent": 3, "akshare": 4, "tushare": 5}


@dataclass(frozen=True)
class _QuoteProjection:
    code: str
    values: dict[str, JsonScalar]
    sources: dict[str, str]
    price_observation: SourceObservation
    restrictions: set[str]
    deviation: float | None
    verified: bool


@dataclass
class _BoardProjection:
    values: dict[str, JsonScalar]
    sources: dict[str, str]
    restrictions: set[str]
    conflicts: set[str]


def merge_code(
    code: str,
    observations: Sequence[SourceObservation],
    *,
    targeted: bool,
) -> tuple[MarketQuote, dict[str, str], set[str]]:
    values, sources, selected_observations = _select_fields(observations, targeted=targeted)
    price_observation = selected_observations.get("price") or max(observations, key=observation_order)
    board_values, board_sources, restrictions, board_conflicts = _board_identity(code, observations)
    if price_observation.missing_reasons.get("cache_refresh") == "cache_degraded":
        restrictions.add("market_data_degraded")
    values.update(board_values)
    sources.update(board_sources)
    prices = _realtime_prices(observations)
    deviation, verified = _verify_prices(prices, targeted=targeted)
    conflicts = set(board_conflicts)
    if deviation is not None and deviation > 0.5 and not verified:
        conflicts.add(f"price_divergence:{code}")
        restrictions.add("cross_source_deviation")
    quote = _project_quote(
        _QuoteProjection(
            code,
            values,
            sources,
            price_observation,
            restrictions,
            deviation,
            verified,
        )
    )
    return quote, sources, conflicts


def _select_fields(
    observations: Sequence[SourceObservation],
    *,
    targeted: bool,
) -> tuple[dict[str, JsonScalar], dict[str, str], dict[str, SourceObservation]]:
    values: dict[str, JsonScalar] = {}
    sources: dict[str, str] = {}
    selected_observations: dict[str, SourceObservation] = {}
    selected_orders: dict[str, tuple[datetime, datetime, int, str, str]] = {}
    for observation in observations:
        normalized_source = source_name(observation.source)
        allows_realtime = normalized_source in _REALTIME_SOURCES
        allows_board = normalized_source in _BOARD_SOURCES
        priority = _SOURCE_PRIORITY.get(normalized_source, 0)
        standard_order = (
            observation.source_time,
            observation.received_at,
            priority,
            observation.data_version,
            observation.payload_hash,
        )
        realtime_order = (
            observation.source_time,
            observation.received_at,
            0 if normalized_source == "tencent" and not targeted else priority,
            observation.data_version,
            observation.payload_hash,
        )
        realtime_order_differs = normalized_source == "tencent" and not targeted
        for field, value in observation.fields.items():
            if value is None:
                continue
            if not allows_realtime and field in _REALTIME_FIELDS:
                continue
            if not allows_board and field in _BOARD_FIELDS:
                continue
            order = realtime_order if realtime_order_differs and field in _REALTIME_FIELDS else standard_order
            if field not in selected_orders or order > selected_orders[field]:
                selected_orders[field] = order
                selected_observations[field] = observation
    for field in sorted(selected_observations):
        selected = selected_observations[field]
        values[field] = selected.fields[field]
        source = source_name(selected.source)
        sources[field] = source
    return values, sources, selected_observations


def _realtime_prices(
    observations: Sequence[SourceObservation],
) -> list[tuple[SourceObservation, float | None]]:
    return [
        (observation, price)
        for observation in observations
        if source_name(observation.source) in _REALTIME_SOURCES
        if (price := _number(observation.fields.get("price"))) is not None
    ]


def _verify_prices(
    prices: Sequence[tuple[SourceObservation, float | None]],
    *,
    targeted: bool,
) -> tuple[float | None, bool]:
    deviation = _maximum_price_deviation(prices)
    verified = deviation is None or deviation <= 0.5
    if not verified and targeted:
        tencent = next((price for observation, price in prices if source_name(observation.source) == "tencent"), None)
        full_market_prices = [
            price
            for observation, price in prices
            if source_name(observation.source) in {"eastmoney", "sina"} and price is not None
        ]
        verified = tencent is not None and any(
            _price_deviation(tencent, full_market_price) <= 0.5 for full_market_price in full_market_prices
        )
    return deviation, verified


def _project_quote(projection: _QuoteProjection) -> MarketQuote:
    values = projection.values
    price_observation = projection.price_observation
    return MarketQuote(
        code=projection.code,
        name=_text(values.get("name")),
        price=_number(values.get("price")),
        previous_close=_number(values.get("previous_close")),
        open_price=_number(values.get("open_price")),
        high=_number(values.get("high")),
        low=_number(values.get("low")),
        pct_change=_number(values.get("pct_change")),
        change_5m=_number(values.get("change_5m")),
        speed=_number(values.get("speed")),
        volume_ratio=_number(values.get("volume_ratio")),
        turnover_rate=_number(values.get("turnover_rate")),
        amount=_number(values.get("amount")),
        amplitude=_number(values.get("amplitude")),
        market_cap=_number(values.get("market_cap")),
        industry=_text(values.get("industry")),
        source=source_name(price_observation.source),
        source_time=price_observation.source_time,
        received_time=price_observation.received_at,
        data_version=price_observation.data_version,
        is_st=_boolean(values.get("is_st")),
        is_suspended=_boolean(values.get("is_suspended")),
        is_one_price_limit=_boolean(values.get("is_one_price_limit")),
        is_blacklisted=_boolean(values.get("is_blacklisted")),
        has_major_regulatory_risk=_boolean(values.get("has_major_regulatory_risk")),
        cross_source_deviation_pct=round(projection.deviation, 6) if projection.deviation is not None else None,
        cross_source_verified=projection.verified,
        board=Board(_text(values.get("board")) or Board.UNSUPPORTED.value),
        board_source=projection.sources.get("board", "code_prefix_fallback"),
        board_reliability=_text(values.get("board_reliability")) or "degraded",
        exchange=_text(values.get("exchange")),
        listing_date=_optional_date(values.get("listing_date")),
        listing_age_sessions=_optional_integer(values.get("listing_age_sessions")),
        is_relisted_first_session=_optional_boolean(values.get("is_relisted_first_session")),
        is_delisting_period_first_session=_optional_boolean(values.get("is_delisting_period_first_session")),
        has_price_limit=_optional_boolean(values.get("has_price_limit")),
        exchange_limit_pct=_number(values.get("exchange_limit_pct")),
        strategy_hot_cap_pct=_number(values.get("strategy_hot_cap_pct")),
        rule_version=_text(values.get("rule_version")),
        rule_effective_date=_optional_date(values.get("rule_effective_date")),
        execution_restrictions=tuple(sorted(projection.restrictions)),
    )


def _board_identity(
    code: str,
    observations: Sequence[SourceObservation],
) -> tuple[dict[str, JsonScalar], dict[str, str], set[str], set[str]]:
    candidates = [observation for observation in observations if observation.fields.get("board") is not None]
    fallback = board_for_code(code)
    projection = _BoardProjection({}, {}, set(), set())
    if candidates:
        _apply_reported_board(code, candidates, fallback, projection)
    else:
        projection.values["board"] = fallback.value
        projection.values["board_reliability"] = "degraded"
        projection.sources["board"] = "code_prefix_fallback"
        projection.sources["board_reliability"] = "code_prefix_fallback"
        projection.restrictions.add("board_identity_degraded")
    _apply_board_defaults(projection.values, projection.sources, projection.restrictions)
    return projection.values, projection.sources, projection.restrictions, projection.conflicts


def _apply_reported_board(
    code: str,
    candidates: Sequence[SourceObservation],
    fallback: Board,
    projection: _BoardProjection,
) -> None:
    values = projection.values
    sources = projection.sources
    selected = max(
        candidates,
        key=lambda item: (_SOURCE_PRIORITY.get(source_name(item.source), 0), *observation_order(item)),
    )
    source = source_name(selected.source)
    selected_board = _board_value(selected.fields.get("board"))
    reported = {_board_value(item.fields.get("board")) for item in candidates}
    if len(reported) > 1 or (fallback is not Board.UNSUPPORTED and selected_board is not fallback):
        projection.restrictions.add("board_classification_conflict")
        projection.conflicts.add(f"board_classification_conflict:{code}")
        values["board_reliability"] = "conflict"
        sources["board"] = "conflict"
        sources["board_reliability"] = "conflict"
    else:
        source_reliability = selected.fields.get("board_reliability")
        values["board_reliability"] = (
            "degraded"
            if source_reliability == "degraded"
            else "verified"
            if source in {"tushare", "akshare"}
            else "reported"
        )
        sources["board"] = source
        sources["board_reliability"] = source
        if source_reliability == "degraded":
            projection.restrictions.add("board_identity_degraded")
    values["board"] = selected_board.value
    for field in _BOARD_FIELDS - {"board"}:
        value = selected.fields.get(field)
        if value is not None:
            values[field] = value
            if field in {"listing_age_sessions", "has_price_limit"}:
                sources[field] = "trading_calendar"
            elif field == "exchange_limit_pct":
                sources[field] = "local_rule"
            else:
                sources[field] = source


def _apply_board_defaults(
    values: dict[str, JsonScalar],
    sources: dict[str, str],
    restrictions: set[str],
) -> None:
    listing_date = _optional_date(values.get("listing_date"))
    listing_age_sessions = _optional_integer(values.get("listing_age_sessions"))
    if listing_date is None:
        restrictions.add("missing_listing_date")
    elif listing_age_sessions is None:
        restrictions.add("missing_listing_age_sessions")
    if values.get("rule_version") is None:
        values["rule_version"] = "cn-board-rules-v1"
        sources["rule_version"] = "local_rule"
    if values.get("rule_effective_date") is None:
        values["rule_effective_date"] = "2023-08-28"
        sources["rule_effective_date"] = "local_rule"
    if values.get("strategy_hot_cap_pct") is None:
        values["strategy_hot_cap_pct"] = 8.0 if values["board"] == Board.MAIN.value else 16.0
    sources["strategy_hot_cap_pct"] = "local_rule"
    if values.get("rule_version") is not None:
        sources["rule_version"] = "local_rule"
    if values.get("rule_effective_date") is not None:
        sources["rule_effective_date"] = "local_rule"


def observation_order(observation: SourceObservation) -> tuple[datetime, datetime, int, str, str]:
    return (
        observation.source_time,
        observation.received_at,
        _SOURCE_PRIORITY.get(source_name(observation.source), 0),
        observation.data_version,
        observation.payload_hash,
    )


def rejection_reason(observation: SourceObservation, observed_at: datetime) -> str | None:
    if observation.status != "success":
        return observation.status
    if not observation.data_version.strip():
        return "empty_data_version"
    if (
        observation.observed_at > observed_at
        or observation.source_time > observed_at
        or observation.received_at > observed_at
        or observation.effective_at > observed_at
    ):
        return "future_observation"
    return None


def _maximum_price_deviation(prices: Sequence[tuple[SourceObservation, float | None]]) -> float | None:
    finite = [price for _observation, price in prices if price is not None and price > 0]
    if len(finite) < 2:
        return None
    baseline = Decimal(str(min(finite)))
    maximum = Decimal(str(max(finite)))
    return float((maximum - baseline) / baseline * Decimal("100"))


def _price_deviation(first: float | None, second: float | None) -> float:
    if first is None or second is None or first <= 0 or second <= 0:
        return math.inf
    first_decimal = Decimal(str(first))
    second_decimal = Decimal(str(second))
    baseline = min(first_decimal, second_decimal)
    return float(abs(first_decimal - second_decimal) / baseline * Decimal("100"))


@lru_cache(maxsize=32)
def source_name(source: str) -> str:
    return source.strip().lower().split("_", 1)[0].split("-", 1)[0]


def source_priority(source: str) -> int:
    return _SOURCE_PRIORITY.get(source_name(source), 0)


def _board_value(value: JsonScalar) -> Board:
    try:
        return Board(_text(value).lower())
    except ValueError:
        return Board.UNSUPPORTED


def _text(value: JsonScalar) -> str:
    return value if isinstance(value, str) else ""


def _number(value: JsonScalar) -> float | None:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _boolean(value: JsonScalar) -> bool:
    return bool(value) if isinstance(value, bool) else False


def _optional_boolean(value: JsonScalar) -> bool | None:
    return value if isinstance(value, bool) else None


def _optional_integer(value: JsonScalar) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number >= 0 and number.is_integer() else None


def _optional_date(value: JsonScalar) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


__all__ = ["merge_code", "observation_order", "rejection_reason", "source_name", "source_priority"]
