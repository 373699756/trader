"""Typed input boundary for the native tomorrow v2 pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from trader.application.cache import request_fingerprint
from trader.application.recommendation_policy_codec import preselection_replay_feature
from trader.domain.market.models import FeatureSnapshot

SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class TomorrowNativeInput:
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
        evaluated_at = _shanghai(self.evaluated_at)
        market_features = tuple(self.market_features)
        requested_codes = tuple(self.requested_codes)
        candidate_features = tuple(self.candidate_features)
        _validate_identity_and_limits(self, evaluated_at)
        _validate_code_sets(market_features, requested_codes, candidate_features)
        _validate_feature_times((*market_features, *candidate_features), evaluated_at)
        object.__setattr__(self, "evaluated_at", evaluated_at)
        object.__setattr__(self, "market_features", market_features)
        object.__setattr__(self, "requested_codes", requested_codes)
        object.__setattr__(self, "candidate_features", candidate_features)
        object.__setattr__(self, "input_version", _input_version(self))


class TomorrowNativeInputPort(Protocol):
    def offer_native(self, native_input: TomorrowNativeInput) -> bool: ...


def _validate_identity_and_limits(
    native_input: TomorrowNativeInput,
    evaluated_at: datetime,
) -> None:
    if native_input.trade_date != evaluated_at.date():
        raise ValueError("tomorrow native input trade date must match evaluation time")
    if not native_input.phase or not native_input.data_version or not native_input.config_version:
        raise ValueError("tomorrow native input identities must not be empty")
    if native_input.candidate_pool_size < 1:
        raise ValueError("tomorrow native candidate pool size must be positive")
    if native_input.preselect_max_age_seconds < 0 or native_input.score_max_age_seconds < 0:
        raise ValueError("tomorrow native quote age limits cannot be negative")


def _validate_code_sets(
    market_features: tuple[FeatureSnapshot, ...],
    requested_codes: tuple[str, ...],
    candidate_features: tuple[FeatureSnapshot, ...],
) -> None:
    if len(requested_codes) != len(set(requested_codes)):
        raise ValueError("tomorrow native requested codes must be unique")
    market_codes = _unique_codes(market_features, "market")
    candidate_codes = _unique_codes(candidate_features, "candidate")
    if not market_codes:
        raise ValueError("tomorrow native input requires market features")
    if not candidate_codes.issubset(market_codes):
        raise ValueError("tomorrow native candidates must belong to the market input")
    if not candidate_codes.issubset(set(requested_codes)):
        raise ValueError("tomorrow native candidates must belong to requested codes")


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
            raise ValueError("tomorrow native input cannot contain future features")


def _unique_codes(features: tuple[FeatureSnapshot, ...], label: str) -> set[str]:
    codes = tuple(feature.quote.code for feature in features)
    if len(codes) != len(set(codes)):
        raise ValueError(f"tomorrow native {label} feature codes must be unique")
    return set(codes)


def _input_version(native_input: TomorrowNativeInput) -> str:
    material = {
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
        quote.source_time,
        quote.received_time,
        feature.observed_at,
        feature.merge_epoch or request_fingerprint({"feature": feature}),
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
        raise ValueError("tomorrow native input time must be timezone-aware")
    return value.astimezone(SHANGHAI)


__all__ = ["TomorrowNativeInput", "TomorrowNativeInputPort"]
