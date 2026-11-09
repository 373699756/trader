"""Explicit, immutable registrations for profile experiments and daily states."""

from __future__ import annotations

import math
import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal

from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.recommendation.domain.scoring.profile_identity import SCORING_PROFILE_IDS, ScoringProfileId

_HASH = re.compile(r"[0-9a-f]{64}")
_NAME = re.compile(r"[a-z][a-z0-9_]{0,95}")
ResearchStrategy = Literal["tomorrow", "d25"]


class ResearchNotReadyError(ValueError):
    """Required preregistration or qualified evidence is absent before a read."""


@dataclass(frozen=True)
class ResearchModelComponent:
    role: Literal["ranker", "utility"]
    family: Literal["lambdarank", "lightgbm", "ridge", "tcn"]
    model_hash: str
    training_hash: str
    feature_hash: str
    sequence_length: int
    epochs: int

    def __post_init__(self) -> None:
        if self.role not in {"ranker", "utility"} or self.family not in {"lambdarank", "lightgbm", "ridge", "tcn"}:
            raise ValueError("research_model_component_invalid")
        if any(_HASH.fullmatch(value) is None for value in (self.model_hash, self.training_hash, self.feature_hash)):
            raise ValueError("research_model_identity_invalid")
        if type(self.sequence_length) is not int or type(self.epochs) is not int:
            raise ValueError("research_model_sequence_invalid")
        expected = (60, 10) if self.family == "tcn" else (0, 0)
        if (self.sequence_length, self.epochs) != expected:
            raise ValueError("research_model_sequence_invalid")


def ValidateResearchComponents(profile_id: ScoringProfileId, components: tuple[ResearchModelComponent, ...]) -> None:
    expected = {
        "v1": (("ranker", "lambdarank"), ("utility", "ridge")),
        "v2": (("utility", "lightgbm"),),
        "v3": (("utility", "ridge"),),
        "v5": (("utility", "tcn"),),
    }
    if profile_id not in SCORING_PROFILE_IDS or tuple((item.role, item.family) for item in components) != expected[profile_id]:
        raise ValueError("research_profile_components_mismatch")


@dataclass(frozen=True)
class ResearchParameter:
    name: str
    value: float
    unit: str

    def __post_init__(self) -> None:
        if (
            _NAME.fullmatch(self.name) is None or _NAME.fullmatch(self.unit) is None
            or isinstance(self.value, bool) or not math.isfinite(self.value)
        ):
            raise ValueError("research_parameter_invalid")
        object.__setattr__(self, "value", float(self.value))


@dataclass(frozen=True)
class CandidateResearchRegistration:
    profile_id: ScoringProfileId
    strategy: ResearchStrategy
    candidate_id: str
    registered_candidate_ids: tuple[str, ...]
    changed_factor: str
    parameters: tuple[ResearchParameter, ...]
    stopping_rule: str
    registered_at: datetime
    registration_receipt: str
    qualification_hash: str
    population_hash: str
    fixed_control_hash: str
    candidate_components: tuple[ResearchModelComponent, ...]
    control_components: tuple[ResearchModelComponent, ...]

    def __post_init__(self) -> None:
        if self.profile_id not in SCORING_PROFILE_IDS or self.strategy not in {"tomorrow", "d25"}:
            raise ValueError("research_registration_identity_invalid")
        names = (*self.registered_candidate_ids, self.candidate_id, self.changed_factor, self.stopping_rule)
        if any(_NAME.fullmatch(value) is None for value in names):
            raise ValueError("research_registration_name_invalid")
        if (
            not 1 <= len(self.registered_candidate_ids) <= 3
            or len(set(self.registered_candidate_ids)) != len(self.registered_candidate_ids)
            or self.candidate_id not in self.registered_candidate_ids
            or not self.parameters or len(self.parameters) > 32
            or len({item.name for item in self.parameters}) != len(self.parameters)
            or self.registered_at.tzinfo is None or self.registered_at.utcoffset() is None
        ):
            raise ValueError("research_registration_budget_or_time_invalid")
        hashes = (self.registration_receipt, self.qualification_hash, self.population_hash, self.fixed_control_hash)
        if any(_HASH.fullmatch(value) is None for value in hashes):
            raise ValueError("research_registration_receipt_invalid")
        ValidateResearchComponents(self.profile_id, self.candidate_components)
        ValidateResearchComponents(self.profile_id, self.control_components)

    @property
    def content_hash(self) -> str:
        return CanonicalDigest(self)

    def validate_before_read(self, at: datetime) -> None:
        if at.tzinfo is None or at.utcoffset() is None or self.registered_at > at:
            raise ValueError("research_registration_not_sealed_before_read")


@dataclass(frozen=True)
class ResearchStateRule:
    indicator: str
    window_sessions: int
    thresholds: tuple[float, ...]
    labels: tuple[str, ...]
    boundary: Literal["upper_inclusive", "lower_inclusive"]
    unit: str = field(kw_only=True)

    def __post_init__(self) -> None:
        if (
            _NAME.fullmatch(self.indicator) is None
            or _NAME.fullmatch(self.unit) is None
            or type(self.window_sessions) is not int or not 1 <= self.window_sessions <= 20_000
            or self.boundary not in {"upper_inclusive", "lower_inclusive"}
            or len(self.labels) != len(self.thresholds) + 1
            or len(set(self.labels)) != len(self.labels)
            or any(isinstance(value, bool) or not math.isfinite(value) for value in self.thresholds)
            or self.thresholds != tuple(sorted(set(self.thresholds)))
        ):
            raise ValueError("research_state_rule_invalid")
        object.__setattr__(self, "thresholds", tuple(float(value) for value in self.thresholds))

    def classify(self, value: float) -> str:
        if isinstance(value, bool) or not math.isfinite(value):
            raise ValueError("research_state_observation_invalid")
        index = bisect_left(self.thresholds, value) if self.boundary == "upper_inclusive" else bisect_right(self.thresholds, value)
        return self.labels[index]


@dataclass(frozen=True)
class ResearchStateDay:
    trade_date: date
    anchor: datetime
    source_visible_at: datetime
    source_record_id: str
    source_receipt: str
    market_value: float
    volatility_value: float
    liquidity_value: float

    def __post_init__(self) -> None:
        if (
            self.anchor.tzinfo is None or self.anchor.utcoffset() is None
            or self.anchor.utcoffset() != timedelta(hours=8)
            or (self.anchor.hour, self.anchor.minute, self.anchor.second, self.anchor.microsecond) != (15, 0, 0, 0)
            or self.source_visible_at.tzinfo is None or self.source_visible_at.utcoffset() is None
            or self.anchor.date() != self.trade_date or self.source_visible_at > self.anchor
            or _NAME.fullmatch(self.source_record_id) is None or _HASH.fullmatch(self.source_receipt) is None
            or any(not math.isfinite(value) for value in (self.market_value, self.volatility_value, self.liquidity_value))
        ):
            raise ValueError("research_state_point_in_time_evidence_invalid")
        object.__setattr__(self, "market_value", float(self.market_value))
        object.__setattr__(self, "volatility_value", float(self.volatility_value))
        object.__setattr__(self, "liquidity_value", float(self.liquidity_value))


@dataclass(frozen=True)
class ResearchStateRegistration:
    granularity: Literal["strategy_trading_day"]
    generator: str
    implementation_hash: str
    qualification_hash: str
    registered_at: datetime
    market: ResearchStateRule
    volatility: ResearchStateRule
    liquidity: ResearchStateRule
    days: tuple[ResearchStateDay, ...]

    def __post_init__(self) -> None:
        dates = tuple(item.trade_date for item in self.days)
        if (
            self.granularity != "strategy_trading_day" or _NAME.fullmatch(self.generator) is None
            or _HASH.fullmatch(self.implementation_hash) is None or _HASH.fullmatch(self.qualification_hash) is None
            or self.registered_at.tzinfo is None or self.registered_at.utcoffset() is None
            or self.market.labels != ("down", "sideways", "up")
            or self.volatility.labels != ("low", "high") or self.liquidity.labels != ("low", "high")
            or not dates or len(dates) > 20_000 or dates != tuple(sorted(set(dates)))
        ):
            raise ValueError("research_state_registration_invalid")

    @property
    def content_hash(self) -> str:
        return CanonicalDigest(self)

    def states(self, day: ResearchStateDay) -> tuple[str, str, str]:
        return (
            self.market.classify(day.market_value), self.volatility.classify(day.volatility_value),
            self.liquidity.classify(day.liquidity_value),
        )


__all__ = [
    "CandidateResearchRegistration", "ResearchModelComponent", "ResearchNotReadyError", "ResearchParameter",
    "ResearchStateDay", "ResearchStateRegistration", "ResearchStateRule", "ResearchStrategy", "ValidateResearchComponents",
]
