"""Strict JSON codecs for explicit research registrations; no legacy decoder."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from typing import Literal, cast

from trader.infra.settings.parser import Integer, Number, RequireExactKeys, RequireMapping, Text
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.training.domain.evaluation.research_registration import (
    CandidateResearchRegistration, ResearchModelComponent, ResearchParameter,
    ResearchStateDay, ResearchStateRegistration, ResearchStateRule,
)


def _objects(raw: Mapping[str, object], name: str, maximum: int) -> tuple[Mapping[str, object], ...]:
    values = raw.get(name)
    if not isinstance(values, list) or len(values) > maximum:
        raise ValueError("research_registration_array_invalid")
    result = []
    for value in values:
        if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
            raise ValueError("research_registration_object_invalid")
        result.append(value)
    return tuple(result)


def _components(raw: Mapping[str, object], name: str) -> tuple[ResearchModelComponent, ...]:
    result = []
    for item in _objects(raw, name, 2):
        RequireExactKeys(item, {"role", "family", "model_hash", "training_hash", "feature_hash", "sequence_length", "epochs"}, "research_model")
        result.append(ResearchModelComponent(
            cast(Literal["ranker", "utility"], Text(item, "role")),
            cast(Literal["lambdarank", "lightgbm", "ridge", "tcn"], Text(item, "family")),
            Text(item, "model_hash"), Text(item, "training_hash"), Text(item, "feature_hash"),
            Integer(item, "sequence_length", minimum=0), Integer(item, "epochs", minimum=0),
        ))
    return tuple(result)


def DecodeCandidateRegistration(raw: Mapping[str, object]) -> CandidateResearchRegistration:
    RequireExactKeys(raw, {
        "profile_id", "strategy", "candidate_id", "registered_candidate_ids", "changed_factor", "parameters",
        "stopping_rule", "registered_at", "registration_receipt", "qualification_hash", "population_hash",
        "fixed_control_hash", "candidate_components", "control_components",
    }, "research_registration")
    names = raw["registered_candidate_ids"]
    if not isinstance(names, list) or len(names) > 3 or any(not isinstance(value, str) for value in names):
        raise ValueError("research_candidate_family_invalid")
    parameters = []
    for item in _objects(raw, "parameters", 32):
        RequireExactKeys(item, {"name", "value", "unit"}, "research_parameter")
        parameters.append(ResearchParameter(Text(item, "name"), Number(item, "value"), Text(item, "unit")))
    return CandidateResearchRegistration(
        cast(ScoringProfileId, Text(raw, "profile_id")), cast(Literal["tomorrow", "d25"], Text(raw, "strategy")),
        Text(raw, "candidate_id"), tuple(cast(str, value) for value in names), Text(raw, "changed_factor"),
        tuple(parameters), Text(raw, "stopping_rule"), datetime.fromisoformat(Text(raw, "registered_at")),
        Text(raw, "registration_receipt"), Text(raw, "qualification_hash"), Text(raw, "population_hash"),
        Text(raw, "fixed_control_hash"), _components(raw, "candidate_components"), _components(raw, "control_components"),
    )


def _rule(raw: Mapping[str, object]) -> ResearchStateRule:
    RequireExactKeys(raw, {"indicator", "unit", "window_sessions", "thresholds", "labels", "boundary"}, "research_state_rule")
    thresholds, labels = raw["thresholds"], raw["labels"]
    if (
        not isinstance(thresholds, list) or len(thresholds) > 2
        or not isinstance(labels, list) or len(labels) > 3 or any(not isinstance(value, str) for value in labels)
    ):
        raise ValueError("research_state_rule_arrays_invalid")
    return ResearchStateRule(
        Text(raw, "indicator"), Integer(raw, "window_sessions", minimum=1),
        tuple(Number({"value": value}, "value") for value in thresholds),
        tuple(cast(str, value) for value in labels),
        cast(Literal["upper_inclusive", "lower_inclusive"], Text(raw, "boundary")),
        unit=Text(raw, "unit"),
    )


def DecodeStateRegistration(raw: Mapping[str, object]) -> ResearchStateRegistration:
    RequireExactKeys(raw, {
        "granularity", "generator", "implementation_hash", "qualification_hash", "registered_at",
        "market", "volatility", "liquidity", "days",
    }, "research_state_registration")
    days = []
    for item in _objects(raw, "days", 20_000):
        RequireExactKeys(item, {
            "trade_date", "anchor", "source_visible_at", "source_record_id", "source_receipt",
            "market_value", "volatility_value", "liquidity_value",
        }, "research_state_day")
        days.append(ResearchStateDay(
            date.fromisoformat(Text(item, "trade_date")), datetime.fromisoformat(Text(item, "anchor")),
            datetime.fromisoformat(Text(item, "source_visible_at")), Text(item, "source_record_id"),
            Text(item, "source_receipt"), Number(item, "market_value"), Number(item, "volatility_value"),
            Number(item, "liquidity_value"),
        ))
    return ResearchStateRegistration(
        cast(Literal["strategy_trading_day"], Text(raw, "granularity")), Text(raw, "generator"),
        Text(raw, "implementation_hash"), Text(raw, "qualification_hash"),
        datetime.fromisoformat(Text(raw, "registered_at")), _rule(RequireMapping(raw, "market")),
        _rule(RequireMapping(raw, "volatility")), _rule(RequireMapping(raw, "liquidity")), tuple(days),
    )


__all__ = ["DecodeCandidateRegistration", "DecodeStateRegistration"]
