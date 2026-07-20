"""Frozen replay input and policy snapshot codecs."""

from __future__ import annotations

from collections.abc import Mapping

from trader.domain.models import (
    FrozenReplayPolicy,
    RecommendationReplayInput,
)
from trader.domain.strategies import DEFAULT_STRATEGY_WEIGHTS
from trader.infrastructure.persistence.snapshot_items import (
    _features_from_dict,
    _features_to_dict,
    _review_to_dict,
)
from trader.infrastructure.persistence.snapshot_primitives import (
    _aware_datetime,
    _integer,
    _mapping_key,
    _nested_number_mapping,
    _number,
    _number_mapping,
    _object,
    _object_list,
    _optional_number,
    _review_mapping,
    _risk_rule_mapping,
    _string_list,
    _text,
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
        "local_strategy_weights": {name: dict(weights) for name, weights in policy.local_strategy_weights.items()},
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
                "strategies": list(rule.strategies),
                "trigger_factor": rule.trigger_factor,
                "trigger_operator": rule.trigger_operator,
                "trigger_thresholds": list(rule.trigger_thresholds),
                "combination_mode": rule.combination_mode,
                "risk_fact_id_fields": list(rule.risk_fact_id_fields),
                "local_trigger_enabled": rule.local_trigger_enabled,
            }
            for code, rule in policy.risk_rules.items()
        },
        "hard_filters": {
            "blacklist_codes": list(policy.blacklist_codes),
            "structured_risk_thresholds": dict(policy.structured_risk_thresholds),
        },
    }


def _replay_policy_from_dict(raw: Mapping[str, object]) -> FrozenReplayPolicy:
    fusion = _object(raw, "fusion")
    selection = _object(raw, "selection")
    thresholds = _object(selection, "thresholds")
    candidate_weights = _object(raw, "candidate_weights")
    dimension_weights = _object(raw, "dimension_weights")
    local_strategy_weights = (
        _object(raw, "local_strategy_weights")
        if "local_strategy_weights" in raw
        else {strategy.value: dict(weights) for strategy, weights in DEFAULT_STRATEGY_WEIGHTS.items()}
    )
    risk_rules = _object(raw, "risk_rules")
    hard_filters = raw.get("hard_filters")
    hard_filter_values = hard_filters if isinstance(hard_filters, dict) else {}
    blacklist_codes = hard_filter_values.get("blacklist_codes", [])
    structured_risk_thresholds = hard_filter_values.get("structured_risk_thresholds", {})
    if not isinstance(blacklist_codes, list) or any(not isinstance(code, str) for code in blacklist_codes):
        raise ValueError("replay blacklist_codes must be a list of strings")
    if not isinstance(structured_risk_thresholds, dict):
        raise ValueError("replay structured_risk_thresholds must be an object")
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
        local_strategy_weights=_nested_number_mapping(local_strategy_weights),
        risk_rules=_risk_rule_mapping(risk_rules),
        blacklist_codes=tuple(blacklist_codes),
        structured_risk_thresholds=_number_mapping(structured_risk_thresholds),
    )
