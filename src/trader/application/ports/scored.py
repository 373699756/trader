"""Typed native-input and control boundaries for scored V2 strategies."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import ClassVar, Protocol
from zoneinfo import ZoneInfo

from trader.application.cache import request_fingerprint
from trader.application.recommendation_policy_codec import preselection_replay_feature
from trader.domain.market.models import FeatureSnapshot, MarketQuote
from trader.domain.recommendation.models import Strategy

SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class ScoredNativeInput:
    strategy: ClassVar[Strategy]
    trade_date: date
    phase: str
    data_version: str
    config_version: str
    evaluated_at: datetime
    market_features: tuple[FeatureSnapshot, ...]
    requested_codes: tuple[str, ...]
    candidate_features: tuple[FeatureSnapshot, ...]
    preselect_max_age_seconds: float
    score_max_age_seconds: float
    candidate_pool_size: int
    input_version: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self) is ScoredNativeInput:
            raise TypeError("scored native input requires a concrete strategy")
        evaluated_at = _shanghai(self.evaluated_at)
        market_features = tuple(self.market_features)
        requested_codes = tuple(self.requested_codes)
        candidate_features = tuple(_normalize_feature_times(feature) for feature in self.candidate_features)
        _validate_identity_and_limits(self, evaluated_at)
        _validate_code_sets(market_features, requested_codes, candidate_features)
        _validate_feature_times((*market_features, *candidate_features), evaluated_at)
        object.__setattr__(self, "evaluated_at", evaluated_at)
        object.__setattr__(self, "market_features", market_features)
        object.__setattr__(self, "requested_codes", requested_codes)
        object.__setattr__(self, "candidate_features", candidate_features)
        object.__setattr__(self, "input_version", _input_version(self))


class TodayNativeInput(ScoredNativeInput):
    strategy = Strategy.TODAY


class TomorrowNativeInput(ScoredNativeInput):
    strategy = Strategy.TOMORROW


class D25NativeInput(ScoredNativeInput):
    strategy = Strategy.D25


class TomorrowNativeInputPort(Protocol):
    def offer_native(self, native_input: TomorrowNativeInput) -> bool: ...


class D25NativeInputPort(Protocol):
    def offer_native(self, native_input: D25NativeInput) -> bool: ...


class TodayNativeInputPort(Protocol):
    def offer_native(self, native_input: TodayNativeInput) -> bool: ...


class V2ControlPort(Protocol):
    def on_clock(self, at: datetime) -> object | None: ...


class V2OverlayPort(Protocol):
    def overlay_codes(self, trade_date: date) -> tuple[str, ...]: ...

    def publish_overlay(
        self,
        quotes: Mapping[str, MarketQuote],
        *,
        observed_at: datetime,
        closing: bool,
    ) -> bool: ...


def _validate_identity_and_limits(
    native_input: ScoredNativeInput,
    evaluated_at: datetime,
) -> None:
    if native_input.trade_date != evaluated_at.date():
        raise ValueError("scored native input trade date must match evaluation time")
    if not native_input.phase or not native_input.data_version or not native_input.config_version:
        raise ValueError("scored native input identities must not be empty")
    if native_input.candidate_pool_size < 1:
        raise ValueError("scored native candidate pool size must be positive")
    if native_input.preselect_max_age_seconds < 0 or native_input.score_max_age_seconds < 0:
        raise ValueError("scored native quote age limits cannot be negative")


def _validate_code_sets(
    market_features: tuple[FeatureSnapshot, ...],
    requested_codes: tuple[str, ...],
    candidate_features: tuple[FeatureSnapshot, ...],
) -> None:
    if len(requested_codes) != len(set(requested_codes)):
        raise ValueError("scored native requested codes must be unique")
    market_codes = _unique_codes(market_features, "market")
    candidate_codes = _unique_codes(candidate_features, "candidate")
    if not market_codes:
        raise ValueError("scored native input requires market features")
    if not candidate_codes.issubset(market_codes):
        raise ValueError("scored native candidates must belong to the market input")
    if not candidate_codes.issubset(set(requested_codes)):
        raise ValueError("scored native candidates must belong to requested codes")


def _validate_feature_times(
    features: tuple[FeatureSnapshot, ...],
    evaluated_at: datetime,
) -> None:
    for feature in features:
        if (
            _shanghai(feature.observed_at) > evaluated_at
            or _shanghai(feature.quote.source_time) > evaluated_at
            or _shanghai(feature.quote.received_time) > evaluated_at
        ):
            raise ValueError("scored native input cannot contain future features")
        if any(
            _shanghai(evidence.published_at) > evaluated_at
            or (evidence.received_at is not None and _shanghai(evidence.received_at) > evaluated_at)
            for evidence in feature.evidence
        ):
            raise ValueError("scored native input cannot contain future evidence")
        if any(_shanghai(fact.observed_at) > evaluated_at for fact in feature.external_risk_facts):
            raise ValueError("scored native input cannot contain future risk facts")


def _normalize_feature_times(feature: FeatureSnapshot) -> FeatureSnapshot:
    quote = replace(
        feature.quote,
        source_time=_shanghai(feature.quote.source_time),
        received_time=_shanghai(feature.quote.received_time),
    )
    evidence = tuple(
        replace(
            item,
            published_at=_shanghai(item.published_at),
            received_at=_shanghai(item.received_at) if item.received_at is not None else None,
        )
        for item in feature.evidence
    )
    risk_facts = tuple(replace(fact, observed_at=_shanghai(fact.observed_at)) for fact in feature.external_risk_facts)
    return replace(
        feature,
        quote=quote,
        observed_at=_shanghai(feature.observed_at),
        evidence=evidence,
        external_risk_facts=risk_facts,
    )


def _unique_codes(features: tuple[FeatureSnapshot, ...], label: str) -> set[str]:
    codes = tuple(feature.quote.code for feature in features)
    if len(codes) != len(set(codes)):
        raise ValueError(f"scored native {label} feature codes must be unique")
    return set(codes)


def _input_version(native_input: ScoredNativeInput) -> str:
    material = {
        "strategy": native_input.strategy,
        "trade_date": native_input.trade_date,
        "phase": native_input.phase,
        "data_version": native_input.data_version,
        "config_version": native_input.config_version,
        "evaluated_at": native_input.evaluated_at,
        "requested_codes": tuple(sorted(native_input.requested_codes)),
        "market": tuple(
            _market_feature_identity(feature)
            for feature in sorted(native_input.market_features, key=lambda item: item.quote.code)
        ),
        "candidates": tuple(
            _feature_identity(feature)
            for feature in sorted(native_input.candidate_features, key=lambda item: item.quote.code)
        ),
        "preselect_max_age_seconds": native_input.preselect_max_age_seconds,
        "score_max_age_seconds": native_input.score_max_age_seconds,
        "candidate_pool_size": native_input.candidate_pool_size,
    }
    return f"native-input:{request_fingerprint(material)[:24]}"


def _feature_identity(feature: FeatureSnapshot) -> tuple[object, ...]:
    quote = feature.quote
    return (
        quote.code,
        quote.data_version,
        _shanghai(quote.source_time),
        _shanghai(quote.received_time),
        _shanghai(feature.observed_at),
        feature.merge_epoch or request_fingerprint({"feature": _normalize_feature_times(feature)}),
        feature.history_days,
        tuple(sorted(feature.missing_fields)),
        tuple(sorted(feature.missing_reasons.items())),
    )


def _market_feature_identity(feature: FeatureSnapshot) -> tuple[object, ...]:
    if feature.merge_epoch:
        return _feature_identity(feature)
    return _feature_identity(preselection_replay_feature(feature))


def _shanghai(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("scored native input time must be timezone-aware")
    return value.astimezone(SHANGHAI)


__all__ = [
    "ScoredNativeInput",
    "TodayNativeInput",
    "TodayNativeInputPort",
    "TomorrowNativeInput",
    "TomorrowNativeInputPort",
    "D25NativeInput",
    "D25NativeInputPort",
    "V2ControlPort",
    "V2OverlayPort",
]
