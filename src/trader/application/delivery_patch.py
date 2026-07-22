"""Compact schema-v2 SSE patches derived from immutable domain snapshots."""

from __future__ import annotations

from trader.domain.recommendation.models import LiveOverlay, Recommendation, RecommendationSnapshot


def snapshot_patch(snapshot: RecommendationSnapshot, *, base_snapshot_id: str | None) -> dict[str, object]:
    return {
        "schema_version": 2,
        "base_snapshot_id": base_snapshot_id,
        "snapshot_id": snapshot.snapshot_id,
        "strategy": snapshot.strategy.value,
        "trade_date": snapshot.trade_date,
        "phase": snapshot.phase,
        "published_at": snapshot.published_at.isoformat(),
        "strategy_version": snapshot.strategy_version,
        "fusion_mode": snapshot.fusion_mode.value,
        "stale": snapshot.stale,
        "frozen": snapshot.frozen,
        "degraded_reasons": list(snapshot.degraded_reasons),
        "filtered_count": snapshot.filtered_count,
        "replace": True,
        "upserts": [_recommendation(item) for item in snapshot.recommendations],
        "removals": [],
    }


def overlay_patch(overlay: LiveOverlay) -> dict[str, object]:
    return {
        "schema_version": 2,
        "snapshot_id": overlay.snapshot_id,
        "strategy": overlay.strategy.value,
        "trade_date": overlay.trade_date,
        "overlay_version": overlay.version,
        "observed_at": overlay.observed_at.isoformat(),
        "closing": overlay.closing,
        "quotes": [
            {
                "code": code,
                "price": quote.price,
                "pct_change": quote.pct_change,
                "source": quote.source,
                "source_time": quote.source_time.isoformat(),
                "quote_data_version": quote.data_version,
            }
            for code, quote in sorted(overlay.quotes.items())
        ],
    }


def _recommendation(item: Recommendation) -> dict[str, object]:
    quote = item.features.quote
    score = item.score
    return {
        "rank": item.rank,
        "code": quote.code,
        "name": quote.name,
        "industry": quote.industry,
        "price": quote.price,
        "pct_change": quote.pct_change,
        "turnover_rate": quote.turnover_rate,
        "amount": quote.amount,
        "market_cap": quote.market_cap,
        "source": quote.source,
        "source_time": quote.source_time.isoformat(),
        "quote_data_version": quote.data_version,
        "anchor_price": quote.price,
        "anchor_daily_return_pct": quote.pct_change,
        "anchor_to_now_pct": None,
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
        "risks": _risks(item),
        "review": None if item.review is None else {"outcome": item.review.outcome.value, "error": item.review.error},
    }


def _risks(item: Recommendation) -> list[dict[str, object]]:
    seen: set[str] = set()
    values: list[dict[str, object]] = []
    for fact in (*item.local_risk_facts, *item.deepseek_risk_facts):
        if fact.risk_fact_id in seen:
            continue
        seen.add(fact.risk_fact_id)
        values.append(
            {
                "risk_code": fact.risk_code,
                "severity": fact.severity,
                "penalty": fact.penalty,
                "assessment": fact.assessment,
            }
        )
    return values


__all__ = ["overlay_patch", "snapshot_patch"]
