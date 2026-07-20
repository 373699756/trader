"""Versioned JSON envelopes for recommendation delivery."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from trader.domain.models import (
    DeepSeekReview,
    DimensionAssessment,
    LiveOverlay,
    LiveQuote,
    Recommendation,
    RecommendationSnapshot,
    RiskFact,
)

API_SCHEMA_VERSION = "v2"


def snapshot_envelope(
    snapshot: RecommendationSnapshot,
    *,
    top_n: int,
    overlay: LiveOverlay | None = None,
    fallback_date: str | None = None,
    fallback_reason: str | None = None,
    requested_date: str | None = None,
    current_trade_date: str | None = None,
    historical: bool = False,
    current_quotes: Mapping[str, LiveQuote] | None = None,
) -> dict[str, object]:
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
        "phase": snapshot.phase,
        "published_at": snapshot.published_at.isoformat(),
        "data_version": snapshot.data_version,
        "strategy_version": snapshot.strategy_version,
        "config_version": snapshot.config_version,
        "fusion_version": snapshot.fusion_version,
        "fusion_mode": snapshot.fusion_mode.value,
        "stale": snapshot.stale,
        "frozen": snapshot.frozen,
        "degraded_reasons": list(snapshot.degraded_reasons),
        "filtered_count": snapshot.filtered_count,
        "filter_reasons": dict(snapshot.filter_reasons),
        "filter_details": [
            {
                "stock_code": item.stock_code,
                "filter_code": item.filter_code,
                "threshold": item.threshold,
                "actual": item.actual,
                "source": item.source,
                "observed_at": item.observed_at.isoformat(),
            }
            for item in snapshot.filter_details
        ],
        "metadata": dict(snapshot.metadata),
        "weights": _snapshot_weights(snapshot),
        "fallback_date": fallback_date,
        "fallback_reason": fallback_reason,
        "live_overlay": _live_overlay(overlay) if overlay is not None else None,
        "items": [
            _recommendation(item, displayed_quotes.get(item.features.quote.code))
            for item in snapshot.recommendations[:top_n]
        ],
        "error": None,
    }


def empty_snapshot_envelope(
    strategy: str,
    trade_date: str | None = None,
    *,
    current_trade_date: str | None = None,
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
        "phase": None,
        "published_at": None,
        "data_version": None,
        "strategy_version": None,
        "config_version": None,
        "fusion_version": None,
        "fusion_mode": "local_degraded",
        "stale": True,
        "frozen": False,
        "degraded_reasons": ["snapshot_not_ready"],
        "filtered_count": 0,
        "filter_reasons": {},
        "filter_details": [],
        "metadata": {},
        "weights": {},
        "fallback_date": None,
        "fallback_reason": None,
        "live_overlay": None,
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
        "phase": None,
        "published_at": None,
        "data_version": None,
        "strategy_version": None,
        "config_version": None,
        "fusion_version": None,
        "fusion_mode": None,
        "stale": True,
        "frozen": False,
        "degraded_reasons": [],
        "weights": {},
        "items": [],
        "error": {
            "code": code,
            "message": message,
            "details": dict(details or {}),
        },
    }


def _recommendation(item: Recommendation, live_quote: LiveQuote | None = None) -> dict[str, object]:
    quote = item.features.quote
    score = item.score
    missing_fields = list(item.features.missing_fields)
    return {
        "rank": item.rank,
        "code": quote.code,
        "name": quote.name,
        "industry": quote.industry,
        "price": live_quote.price if live_quote is not None else quote.price,
        "previous_close": quote.previous_close,
        "pct_change": live_quote.pct_change if live_quote is not None else quote.pct_change,
        "change_5m": quote.change_5m,
        "speed": quote.speed,
        "volume_ratio": quote.volume_ratio,
        "turnover_rate": quote.turnover_rate,
        "amount": quote.amount,
        "amplitude": quote.amplitude,
        "market_cap": quote.market_cap,
        "source": live_quote.source if live_quote is not None else quote.source,
        "source_time": (live_quote.source_time if live_quote is not None else quote.source_time).isoformat(),
        "received_time": (live_quote.received_time if live_quote is not None else quote.received_time).isoformat(),
        "quote_data_version": live_quote.data_version if live_quote is not None else quote.data_version,
        "anchor_price": quote.price,
        "anchor_daily_return_pct": quote.pct_change,
        "anchor_to_now_pct": _anchor_to_now(quote.price, live_quote.price) if live_quote is not None else None,
        "target_price": item.target_price,
        "action": item.action.value,
        "action_reason": item.action_reason,
        "veto": item.veto,
        "scores": {
            "components": dict(score.components),
            "base_score": score.base_score,
            "local_risk_penalty": score.local_risk_penalty,
            "local_risk_penalty_before_cap": sum(max(0.0, fact.penalty) for fact in item.local_risk_facts),
            "local_score": score.local_score,
            "deepseek_score": score.deepseek_score,
            "confidence_coverage": score.confidence_coverage,
            "deepseek_risk_penalty": score.deepseek_risk_penalty,
            "deepseek_risk_penalty_before_cap": sum(max(0.0, fact.penalty) for fact in item.deepseek_risk_facts),
            "final_score": score.final_score,
            "fusion_mode": score.fusion_mode.value,
            "fusion_applied": score.fusion_applied,
        },
        "features": dict(item.features.values),
        "normalization": {
            factor_id: {
                "lower_bound": stats.lower_bound,
                "upper_bound": stats.upper_bound,
                "sample_size": stats.sample_size,
                "missing_count": stats.missing_count,
                "lower_quantile": stats.lower_quantile,
                "upper_quantile": stats.upper_quantile,
                "population_data_version": stats.population_data_version,
            }
            for factor_id, stats in item.features.normalization.items()
        },
        "missing_fields": missing_fields,
        "missing_reasons": {field: _missing_reason(field) for field in missing_fields},
        "market_regime": item.features.market_regime,
        "history_days": item.features.history_days,
        "evidence": [
            {
                "evidence_id": evidence.evidence_id,
                "type": evidence.evidence_type,
                "title": evidence.title,
                "source": evidence.source,
                "published_at": evidence.published_at.isoformat(),
                "received_at": evidence.received_at.isoformat() if evidence.received_at is not None else None,
                "data_version": evidence.data_version,
            }
            for evidence in item.features.evidence
        ],
        "local_risk_facts": [_risk_fact(fact) for fact in item.local_risk_facts],
        "deepseek_risk_facts": [_risk_fact(fact) for fact in item.deepseek_risk_facts],
        "review": _review(item.review) if item.review is not None else None,
    }


def _live_overlay(overlay: LiveOverlay) -> dict[str, object]:
    return {
        "version": overlay.version,
        "observed_at": overlay.observed_at.isoformat(),
        "closing": overlay.closing,
    }


def _snapshot_weights(snapshot: RecommendationSnapshot) -> dict[str, object]:
    replay = snapshot.replay_input
    if replay is None:
        return {}
    policy = replay.policy
    return {
        "fusion": {"local": policy.local_weight, "deepseek": policy.deepseek_weight},
        "deepseek_dimensions": dict(policy.dimension_weights.get(snapshot.strategy.value, {})),
    }


def _anchor_to_now(anchor_price: float | None, current_price: float | None) -> float | None:
    if anchor_price is None or anchor_price <= 0 or current_price is None:
        return None
    return round((current_price / anchor_price - 1.0) * 100.0, 4)


def _missing_reason(field: str) -> str:
    if field in {"news_sentiment", "evidence_freshness"}:
        return "新闻或公告证据不可用"
    if field in {
        "value_score",
        "growth_score",
        "quality_score",
        "risk_protection_score",
        "financial_deterioration",
        "pledge_risk",
        "reduction_or_unlock",
        "negative_announcement_level",
    }:
        return "财务或公司事件数据不可用或不满足点时规则"
    if field in {
        "tail_return_30m_pct",
        "tail_return_30m",
        "tail_volume_ratio_raw",
        "tail_volume_ratio",
    }:
        return "尾盘分钟数据不可用或样本不足"
    if field in {
        "industry_policy_score",
        "industry_strength",
        "industry_breadth",
        "industry_trend",
    }:
        return "行业数据不可用"
    if field == "speed_percentile":
        return "行情源未提供瞬时速度"
    return "当前快照缺少上游输入"


def _review(review: DeepSeekReview) -> dict[str, object]:
    return {
        "outcome": review.outcome.value,
        "completed_at": review.completed_at.isoformat(),
        "error": review.error,
        "rating": review.rating,
        "review_stage": review.review_stage,
        "challenger_status": review.challenger_status,
        "requested_model": review.requested_model,
        "actual_model": review.actual_model,
        "thinking_mode": review.thinking_mode,
        "raw_confidence": review.raw_confidence,
        "calibrated_confidence": review.calibrated_confidence,
        "evidence_manifest_hash": review.evidence_manifest_hash,
        "calibration_version": review.calibration_version,
        "model_role": review.model_role,
        "reasoning_effort": review.reasoning_effort,
        "system_fingerprint": review.system_fingerprint,
        "prompt_cache_hit_tokens": review.prompt_cache_hit_tokens,
        "prompt_cache_miss_tokens": review.prompt_cache_miss_tokens,
        "challenger_requested_model": review.challenger_requested_model,
        "challenger_actual_model": review.challenger_actual_model,
        "challenger_thinking_mode": review.challenger_thinking_mode,
        "challenger_reasoning_effort": review.challenger_reasoning_effort,
        "challenger_system_fingerprint": review.challenger_system_fingerprint,
        "challenger_prompt_cache_hit_tokens": review.challenger_prompt_cache_hit_tokens,
        "challenger_prompt_cache_miss_tokens": review.challenger_prompt_cache_miss_tokens,
        "dimensions": {name: _dimension(value) for name, value in review.dimensions.items()},
        "risk_facts": [_risk_fact(fact) for fact in review.risk_facts],
    }


def _dimension(dimension: DimensionAssessment) -> dict[str, object]:
    return {
        "score": dimension.score,
        "confidence": dimension.confidence,
        "assessment": dimension.assessment,
        "flags": list(dimension.flags),
        "evidence_ids": list(dimension.evidence_ids),
        "unknown": dimension.is_unknown,
    }


def _risk_fact(fact: RiskFact) -> dict[str, object]:
    return {
        "risk_fact_id": fact.risk_fact_id,
        "risk_code": fact.risk_code,
        "severity": fact.severity,
        "penalty": fact.penalty,
        "source": fact.source,
        "observed_at": _isoformat(fact.observed_at),
        "confidence": fact.confidence,
        "evidence_ids": list(fact.evidence_ids),
        "group": fact.group,
        "threshold": fact.threshold,
        "actual": fact.actual,
        "veto": fact.veto,
        "assessment": fact.assessment,
    }


def _isoformat(value: datetime) -> str:
    return value.isoformat()


__all__ = [
    "API_SCHEMA_VERSION",
    "empty_snapshot_envelope",
    "error_envelope",
    "snapshot_envelope",
]
