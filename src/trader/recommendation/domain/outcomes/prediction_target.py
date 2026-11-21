"""Configurable closing-price events shared by model fitting and settlement."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction

from trader.recommendation.domain.outcomes.tomorrow_target import (
    TOMORROW_CLOSE_GAIN_TARGET,
    EvaluateTomorrowCloseGain,
    TomorrowCloseGain,
)
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import SCORING_PROFILE_IDS, ScoringProfileId

_TOMORROW_PREFIX = "next_close_gain_at_least_"
_D25_PREFIX = "mean_close_gain_d2_d5_at_least_"
_THRESHOLD = re.compile(r"(0|[1-9][0-9]*)_bps\Z")


@dataclass(frozen=True)
class PredictionTarget:
    strategy: Strategy
    threshold_bps: int

    def __post_init__(self) -> None:
        if self.strategy is not Strategy.TOMORROW and self.strategy is not Strategy.D25:
            raise ValueError("prediction target requires Tomorrow or D25")
        if type(self.threshold_bps) is not int or self.threshold_bps < 0:
            raise ValueError("prediction target requires nonnegative integer basis points")

    @property
    def identity(self) -> str:
        prefix = _TOMORROW_PREFIX if self.strategy is Strategy.TOMORROW else _D25_PREFIX
        return f"{prefix}{self.threshold_bps}_bps"


@dataclass(frozen=True)
class ProfilePredictionTargets:
    tomorrow: PredictionTarget
    d25: PredictionTarget

    def __post_init__(self) -> None:
        if self.tomorrow.strategy is not Strategy.TOMORROW or self.d25.strategy is not Strategy.D25:
            raise ValueError("profile must provide one target for each scored strategy")


@dataclass(frozen=True)
class PredictionTargets:
    v1: ProfilePredictionTargets
    v2: ProfilePredictionTargets
    v3: ProfilePredictionTargets
    v5: ProfilePredictionTargets

    def for_profile(self, profile: ScoringProfileId, strategy: Strategy) -> PredictionTarget:
        targets = (self.v1, self.v2, self.v3, self.v5)[SCORING_PROFILE_IDS.index(profile)]
        if strategy is Strategy.TOMORROW:
            return targets.tomorrow
        if strategy is Strategy.D25:
            return targets.d25
        raise ValueError("Long has no prediction target")

    def tomorrow_groups(
        self, profiles: tuple[ScoringProfileId, ...]
    ) -> tuple[tuple[PredictionTarget, tuple[ScoringProfileId, ...]], ...]:
        targets = tuple(self.for_profile(profile, Strategy.TOMORROW) for profile in profiles)
        return tuple(
            (target, tuple(profile for profile, value in zip(profiles, targets, strict=True) if value == target))
            for index, target in enumerate(targets)
            if target not in targets[:index]
        )


def ParsePredictionTargetIdentity(identity: str | None) -> PredictionTarget | None:
    if identity is None:
        return None
    if identity == TOMORROW_CLOSE_GAIN_TARGET:
        return PredictionTarget(Strategy.TOMORROW, 500)
    for strategy, prefix in ((Strategy.TOMORROW, _TOMORROW_PREFIX), (Strategy.D25, _D25_PREFIX)):
        if identity.startswith(prefix) and (match := _THRESHOLD.fullmatch(identity[len(prefix):])):
            return PredictionTarget(strategy, int(match[1]))
    return None


def EvaluatePredictionTarget(
    target: PredictionTarget,
    signal_close: Decimal | None,
    future_closes: tuple[Decimal | None, ...],
    *,
    confirmed_suspended: bool = False,
    next_raw_reference: Decimal | None = None,
    next_raw_close: Decimal | None = None,
) -> TomorrowCloseGain:
    """Judge an event using already-qualified closes on a common price basis."""
    if target.strategy is Strategy.TOMORROW:
        if len(future_closes) != 1:
            return TomorrowCloseGain.PENDING
        return EvaluateTomorrowCloseGain(
            signal_close,
            future_closes[0],
            confirmed_suspended=confirmed_suspended,
            next_raw_reference=next_raw_reference,
            next_raw_close=next_raw_close,
            threshold_bps=target.threshold_bps,
        )
    actual_return = ComparableCloseReturn(signal_close, future_closes, sessions=4)
    if actual_return is None:
        return TomorrowCloseGain.PENDING
    return TomorrowCloseGain.HIT if actual_return * 10_000 >= target.threshold_bps else TomorrowCloseGain.MISS


def ComparableCloseReturn(
    signal_close: Decimal | None,
    future_closes: tuple[Decimal | None, ...],
    *,
    sessions: int,
) -> Fraction | None:
    if signal_close is None or not signal_close.is_finite() or signal_close <= 0 or sessions < 1 or len(future_closes) != sessions:
        return None
    qualified = tuple(Fraction(close) for close in future_closes if close is not None and close.is_finite() and close > 0)
    if len(qualified) != sessions:
        return None
    return sum(qualified) / (sessions * Fraction(signal_close)) - 1
