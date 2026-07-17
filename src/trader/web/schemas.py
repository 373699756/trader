"""Versioned JSON envelopes for recommendation delivery."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from trader.domain.models import (
    DeepSeekReview,
    DimensionAssessment,
    Recommendation,
    RecommendationSnapshot,
    RiskFact,
)

API_SCHEMA_VERSION = "v2"


def snapshot_envelope(snapshot: RecommendationSnapshot, *, top_n: int) -> dict[str, object]:
    return {
        "schema_version": API_SCHEMA_VERSION,
        "status": "ready",
        "snapshot_id": snapshot.snapshot_id,
        "strategy": snapshot.strategy.value,
        "trade_date": snapshot.trade_date,
        "phase": snapshot.phase,
        "published_at": snapshot.published_at.isoformat(),
        "data_version": snapshot.data_version,
        "strategy_version": snapshot.strategy_version,
        "fusion_version": snapshot.fusion_version,
        "fusion_mode": snapshot.fusion_mode.value,
        "stale": snapshot.stale,
        "frozen": snapshot.frozen,
        "degraded_reasons": list(snapshot.degraded_reasons),
        "filtered_count": snapshot.filtered_count,
        "filter_reasons": dict(snapshot.filter_reasons),
        "metadata": dict(snapshot.metadata),
        "items": [_recommendation(item) for item in snapshot.recommendations[:top_n]],
        "error": None,
    }


def empty_snapshot_envelope(strategy: str, trade_date: str | None = None) -> dict[str, object]:
    return {
        "schema_version": API_SCHEMA_VERSION,
        "status": "not_ready",
        "snapshot_id": None,
        "strategy": strategy,
        "trade_date": trade_date,
        "phase": None,
        "published_at": None,
        "data_version": None,
        "strategy_version": None,
        "fusion_version": None,
        "fusion_mode": "local_degraded",
        "stale": True,
        "frozen": False,
        "degraded_reasons": ["snapshot_not_ready"],
        "filtered_count": 0,
        "filter_reasons": {},
        "metadata": {},
        "items": [],
        "error": None,
    }


def error_envelope(code: str, message: str, *, details: Mapping[str, object] | None = None) -> dict[str, object]:
    return {
        "schema_version": API_SCHEMA_VERSION,
        "status": "error",
        "snapshot_id": None,
        "published_at": None,
        "data_version": None,
        "strategy_version": None,
        "fusion_version": None,
        "fusion_mode": None,
        "stale": True,
        "degraded_reasons": [],
        "items": [],
        "error": {
            "code": code,
            "message": message,
            "details": dict(details or {}),
        },
    }


def _recommendation(item: Recommendation) -> dict[str, object]:
    quote = item.features.quote
    score = item.score
    missing_fields = list(item.features.missing_fields)
    return {
        "rank": item.rank,
        "code": quote.code,
        "name": quote.name,
        "industry": quote.industry,
        "price": quote.price,
        "previous_close": quote.previous_close,
        "pct_change": quote.pct_change,
        "change_5m": quote.change_5m,
        "speed": quote.speed,
        "volume_ratio": quote.volume_ratio,
        "turnover_rate": quote.turnover_rate,
        "amount": quote.amount,
        "amplitude": quote.amplitude,
        "market_cap": quote.market_cap,
        "source": quote.source,
        "source_time": quote.source_time.isoformat(),
        "received_time": quote.received_time.isoformat(),
        "quote_data_version": quote.data_version,
        "anchor_price": quote.price,
        "anchor_daily_return_pct": quote.pct_change,
        "target_price": item.target_price,
        "action": item.action.value,
        "action_reason": item.action_reason,
        "veto": item.veto,
        "scores": {
            "components": dict(score.components),
            "base_score": score.base_score,
            "local_risk_penalty": score.local_risk_penalty,
            "local_score": score.local_score,
            "deepseek_score": score.deepseek_score,
            "confidence_coverage": score.confidence_coverage,
            "deepseek_risk_penalty": score.deepseek_risk_penalty,
            "final_score": score.final_score,
            "fusion_mode": score.fusion_mode.value,
            "fusion_applied": score.fusion_applied,
        },
        "features": dict(item.features.values),
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
            }
            for evidence in item.features.evidence
        ],
        "local_risk_facts": [_risk_fact(fact) for fact in item.local_risk_facts],
        "deepseek_risk_facts": [_risk_fact(fact) for fact in item.deepseek_risk_facts],
        "review": _review(item.review) if item.review is not None else None,
    }


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
        return "财务或公司事件数据尚未接入"
    if field in {"tail_return_30m", "tail_volume_ratio"}:
        return "尾盘分钟数据尚未接入"
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
        "veto": fact.veto,
    }


def _isoformat(value: datetime) -> str:
    return value.isoformat()


__all__ = [
    "API_SCHEMA_VERSION",
    "empty_snapshot_envelope",
    "error_envelope",
    "snapshot_envelope",
]
