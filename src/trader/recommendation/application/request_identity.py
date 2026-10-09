"""Stable identities for recommendation requests and immutable inputs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from trader.recommendation.domain.evidence.review import RiskFact
from trader.recommendation.domain.market.models import (
    BoardPopulation,
    CrossSectionStats,
    Evidence,
    FeatureSnapshot,
    MarketQuote,
    ModelIndustryReference,
)


def request_fingerprint(request: Mapping[str, object]) -> str:
    payload = json.dumps(
        _normalize_request(request),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=_canonical_value,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _normalize_request(request: Mapping[str, object]) -> dict[str, object]:
    normalized: dict[str, object] = {}
    for key, value in request.items():
        if key in {"codes", "fields"} and isinstance(value, (tuple, list, set, frozenset)):
            normalized[key] = sorted({str(item) for item in value})
        elif isinstance(value, Mapping):
            normalized[key] = _normalize_request(value)
        else:
            normalized[key] = value
    return normalized


def _canonical_value(value: object) -> object:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("request identity decimals must be finite")
        return format(value, "f")
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("request identity datetime must be timezone-aware")
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=repr)
    if isinstance(value, FeatureSnapshot):
        return _feature_material(value)
    raise TypeError(f"unsupported request identity value: {type(value).__name__}")


def _feature_material(feature: FeatureSnapshot) -> Mapping[str, object]:
    """Keep the accepted input identity explicit and independent of dataclass expansion."""
    return {
        "quote": _quote_material(feature.quote),
        "values": feature.values,
        "observed_at": feature.observed_at,
        "history_days": feature.history_days,
        "market_regime": feature.market_regime,
        "missing_fields": feature.missing_fields,
        "evidence": tuple(_evidence_material(item) for item in feature.evidence),
        "external_risk_facts": tuple(_risk_material(item) for item in feature.external_risk_facts),
        "normalization": {key: _normalization_material(item) for key, item in feature.normalization.items()},
        "missing_reasons": feature.missing_reasons,
        "board_data_reliability": feature.board_data_reliability,
        "board_supported_weight": feature.board_supported_weight,
        "board_policy_id": feature.board_policy_id,
        "board_policy_version": feature.board_policy_version,
        "board_population": _population_material(feature.board_population)
        if feature.board_population is not None
        else None,
        "merge_epoch": feature.merge_epoch,
        "competition_group_id": feature.competition_group_id,
        "competition_group_source": feature.competition_group_source,
        "competition_group_version": feature.competition_group_version,
        "liquidity_bucket": feature.liquidity_bucket,
        "parameter_status": feature.parameter_status,
        "selection_skip_reason": feature.selection_skip_reason,
        "model_industry": _industry_material(feature.model_industry) if feature.model_industry is not None else None,
    }


def _quote_material(quote: MarketQuote) -> Mapping[str, object]:
    return {
        "code": quote.code,
        "name": quote.name,
        "price": quote.price,
        "previous_close": quote.previous_close,
        "open_price": quote.open_price,
        "high": quote.high,
        "low": quote.low,
        "pct_change": quote.pct_change,
        "change_5m": quote.change_5m,
        "speed": quote.speed,
        "volume_ratio": quote.volume_ratio,
        "turnover_rate": quote.turnover_rate,
        "amount": quote.amount,
        "amplitude": quote.amplitude,
        "market_cap": quote.market_cap,
        "industry": quote.industry,
        "source": quote.source,
        "source_time": quote.source_time,
        "received_time": quote.received_time,
        "data_version": quote.data_version,
        "is_st": quote.is_st,
        "is_suspended": quote.is_suspended,
        "is_one_price_limit": quote.is_one_price_limit,
        "is_blacklisted": quote.is_blacklisted,
        "has_major_regulatory_risk": quote.has_major_regulatory_risk,
        "cross_source_deviation_pct": quote.cross_source_deviation_pct,
        "cross_source_verified": quote.cross_source_verified,
        "board": quote.board,
        "board_source": quote.board_source,
        "board_reliability": quote.board_reliability,
        "exchange": quote.exchange,
        "listing_date": quote.listing_date,
        "listing_age_sessions": quote.listing_age_sessions,
        "is_relisted_first_session": quote.is_relisted_first_session,
        "is_delisting_period_first_session": quote.is_delisting_period_first_session,
        "has_price_limit": quote.has_price_limit,
        "exchange_limit_pct": quote.exchange_limit_pct,
        "strategy_hot_cap_pct": quote.strategy_hot_cap_pct,
        "rule_version": quote.rule_version,
        "rule_effective_date": quote.rule_effective_date,
        "execution_restrictions": quote.execution_restrictions,
    }


def _evidence_material(evidence: Evidence) -> Mapping[str, object]:
    return {
        "evidence_id": evidence.evidence_id,
        "evidence_type": evidence.evidence_type,
        "title": evidence.title,
        "source": evidence.source,
        "published_at": evidence.published_at,
        "received_at": evidence.received_at,
        "data_version": evidence.data_version,
    }


def _risk_material(fact: RiskFact) -> Mapping[str, object]:
    return {
        "risk_fact_id": fact.risk_fact_id,
        "risk_code": fact.risk_code,
        "severity": fact.severity,
        "penalty": fact.penalty,
        "source": fact.source,
        "observed_at": fact.observed_at,
        "confidence": fact.confidence,
        "evidence_ids": fact.evidence_ids,
        "group": fact.group,
        "veto": fact.veto,
        "threshold": fact.threshold,
        "actual": fact.actual,
        "assessment": fact.assessment,
    }


def _normalization_material(stats: CrossSectionStats) -> Mapping[str, object]:
    return {
        "lower_bound": stats.lower_bound,
        "upper_bound": stats.upper_bound,
        "sample_size": stats.sample_size,
        "missing_count": stats.missing_count,
        "lower_quantile": stats.lower_quantile,
        "upper_quantile": stats.upper_quantile,
        "population_data_version": stats.population_data_version,
    }


def _population_material(population: BoardPopulation) -> Mapping[str, object]:
    return {
        "trade_date": population.trade_date,
        "phase": population.phase,
        "board": population.board,
        "data_version": population.data_version,
        "schema_version": population.schema_version,
        "population_version": population.population_version,
        "sample_size": population.sample_size,
        "missing_count": population.missing_count,
        "liquidity_p50": population.liquidity_p50,
        "liquidity_p80": population.liquidity_p80,
        "fallback_trade_date": population.fallback_trade_date,
        "fallback_age_sessions": population.fallback_age_sessions,
        "status": population.status,
    }


def _industry_material(industry: ModelIndustryReference) -> Mapping[str, object]:
    return {
        "industry_id": industry.industry_id,
        "classification": industry.classification,
        "effective_date": industry.effective_date,
        "source": industry.source,
        "data_version": industry.data_version,
    }


__all__ = ["request_fingerprint"]
