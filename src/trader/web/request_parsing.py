"""Pure parsing helpers for the read-only HTTP boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from trader.domain.recommendation.models import Strategy
from trader.web.route_services import WebApiConfig


@dataclass(frozen=True)
class RequestFailure:
    code: str
    message: str
    status_code: int = 400


@dataclass(frozen=True)
class RecommendationRequest:
    strategy: Strategy
    top_n: int
    trade_date: str | None
    view: str


def parse_recommendation_request(
    strategy_name: str,
    *,
    top_n: str | None,
    trade_date: str | None,
    view: str,
    config: WebApiConfig,
) -> RecommendationRequest | RequestFailure:
    strategy = parse_strategy(strategy_name)
    parsed_top_n = bounded_integer(top_n, config.default_top_n)
    failure = _recommendation_failure(strategy, parsed_top_n, trade_date, view, config)
    if failure is not None:
        return failure
    if strategy is None or parsed_top_n is None:
        raise AssertionError("validated recommendation request lost parsed values")
    return RecommendationRequest(strategy, parsed_top_n, trade_date, view)


def parse_strategy(raw: str) -> Strategy | None:
    try:
        return Strategy(raw)
    except ValueError:
        return None


def bounded_integer(raw: str | None, default: int) -> int | None:
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 and str(value) == raw.strip() else None


def _recommendation_failure(
    strategy: Strategy | None,
    top_n: int | None,
    trade_date: str | None,
    view: str,
    config: WebApiConfig,
) -> RequestFailure | None:
    failure: RequestFailure | None = None
    if strategy is None:
        failure = RequestFailure("invalid_strategy", "strategy must be today, tomorrow, d25 or long")
    elif top_n is None or top_n > config.maximum_top_n:
        failure = RequestFailure(
            "invalid_top_n",
            f"top_n must be an integer from 0 to {config.maximum_top_n}",
        )
    elif trade_date is not None and not _valid_date(trade_date):
        failure = RequestFailure("invalid_date", "date must use YYYY-MM-DD")
    elif view not in {"current", "official", "live"}:
        failure = RequestFailure("invalid_view", "view must be current, official or live")
    elif trade_date is not None and view == "live":
        failure = RequestFailure("invalid_view", "live view cannot be combined with a historical date")
    return failure


def _valid_date(raw: str) -> bool:
    try:
        parsed = date.fromisoformat(raw)
    except ValueError:
        return False
    return parsed.isoformat() == raw


__all__ = [
    "RecommendationRequest",
    "RequestFailure",
    "bounded_integer",
    "parse_recommendation_request",
    "parse_strategy",
]
