"""Versioned JSON envelopes for recommendation delivery."""

from __future__ import annotations

from collections.abc import Mapping

from trader.domain.market.models import LiveQuote
from trader.domain.recommendation.models import (
    LiveOverlay,
    Recommendation,
    RecommendationSnapshot,
)
from trader.domain.review.models import (
    DeepSeekReview,
    RiskFact,
)

API_SCHEMA_VERSION = "v3"


def snapshot_envelope(
    snapshot: RecommendationSnapshot,
    *,
    top_n: int,
    overlay: LiveOverlay | None = None,
    requested_date: str | None = None,
    current_trade_date: str | None = None,
    historical: bool = False,
    current_quotes: Mapping[str, LiveQuote] | None = None,
    view: str = "official",
) -> dict[str, object]:
    """Project a domain snapshot into the compact dashboard contract."""
    live_quotes = overlay.quotes if overlay is not None and overlay.snapshot_id == snapshot.snapshot_id else {}
    displayed_quotes = current_quotes if historical and current_quotes is not None else live_quotes
    return {
        "schema_version": API_SCHEMA_VERSION,
        "status": "ready",
        "snapshot_id": snapshot.snapshot_id,
        "strategy": snapshot.strategy.value,
        "trade_date": snapshot.trade_date,
        "requested_date": requested_date,
        "current_trade_date": current_trade_date,
        "historical": historical,
        "view": "history" if historical else view,
        "phase": snapshot.phase,
        "published_at": snapshot.published_at.isoformat(),
        "strategy_version": snapshot.strategy_version,
        "fusion_mode": snapshot.fusion_mode.value,
        "stale": snapshot.stale,
        "frozen": snapshot.frozen,
        "degraded_reasons": list(snapshot.degraded_reasons),
        "filtered_count": snapshot.filtered_count,
        "items": [
            _recommendation(
                item,
                displayed_quotes.get(item.features.quote.code),
                historical=historical,
            )
            for item in snapshot.recommendations[:top_n]
        ],
        "error": None,
    }


def empty_snapshot_envelope(
    strategy: str,
    trade_date: str | None = None,
    *,
    current_trade_date: str | None = None,
    view: str = "official",
) -> dict[str, object]:
    return {
        "schema_version": API_SCHEMA_VERSION,
        "status": "not_ready",
        "snapshot_id": None,
        "strategy": strategy,
        "trade_date": trade_date,
        "requested_date": trade_date,
        "current_trade_date": current_trade_date,
        "historical": trade_date is not None,
        "view": "history" if trade_date is not None else view,
        "phase": None,
        "published_at": None,
        "strategy_version": None,
        "fusion_mode": "local_degraded",
        "stale": True,
        "frozen": False,
        "degraded_reasons": ["snapshot_not_ready"],
        "filtered_count": 0,
        "items": [],
        "error": None,
    }


def error_envelope(
    code: str,
    message: str,
    *,
    details: Mapping[str, object] | None = None,
    strategy: str | None = None,
    trade_date: str | None = None,
) -> dict[str, object]:
    return {
        "schema_version": API_SCHEMA_VERSION,
        "status": "error",
        "snapshot_id": None,
        "strategy": strategy,
        "trade_date": trade_date,
        "requested_date": trade_date,
        "current_trade_date": None,
        "historical": trade_date is not None,
        "view": "history" if trade_date is not None else "official",
        "phase": None,
        "published_at": None,
        "strategy_version": None,
        "fusion_mode": None,
        "stale": True,
        "frozen": False,
        "degraded_reasons": [],
        "filtered_count": 0,
        "items": [],
        "error": {
            "code": code,
            "message": message,
            "details": dict(details or {}),
        },
    }


def _recommendation(
    item: Recommendation,
    live_quote: LiveQuote | None = None,
    *,
    historical: bool = False,
) -> dict[str, object]:
    quote = item.features.quote
    score = item.score
    displayed_price = live_quote.price if live_quote is not None else (None if historical else quote.price)
    displayed_change = live_quote.pct_change if live_quote is not None else (None if historical else quote.pct_change)
    displayed_source = live_quote.source if live_quote is not None else (None if historical else quote.source)
    displayed_source_time = (
        live_quote.source_time if live_quote is not None else (None if historical else quote.source_time)
    )
    displayed_data_version = (
        live_quote.data_version if live_quote is not None else (None if historical else quote.data_version)
    )
    return {
        "rank": item.rank,
        "code": quote.code,
        "name": quote.name,
        "industry": quote.industry,
        "price": displayed_price,
        "pct_change": displayed_change,
        "turnover_rate": quote.turnover_rate,
        "amount": quote.amount,
        "market_cap": quote.market_cap,
        "source": displayed_source,
        "source_time": displayed_source_time.isoformat() if displayed_source_time is not None else None,
        "quote_data_version": displayed_data_version,
        "anchor_price": quote.price,
        "anchor_daily_return_pct": quote.pct_change,
        "anchor_to_now_pct": _anchor_to_now(quote.price, live_quote.price) if live_quote is not None else None,
        "action": item.action.value,
        "action_reason": item.action_reason,
        "setup_type": item.downside.setup_type if item.downside is not None else None,
        "downside": (
            {
                "status": item.downside.status,
                "reasons": list(item.downside.reasons),
                "atr20_pct": item.downside.atr20_pct,
                "intraday_reversal_atr": item.downside.intraday_reversal_atr,
                "historical_drawdown_pct": item.downside.historical_drawdown_pct,
            }
            if item.downside is not None
            else None
        ),
        "scores": {
            "local_score": score.local_score,
            "deepseek_score": score.deepseek_score,
            "deepseek_risk_penalty": score.deepseek_risk_penalty,
            "final_score": score.final_score,
        },
        "risks": _risk_summaries((*item.local_risk_facts, *item.deepseek_risk_facts)),
        "review": _review_summary(item.review),
    }


def _risk_summaries(facts: tuple[RiskFact, ...]) -> list[dict[str, object]]:
    seen: set[str] = set()
    result: list[dict[str, object]] = []
    for fact in facts:
        if fact.risk_fact_id in seen:
            continue
        seen.add(fact.risk_fact_id)
        result.append(
            {
                "risk_code": fact.risk_code,
                "severity": fact.severity,
                "penalty": fact.penalty,
                "assessment": fact.assessment,
            }
        )
    return result


def _review_summary(review: DeepSeekReview | None) -> dict[str, object] | None:
    if review is None:
        return None
    return {"outcome": review.outcome.value, "error": review.error}


def _anchor_to_now(anchor_price: float | None, current_price: float | None) -> float | None:
    if anchor_price is None or anchor_price <= 0 or current_price is None:
        return None
    return round((current_price / anchor_price - 1.0) * 100.0, 4)


__all__ = [
    "API_SCHEMA_VERSION",
    "empty_snapshot_envelope",
    "error_envelope",
    "snapshot_envelope",
]
