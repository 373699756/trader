"""Frozen replay input and policy snapshot codecs."""

from __future__ import annotations

from collections.abc import Mapping

from trader.domain.models import (
    Board,
    BoardScoreBatch,
    FrozenReplayPolicy,
    RecommendationReplayInput,
    Strategy,
)
from trader.domain.strategies import DEFAULT_STRATEGY_WEIGHTS
from trader.infra.persistence.snapshot_items import (
    _features_from_dict,
    _features_to_dict,
    _recommendation_from_dict,
    _recommendation_to_dict,
    _review_to_dict,
)
from trader.infra.persistence.snapshot_primitives import (
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
        "board_batches": [_board_batch_to_dict(batch) for batch in replay_input.board_batches],
    }


def _replay_input_from_dict(raw: Mapping[str, object]) -> RecommendationReplayInput:
    market_raw = _object_list(raw, "market_features")
    requested_raw = _string_list(raw, "requested_codes")
    candidates_raw = _object_list(raw, "candidate_features")
    reviews_raw = _object(raw, "reviews")
    target_prices_raw = _object(raw, "target_prices")
    board_batches_raw = raw.get("board_batches", [])
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
        board_batches=tuple(_board_batch_from_dict(item) for item in board_batches_raw if isinstance(item, dict))
        if isinstance(board_batches_raw, list)
        else (),
    )


def _board_batch_to_dict(batch: BoardScoreBatch) -> dict[str, object]:
    return {
        "board": batch.board.value,
        "strategy": batch.strategy.value,
        "merge_epoch": batch.merge_epoch,
        "policy_id": batch.policy_id,
        "status": batch.status,
        "recommendations": [_recommendation_to_dict(item) for item in batch.recommendations],
        "degraded_reasons": list(batch.degraded_reasons),
        "policy_version": batch.policy_version,
        "population_version": batch.population_version,
    }


def _board_batch_from_dict(raw: Mapping[str, object]) -> BoardScoreBatch:
    recommendations = raw.get("recommendations")
    reasons = raw.get("degraded_reasons")
    return BoardScoreBatch(
        board=Board(_text(raw, "board")),
        strategy=Strategy(_text(raw, "strategy")),
        merge_epoch=_text(raw, "merge_epoch"),
        policy_id=_text(raw, "policy_id"),
        status=str(raw.get("status") or "success"),  # type: ignore[arg-type]
        recommendations=tuple(_recommendation_from_dict(item) for item in recommendations if isinstance(item, dict))
        if isinstance(recommendations, list)
        else (),
        degraded_reasons=tuple(str(item) for item in reasons if isinstance(item, str))
        if isinstance(reasons, list)
        else (),
        policy_version=str(raw.get("policy_version") or ""),
        population_version=str(raw.get("population_version") or ""),
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
            "maximum_board_fraction": policy.maximum_board_fraction,
            "competition_group_limits": dict(policy.competition_group_limits),
            "candidate_min_score": policy.candidate_min_score,
            "minimum_board_reliability": policy.minimum_board_reliability,
        },
        "board_policy_version": policy.board_policy_version,
        "board_candidate_weights": {
            strategy: {board: dict(weights) for board, weights in boards.items()}
            for strategy, boards in policy.board_candidate_weights.items()
        },
        "board_local_strategy_weights": {
            strategy: {board: dict(weights) for board, weights in boards.items()}
            for strategy, boards in policy.board_local_strategy_weights.items()
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
    competition_raw = selection.get("competition_group_limits", {})
    if not isinstance(competition_raw, dict) or any(
        not isinstance(name, str) or not isinstance(value, int) or isinstance(value, bool)
        for name, value in competition_raw.items()
    ):
        raise ValueError("replay competition_group_limits must be an object of integers")
    board_candidate_raw = raw.get("board_candidate_weights", {})
    board_local_raw = raw.get("board_local_strategy_weights", {})
    if not isinstance(board_candidate_raw, dict) or not isinstance(board_local_raw, dict):
        raise ValueError("replay board policy weights must be objects")
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
        maximum_board_fraction=_optional_number(selection.get("maximum_board_fraction")) or 1.0,
        competition_group_limits={str(name): int(value) for name, value in competition_raw.items()},
        candidate_min_score=_optional_number(selection.get("candidate_min_score")) or 0.0,
        minimum_board_reliability=_optional_number(selection.get("minimum_board_reliability")) or 0.0,
        board_policy_version=str(raw.get("board_policy_version") or ""),
        board_candidate_weights=_triple_number_mapping(board_candidate_raw),
        board_local_strategy_weights=_triple_number_mapping(board_local_raw),
    )


def _triple_number_mapping(raw: Mapping[str, object]) -> dict[str, dict[str, dict[str, float]]]:
    result: dict[str, dict[str, dict[str, float]]] = {}
    for strategy, boards in raw.items():
        if not isinstance(boards, dict):
            raise ValueError("replay board policy mappings must contain board objects")
        result[str(strategy)] = {}
        for board, weights in boards.items():
            if not isinstance(weights, dict):
                raise ValueError("replay board policy mappings must contain weight objects")
            result[str(strategy)][str(board)] = _number_mapping(weights)
    return result
