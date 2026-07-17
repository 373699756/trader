"""Canonical JSON serialization for published and frozen recommendation snapshots."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from datetime import datetime

from trader.domain.models import (
    DeepSeekReview,
    DimensionAssessment,
    Evidence,
    FeatureSnapshot,
    FrozenReplayPolicy,
    FusionMode,
    MarketQuote,
    Recommendation,
    RecommendationAction,
    RecommendationReplayInput,
    RecommendationSnapshot,
    ReviewOutcome,
    RiskFact,
    RiskRule,
    ScoreBreakdown,
    Strategy,
)

SNAPSHOT_SCHEMA_VERSION = "recommendation_snapshot_v2"


def snapshot_to_dict(snapshot: RecommendationSnapshot) -> dict[str, object]:
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "snapshot_id": snapshot.snapshot_id,
        "strategy": snapshot.strategy.value,
        "trade_date": snapshot.trade_date,
        "phase": snapshot.phase,
        "data_version": snapshot.data_version,
        "strategy_version": snapshot.strategy_version,
        "config_version": snapshot.config_version,
        "fusion_version": snapshot.fusion_version,
        "fusion_mode": snapshot.fusion_mode.value,
        "published_at": snapshot.published_at.isoformat(),
        "filtered_count": snapshot.filtered_count,
        "filter_reasons": dict(snapshot.filter_reasons),
        "stale": snapshot.stale,
        "frozen": snapshot.frozen,
        "degraded_reasons": list(snapshot.degraded_reasons),
        "metadata": dict(snapshot.metadata),
        "replay_input": _replay_input_to_dict(snapshot.replay_input) if snapshot.replay_input is not None else None,
        "recommendations": [_recommendation_to_dict(item) for item in snapshot.recommendations],
    }


def snapshot_bytes(snapshot: RecommendationSnapshot) -> bytes:
    return json.dumps(
        snapshot_to_dict(snapshot),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def snapshot_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def snapshot_from_dict(raw: Mapping[str, object]) -> RecommendationSnapshot:
    if raw.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise ValueError("unsupported recommendation snapshot schema")
    recommendations_raw = raw.get("recommendations")
    if not isinstance(recommendations_raw, list):
        raise ValueError("recommendations must be a list")
    recommendations = tuple(_recommendation_from_dict(item) for item in recommendations_raw if isinstance(item, dict))
    filter_reasons = raw.get("filter_reasons")
    metadata = raw.get("metadata")
    replay_raw = raw.get("replay_input")
    degraded_raw = raw.get("degraded_reasons")
    return RecommendationSnapshot(
        snapshot_id=_text(raw, "snapshot_id"),
        strategy=Strategy(_text(raw, "strategy")),
        trade_date=_text(raw, "trade_date"),
        phase=_text(raw, "phase"),
        data_version=_text(raw, "data_version"),
        strategy_version=_text(raw, "strategy_version"),
        config_version=str(raw.get("config_version") or "legacy-unrecorded"),
        fusion_version=_text(raw, "fusion_version"),
        fusion_mode=FusionMode(_text(raw, "fusion_mode")),
        published_at=datetime.fromisoformat(_text(raw, "published_at")),
        recommendations=recommendations,
        filtered_count=_integer(raw, "filtered_count"),
        filter_reasons={str(key): int(value) for key, value in filter_reasons.items()}
        if isinstance(filter_reasons, dict)
        else {},
        stale=bool(raw.get("stale")),
        frozen=bool(raw.get("frozen")),
        degraded_reasons=tuple(str(value) for value in degraded_raw if isinstance(value, str))
        if isinstance(degraded_raw, list)
        else (),
        metadata=dict(metadata) if isinstance(metadata, dict) else {},
        replay_input=_replay_input_from_dict(replay_raw) if isinstance(replay_raw, dict) else None,
    )


def _replay_input_to_dict(replay_input: RecommendationReplayInput) -> dict[str, object]:
    return {
        "schema_version": replay_input.schema_version,
        "algorithm_version": replay_input.algorithm_version,
        "policy": _replay_policy_to_dict(replay_input.policy),
        "evaluated_at": replay_input.evaluated_at.isoformat(),
        "market_features": [_features_to_dict(item) for item in replay_input.market_features],
        "requested_codes": list(replay_input.requested_codes),
        "candidate_features": [_features_to_dict(item) for item in replay_input.candidate_features],
        "reviews": {code: _review_to_dict(review) for code, review in replay_input.reviews.items()},
        "preselect_max_age_seconds": replay_input.preselect_max_age_seconds,
        "score_max_age_seconds": replay_input.score_max_age_seconds,
        "candidate_pool_size": replay_input.candidate_pool_size,
        "target_prices": dict(replay_input.target_prices),
    }


def _replay_input_from_dict(raw: Mapping[str, object]) -> RecommendationReplayInput:
    market_raw = _object_list(raw, "market_features")
    requested_raw = _string_list(raw, "requested_codes")
    candidates_raw = _object_list(raw, "candidate_features")
    reviews_raw = _object(raw, "reviews")
    target_prices_raw = _object(raw, "target_prices")
    return RecommendationReplayInput(
        schema_version=_text(raw, "schema_version"),
        algorithm_version=_text(raw, "algorithm_version"),
        policy=_replay_policy_from_dict(_object(raw, "policy")),
        evaluated_at=_aware_datetime(raw, "evaluated_at"),
        market_features=tuple(_features_from_dict(item) for item in market_raw),
        requested_codes=tuple(requested_raw),
        candidate_features=tuple(_features_from_dict(item) for item in candidates_raw),
        reviews=_review_mapping(reviews_raw),
        preselect_max_age_seconds=_number(raw, "preselect_max_age_seconds"),
        score_max_age_seconds=_number(raw, "score_max_age_seconds"),
        candidate_pool_size=_integer(raw, "candidate_pool_size"),
        target_prices={_mapping_key(code): _optional_number(value) for code, value in target_prices_raw.items()},
    )


def _replay_policy_to_dict(policy: FrozenReplayPolicy) -> dict[str, object]:
    return {
        "strategy_version": policy.strategy_version,
        "fusion_version": policy.fusion_version,
        "fusion": {
            "local_weight": policy.local_weight,
            "deepseek_weight": policy.deepseek_weight,
            "confidence_coverage_min": policy.confidence_coverage_min,
            "minimum_known_dimensions": policy.minimum_known_dimensions,
            "local_risk_cap": policy.local_risk_cap,
            "deepseek_risk_cap": policy.deepseek_risk_cap,
        },
        "selection": {
            "default_top_k": policy.default_top_k,
            "maximum_top_k": policy.maximum_top_k,
            "maximum_per_industry": policy.maximum_per_industry,
            "observation_margin": policy.observation_margin,
            "thresholds": dict(policy.thresholds),
        },
        "candidate_weights": dict(policy.candidate_weights),
        "dimension_weights": {name: dict(weights) for name, weights in policy.dimension_weights.items()},
        "risk_rules": {
            code: {
                "risk_code": rule.risk_code,
                "severity": rule.severity,
                "penalty": rule.penalty,
                "minimum_confidence": rule.minimum_confidence,
                "group": rule.group,
                "evidence_ttl_hours": rule.evidence_ttl_hours,
                "veto": rule.veto,
                "allowed_evidence_types": list(rule.allowed_evidence_types),
            }
            for code, rule in policy.risk_rules.items()
        },
    }


def _replay_policy_from_dict(raw: Mapping[str, object]) -> FrozenReplayPolicy:
    fusion = _object(raw, "fusion")
    selection = _object(raw, "selection")
    thresholds = _object(selection, "thresholds")
    candidate_weights = _object(raw, "candidate_weights")
    dimension_weights = _object(raw, "dimension_weights")
    risk_rules = _object(raw, "risk_rules")
    return FrozenReplayPolicy(
        strategy_version=_text(raw, "strategy_version"),
        fusion_version=_text(raw, "fusion_version"),
        local_weight=_number(fusion, "local_weight"),
        deepseek_weight=_number(fusion, "deepseek_weight"),
        confidence_coverage_min=_number(fusion, "confidence_coverage_min"),
        minimum_known_dimensions=_integer(fusion, "minimum_known_dimensions"),
        local_risk_cap=_number(fusion, "local_risk_cap"),
        deepseek_risk_cap=_number(fusion, "deepseek_risk_cap"),
        default_top_k=_integer(selection, "default_top_k"),
        maximum_top_k=_integer(selection, "maximum_top_k"),
        maximum_per_industry=_integer(selection, "maximum_per_industry"),
        observation_margin=_number(selection, "observation_margin"),
        thresholds=_number_mapping(thresholds),
        candidate_weights=_number_mapping(candidate_weights),
        dimension_weights=_nested_number_mapping(dimension_weights),
        risk_rules=_risk_rule_mapping(risk_rules),
    )


def _recommendation_to_dict(item: Recommendation) -> dict[str, object]:
    return {
        "strategy": item.strategy.value,
        "features": _features_to_dict(item.features),
        "score": _score_to_dict(item.score),
        "local_risk_facts": [_risk_fact_to_dict(fact) for fact in item.local_risk_facts],
        "deepseek_risk_facts": [_risk_fact_to_dict(fact) for fact in item.deepseek_risk_facts],
        "review": _review_to_dict(item.review) if item.review is not None else None,
        "action": item.action.value,
        "action_reason": item.action_reason,
        "veto": item.veto,
        "rank": item.rank,
        "target_price": item.target_price,
    }


def _recommendation_from_dict(raw: Mapping[str, object]) -> Recommendation:
    review_raw = raw.get("review")
    local_risks = raw.get("local_risk_facts")
    deepseek_risks = raw.get("deepseek_risk_facts")
    return Recommendation(
        strategy=Strategy(_text(raw, "strategy")),
        features=_features_from_dict(_object(raw, "features")),
        score=_score_from_dict(_object(raw, "score")),
        local_risk_facts=tuple(_risk_fact_from_dict(item) for item in local_risks if isinstance(item, dict))
        if isinstance(local_risks, list)
        else (),
        deepseek_risk_facts=tuple(_risk_fact_from_dict(item) for item in deepseek_risks if isinstance(item, dict))
        if isinstance(deepseek_risks, list)
        else (),
        review=_review_from_dict(review_raw) if isinstance(review_raw, dict) else None,
        action=RecommendationAction(_text(raw, "action")),
        action_reason=_text(raw, "action_reason"),
        veto=bool(raw.get("veto")),
        rank=_integer(raw, "rank"),
        target_price=_optional_number(raw.get("target_price")),
    )


def _features_to_dict(features: FeatureSnapshot) -> dict[str, object]:
    return {
        "quote": _quote_to_dict(features.quote),
        "values": dict(features.values),
        "observed_at": features.observed_at.isoformat(),
        "history_days": features.history_days,
        "market_regime": features.market_regime,
        "missing_fields": list(features.missing_fields),
        "evidence": [_evidence_to_dict(item) for item in features.evidence],
        "external_risk_facts": [_risk_fact_to_dict(item) for item in features.external_risk_facts],
    }


def _features_from_dict(raw: Mapping[str, object]) -> FeatureSnapshot:
    values = raw.get("values")
    evidence = raw.get("evidence")
    risks = raw.get("external_risk_facts")
    missing = raw.get("missing_fields")
    return FeatureSnapshot(
        quote=_quote_from_dict(_object(raw, "quote")),
        values={str(key): _optional_number(value) for key, value in values.items()} if isinstance(values, dict) else {},
        observed_at=datetime.fromisoformat(_text(raw, "observed_at")),
        history_days=_integer(raw, "history_days"),
        market_regime=_text(raw, "market_regime"),
        missing_fields=tuple(str(value) for value in missing if isinstance(value, str))
        if isinstance(missing, list)
        else (),
        evidence=tuple(_evidence_from_dict(item) for item in evidence if isinstance(item, dict))
        if isinstance(evidence, list)
        else (),
        external_risk_facts=tuple(_risk_fact_from_dict(item) for item in risks if isinstance(item, dict))
        if isinstance(risks, list)
        else (),
    )


def _quote_to_dict(quote: MarketQuote) -> dict[str, object]:
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
        "source_time": quote.source_time.isoformat(),
        "received_time": quote.received_time.isoformat(),
        "data_version": quote.data_version,
        "is_st": quote.is_st,
        "is_suspended": quote.is_suspended,
        "is_one_price_limit": quote.is_one_price_limit,
        "is_blacklisted": quote.is_blacklisted,
        "has_major_regulatory_risk": quote.has_major_regulatory_risk,
        "cross_source_deviation_pct": quote.cross_source_deviation_pct,
        "cross_source_verified": quote.cross_source_verified,
    }


def _quote_from_dict(raw: Mapping[str, object]) -> MarketQuote:
    return MarketQuote(
        code=_text(raw, "code"),
        name=_text(raw, "name"),
        price=_optional_number(raw.get("price")),
        previous_close=_optional_number(raw.get("previous_close")),
        open_price=_optional_number(raw.get("open_price")),
        high=_optional_number(raw.get("high")),
        low=_optional_number(raw.get("low")),
        pct_change=_optional_number(raw.get("pct_change")),
        change_5m=_optional_number(raw.get("change_5m")),
        speed=_optional_number(raw.get("speed")),
        volume_ratio=_optional_number(raw.get("volume_ratio")),
        turnover_rate=_optional_number(raw.get("turnover_rate")),
        amount=_optional_number(raw.get("amount")),
        amplitude=_optional_number(raw.get("amplitude")),
        market_cap=_optional_number(raw.get("market_cap")),
        industry=str(raw.get("industry") or ""),
        source=_text(raw, "source"),
        source_time=datetime.fromisoformat(_text(raw, "source_time")),
        received_time=datetime.fromisoformat(_text(raw, "received_time")),
        data_version=_text(raw, "data_version"),
        is_st=bool(raw.get("is_st")),
        is_suspended=bool(raw.get("is_suspended")),
        is_one_price_limit=bool(raw.get("is_one_price_limit")),
        is_blacklisted=bool(raw.get("is_blacklisted")),
        has_major_regulatory_risk=bool(raw.get("has_major_regulatory_risk")),
        cross_source_deviation_pct=_optional_number(raw.get("cross_source_deviation_pct")),
        cross_source_verified=bool(raw.get("cross_source_verified", True)),
    )


def _score_to_dict(score: ScoreBreakdown) -> dict[str, object]:
    return {
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
    }


def _score_from_dict(raw: Mapping[str, object]) -> ScoreBreakdown:
    components = raw.get("components")
    return ScoreBreakdown(
        components={str(key): float(value) for key, value in components.items()}
        if isinstance(components, dict)
        else {},
        base_score=_number(raw, "base_score"),
        local_risk_penalty=_number(raw, "local_risk_penalty"),
        local_score=_number(raw, "local_score"),
        deepseek_score=_optional_number(raw.get("deepseek_score")),
        confidence_coverage=_number(raw, "confidence_coverage"),
        deepseek_risk_penalty=_number(raw, "deepseek_risk_penalty"),
        final_score=_number(raw, "final_score"),
        fusion_mode=FusionMode(_text(raw, "fusion_mode")),
        fusion_applied=bool(raw.get("fusion_applied")),
    )


def _risk_fact_to_dict(fact: RiskFact) -> dict[str, object]:
    return {
        "risk_fact_id": fact.risk_fact_id,
        "risk_code": fact.risk_code,
        "severity": fact.severity,
        "penalty": fact.penalty,
        "source": fact.source,
        "observed_at": fact.observed_at.isoformat(),
        "confidence": fact.confidence,
        "evidence_ids": list(fact.evidence_ids),
        "group": fact.group,
        "veto": fact.veto,
    }


def _risk_fact_from_dict(raw: Mapping[str, object]) -> RiskFact:
    evidence_ids = raw.get("evidence_ids")
    return RiskFact(
        risk_fact_id=_text(raw, "risk_fact_id"),
        risk_code=_text(raw, "risk_code"),
        severity=_text(raw, "severity"),
        penalty=_number(raw, "penalty"),
        source=_text(raw, "source"),
        observed_at=datetime.fromisoformat(_text(raw, "observed_at")),
        confidence=_number(raw, "confidence"),
        evidence_ids=tuple(str(value) for value in evidence_ids if isinstance(value, str))
        if isinstance(evidence_ids, list)
        else (),
        group=str(raw.get("group") or ""),
        veto=bool(raw.get("veto")),
    )


def _review_to_dict(review: DeepSeekReview) -> dict[str, object]:
    return {
        "code": review.code,
        "outcome": review.outcome.value,
        "dimensions": {
            name: {
                "name": dimension.name,
                "score": dimension.score,
                "confidence": dimension.confidence,
                "assessment": dimension.assessment,
                "flags": list(dimension.flags),
                "evidence_ids": list(dimension.evidence_ids),
                "is_unknown": dimension.is_unknown,
            }
            for name, dimension in review.dimensions.items()
        },
        "risk_facts": [_risk_fact_to_dict(fact) for fact in review.risk_facts],
        "completed_at": review.completed_at.isoformat(),
        "error": review.error,
    }


def _review_from_dict(raw: Mapping[str, object]) -> DeepSeekReview:
    dimensions_raw = raw.get("dimensions")
    risks_raw = raw.get("risk_facts")
    dimensions: dict[str, DimensionAssessment] = {}
    if isinstance(dimensions_raw, dict):
        for name, item in dimensions_raw.items():
            if not isinstance(item, dict):
                continue
            flags = item.get("flags")
            evidence_ids = item.get("evidence_ids")
            dimensions[str(name)] = DimensionAssessment(
                name=_text(item, "name"),
                score=_number(item, "score"),
                confidence=_number(item, "confidence"),
                assessment=_text(item, "assessment"),
                flags=tuple(str(value) for value in flags if isinstance(value, str)) if isinstance(flags, list) else (),
                evidence_ids=tuple(str(value) for value in evidence_ids if isinstance(value, str))
                if isinstance(evidence_ids, list)
                else (),
                is_unknown=bool(item.get("is_unknown")),
            )
    return DeepSeekReview(
        code=_text(raw, "code"),
        outcome=ReviewOutcome(_text(raw, "outcome")),
        dimensions=dimensions,
        risk_facts=tuple(_risk_fact_from_dict(item) for item in risks_raw if isinstance(item, dict))
        if isinstance(risks_raw, list)
        else (),
        completed_at=datetime.fromisoformat(_text(raw, "completed_at")),
        error=str(raw.get("error") or ""),
    )


def _evidence_to_dict(evidence: Evidence) -> dict[str, object]:
    return {
        "evidence_id": evidence.evidence_id,
        "evidence_type": evidence.evidence_type,
        "title": evidence.title,
        "source": evidence.source,
        "published_at": evidence.published_at.isoformat(),
    }


def _evidence_from_dict(raw: Mapping[str, object]) -> Evidence:
    return Evidence(
        evidence_id=_text(raw, "evidence_id"),
        evidence_type=_text(raw, "evidence_type"),
        title=_text(raw, "title"),
        source=_text(raw, "source"),
        published_at=datetime.fromisoformat(_text(raw, "published_at")),
    )


def _object(raw: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value


def _object_list(raw: Mapping[str, object], key: str) -> tuple[Mapping[str, object], ...]:
    value = raw.get(key)
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"{key} must be a list of objects")
    return tuple(value)


def _string_list(raw: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = raw.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"{key} must be a list of non-empty strings")
    return tuple(value)


def _aware_datetime(raw: Mapping[str, object], key: str) -> datetime:
    value = datetime.fromisoformat(_text(raw, key))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{key} must be timezone-aware")
    return value


def _mapping_key(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("mapping keys must be non-empty strings")
    return value


def _number_mapping(raw: Mapping[str, object]) -> dict[str, float]:
    return {_mapping_key(key): _required_number(value) for key, value in raw.items()}


def _nested_number_mapping(raw: Mapping[str, object]) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            raise ValueError("nested number mappings must contain objects")
        result[_mapping_key(key)] = _number_mapping(value)
    return result


def _risk_rule_mapping(raw: Mapping[str, object]) -> dict[str, RiskRule]:
    result: dict[str, RiskRule] = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            raise ValueError("risk rule mappings must contain objects")
        code = _mapping_key(key)
        ttl = value.get("evidence_ttl_hours", 876_000)
        veto = value.get("veto", False)
        evidence_types = value.get("allowed_evidence_types", [])
        if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl < 1:
            raise ValueError("risk rule evidence_ttl_hours must be a positive integer")
        if not isinstance(veto, bool):
            raise ValueError("risk rule veto must be boolean")
        if not isinstance(evidence_types, list) or any(
            not isinstance(item, str) or not item for item in evidence_types
        ):
            raise ValueError("risk rule allowed_evidence_types must be a list of non-empty strings")
        result[code] = RiskRule(
            risk_code=_text(value, "risk_code"),
            severity=_text(value, "severity"),
            penalty=_number(value, "penalty"),
            minimum_confidence=_number(value, "minimum_confidence"),
            group=_text(value, "group"),
            evidence_ttl_hours=ttl,
            veto=veto,
            allowed_evidence_types=tuple(evidence_types),
        )
    return result


def _review_mapping(raw: Mapping[str, object]) -> dict[str, DeepSeekReview]:
    result: dict[str, DeepSeekReview] = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            raise ValueError("review mappings must contain objects")
        result[_mapping_key(key)] = _review_from_dict(value)
    return result


def _text(raw: Mapping[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _integer(raw: Mapping[str, object], key: str) -> int:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    return value


def _number(raw: Mapping[str, object], key: str) -> float:
    value = _optional_number(raw.get(key))
    if value is None:
        raise ValueError(f"{key} must be a number")
    return value


def _optional_number(value: object) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError("expected a number or null")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("numbers must be finite")
    return result


def _required_number(value: object) -> float:
    result = _optional_number(value)
    if result is None:
        raise ValueError("expected a number")
    return result


__all__ = [
    "SNAPSHOT_SCHEMA_VERSION",
    "snapshot_bytes",
    "snapshot_from_dict",
    "snapshot_sha256",
    "snapshot_to_dict",
]
