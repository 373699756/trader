"""Compatibility projection from unified decisions to the July dashboard contract."""

from __future__ import annotations

from collections.abc import Mapping

from trader.application.decision_queries import DecisionItemView, DecisionView
from trader.domain.market.models import MarketQuote
from trader.domain.recommendation.models import Strategy


def serialize_legacy_decision(
    view: DecisionView,
    *,
    top_n: int,
    quotes: Mapping[str, MarketQuote] | None = None,
) -> dict[str, object]:
    historical = view.view == "history"
    items = view.items if view.strategy is Strategy.LONG else view.items[:top_n]
    published_at = view.observed_at.isoformat() if view.observed_at is not None else None
    trade_date = view.trade_date.isoformat() if view.trade_date is not None else None
    return {
        "schema_version": "v3",
        "status": view.status,
        "snapshot_id": view.decision_version,
        "projection_version": view.decision_version,
        "strategy": view.strategy.value,
        "trade_date": trade_date,
        "requested_date": trade_date if historical else None,
        "current_trade_date": trade_date if not historical else None,
        "historical": historical,
        "view": "history" if historical else ("official" if view.frozen else "live"),
        "phase": view.stage,
        "published_at": published_at,
        "strategy_version": dict(view.input_versions).get("strategy"),
        "fusion_mode": "unified_v2",
        "score_status": view.score_status,
        "stale": view.status != "ready",
        "frozen": view.frozen,
        "degraded_reasons": list(view.degraded_reasons),
        "filtered_count": view.coverage.rejected_count,
        "selection_diagnostics": _selection_diagnostics(view),
        "readiness_reason": view.degraded_reasons[0] if view.status != "ready" and view.degraded_reasons else None,
        "long_groups": [],
        "items": [_legacy_item(item, (quotes or {}).get(item.code)) for item in items],
        "error": None,
    }


def _selection_diagnostics(view: DecisionView) -> dict[str, object]:
    final_scores = tuple(item.final_score for item in view.items if item.final_score is not None)
    local_scores = tuple(item.local_score for item in view.items if item.local_score is not None)
    return {
        "scored_candidate_count": view.coverage.evaluated_count,
        "actionable_candidate_count": view.coverage.selected_count,
        "score_qualified_count": view.coverage.selected_count,
        "selection_floor": None,
        "executable_threshold": None,
        "observation_floor": None,
        "executable_limit": view.coverage.executable_count,
        "observation_limit": view.coverage.observation_count,
        "selected_executable_count": view.coverage.executable_count,
        "selected_observation_count": view.coverage.observation_count,
        "blocked_reason_counts": dict(view.filter_reason_counts),
        "selection_skip_reason_counts": {},
        "maximum_local_score": max(local_scores, default=None),
        "maximum_final_score": max(final_scores, default=None),
        "empty_reason": None if view.items else "current_decision_unavailable",
    }


def _legacy_item(item: DecisionItemView, quote: MarketQuote | None) -> dict[str, object]:
    source_time_value = item.quote_time or (quote.source_time if quote is not None else None)
    source_time = source_time_value.isoformat() if source_time_value is not None else None
    return {
        "rank": item.rank,
        "code": item.code,
        "name": item.name or (quote.name if quote is not None else ""),
        "industry": item.industry or (quote.industry if quote is not None else ""),
        "price": item.price if item.price is not None else (quote.price if quote is not None else None),
        "pct_change": item.pct_change
        if item.pct_change is not None
        else (quote.pct_change if quote is not None else None),
        "turnover_rate": item.turnover_rate
        if item.turnover_rate is not None
        else (quote.turnover_rate if quote is not None else None),
        "amount": item.amount if item.amount is not None else (quote.amount if quote is not None else None),
        "market_cap": item.market_cap
        if item.market_cap is not None
        else (quote.market_cap if quote is not None else None),
        "source": item.quote_source or (quote.source if quote is not None else None),
        "source_time": source_time,
        "quote_data_version": None,
        "anchor_price": item.price if item.price is not None else (quote.price if quote is not None else None),
        "anchor_source_time": source_time,
        "anchor_daily_return_pct": item.pct_change
        if item.pct_change is not None
        else (quote.pct_change if quote is not None else None),
        "anchor_to_now_pct": None,
        "action": item.action,
        "action_reason": item.action_reason,
        "setup_type": None,
        "downside": None,
        "scores": {
            "candidate_score": item.candidate_score,
            "local_score": item.local_score,
            "deepseek_score": item.deepseek_score,
            "deepseek_risk_penalty": item.deepseek_risk_penalty,
            "final_score": item.final_score,
        },
        "research": {
            "status": "unavailable",
            "covered_components": 0,
            "total_components": 0,
            "components": {},
        },
        "risks": [
            {"risk_code": code, "severity": None, "penalty": None, "assessment": None} for code in item.risk_codes
        ],
        "review": None,
    }


__all__ = ["serialize_legacy_decision"]
