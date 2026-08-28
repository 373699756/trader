"""Pure immutable contract for the preregistered Tomorrow shadow family."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

TomorrowShadowChallengerId = Literal[
    "residual_reversal_v1",
    "residual_momentum_v1",
    "session_decomposition_v1",
    "cost_risk_adjusted_v1",
    "constrained_ensemble_v1",
]
TOMORROW_SHADOW_CHALLENGER_FAMILY: tuple[TomorrowShadowChallengerId, ...] = (
    "residual_reversal_v1",
    "residual_momentum_v1",
    "session_decomposition_v1",
    "cost_risk_adjusted_v1",
    "constrained_ensemble_v1",
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COST_RATES = (0.002, 0.005, 0.01)
_BLOCK_DAYS = (3, 5, 10)


@dataclass(frozen=True)
class TomorrowShadowChallenger:
    challenger_id: TomorrowShadowChallengerId
    feature_families: tuple[str, ...]
    model_families: tuple[str, ...]
    model_weights: tuple[float, ...]
    selection_mode: str
    version: str

    def __post_init__(self) -> None:
        if self.challenger_id not in TOMORROW_SHADOW_CHALLENGER_FAMILY:
            raise ValueError("Tomorrow shadow challenger is outside the fixed family")
        if not self.feature_families or not self.model_families or not self.selection_mode or not self.version:
            raise ValueError("Tomorrow shadow challenger definition is incomplete")
        if len(self.model_weights) != len(self.model_families) or not math.isclose(
            math.fsum(self.model_weights), 1.0, abs_tol=1e-12
        ):
            raise ValueError("Tomorrow shadow challenger model weights must sum to one")
        if any(weight <= 0.0 or not math.isfinite(weight) for weight in self.model_weights):
            raise ValueError("Tomorrow shadow challenger model weights are invalid")


_CHALLENGERS = (
    TomorrowShadowChallenger(
        "residual_reversal_v1",
        ("residual_reversal",),
        ("linear",),
        (1.0,),
        "cost_aware_top6",
        "residual_reversal_v1",
    ),
    TomorrowShadowChallenger(
        "residual_momentum_v1",
        ("residual_momentum",),
        ("linear",),
        (1.0,),
        "cost_aware_top6",
        "residual_momentum_v1",
    ),
    TomorrowShadowChallenger(
        "session_decomposition_v1",
        ("overnight", "intraday", "tail"),
        ("linear",),
        (1.0,),
        "cost_aware_top6",
        "session_decomposition_v1",
    ),
    TomorrowShadowChallenger(
        "cost_risk_adjusted_v1",
        ("residual_reversal", "residual_momentum", "overnight", "intraday", "tail"),
        ("linear",),
        (1.0,),
        "calibrated_cost_risk_top6",
        "cost_risk_adjusted_v1",
    ),
    TomorrowShadowChallenger(
        "constrained_ensemble_v1",
        ("residual_reversal", "residual_momentum", "overnight", "intraday", "tail"),
        ("linear", "lightgbm"),
        (0.5, 0.5),
        "calibrated_constrained_ensemble_top6",
        "constrained_ensemble_v1",
    ),
)


@dataclass(frozen=True)
class TomorrowShadowPreregistration:
    research_identity: str
    preregistered_on: date
    historical_dates: tuple[date, ...]
    forward_dates: tuple[date, ...]
    challengers: tuple[TomorrowShadowChallenger, ...]
    feature_schema: str = "score_tomorrow_point_in_time_features_v1"
    shadow_schema: str = "score_tomorrow_shadow_report_v1"
    selection_schema: str = "score_tomorrow_cost_aware_selection_report_v1"
    cost_rates: tuple[float, ...] = _COST_RATES
    bootstrap_block_days: tuple[int, ...] = _BLOCK_DAYS
    primary_block_days: int = 5
    bootstrap_master_seed: int = 20260828
    bootstrap_repetitions: int = 10_000
    holm_alpha: float = 0.05
    minimum_total_pairs: int = 300
    minimum_forward_pairs: int = 100
    minimum_mean_increment: float = 0.002
    maximum_turnover_increase: float = 0.05
    minimum_oracle_recall: float = 0.99
    maximum_stock_positive_fraction: float = 0.10
    maximum_top_five_positive_fraction: float = 0.30
    production_authority: bool = False
    schema_version: str = "score_tomorrow_shadow_preregistration_v1"
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        if self.research_identity != "score_tomorrow_shadow_p1_v1" or self.preregistered_on != date(2026, 8, 28):
            raise ValueError("Tomorrow shadow preregistration identity is frozen")
        if len(self.historical_dates) != 40 or len(self.forward_dates) != 20:
            raise ValueError("Tomorrow shadow preregistration requires fixed 40+20 dates")
        if self.preregistered_on >= self.historical_dates[0]:
            raise ValueError("Tomorrow shadow preregistration must precede its first planned date")
        all_dates = (*self.historical_dates, *self.forward_dates)
        if len(set(all_dates)) != 60 or tuple(sorted(all_dates)) != all_dates:
            raise ValueError("Tomorrow shadow planned dates must be unique and ordered")
        if any(value.weekday() >= 5 for value in all_dates):
            raise ValueError("Tomorrow shadow planned dates must be weekdays")
        if tuple(item.challenger_id for item in self.challengers) != TOMORROW_SHADOW_CHALLENGER_FAMILY:
            raise ValueError("Tomorrow shadow preregistration requires the fixed five-challenger family")
        if self.challengers != _CHALLENGERS:
            raise ValueError("Tomorrow shadow challenger definitions are frozen")
        if (
            self.cost_rates != _COST_RATES
            or self.bootstrap_block_days != _BLOCK_DAYS
            or self.primary_block_days != 5
            or self.bootstrap_master_seed != 20260828
            or self.bootstrap_repetitions != 10_000
            or self.holm_alpha != 0.05
            or self.minimum_total_pairs != 300
            or self.minimum_forward_pairs != 100
            or self.minimum_mean_increment != 0.002
            or self.maximum_turnover_increase != 0.05
            or self.minimum_oracle_recall != 0.99
            or self.maximum_stock_positive_fraction != 0.10
            or self.maximum_top_five_positive_fraction != 0.30
        ):
            raise ValueError("Tomorrow shadow frozen promotion thresholds are invalid")
        if self.production_authority or self.schema_version != "score_tomorrow_shadow_preregistration_v1":
            raise ValueError("Tomorrow shadow preregistration cannot authorize production")
        object.__setattr__(self, "content_hash", _canonical_hash(self))

    @property
    def challenger_family(self) -> tuple[TomorrowShadowChallengerId, ...]:
        return tuple(item.challenger_id for item in self.challengers)


def _weekdays(start: date, end: date) -> tuple[date, ...]:
    return tuple(
        date.fromordinal(ordinal)
        for ordinal in range(start.toordinal(), end.toordinal() + 1)
        if date.fromordinal(ordinal).weekday() < 5
    )


@dataclass(frozen=True)
class TomorrowShadowCalendarAttestation:
    research_spec_hash: str
    confirmed_on: date
    authority_document_hash: str
    trading_dates: tuple[date, ...]
    authority: Literal["shanghai_stock_exchange"] = "shanghai_stock_exchange"
    schema_version: str = "score_tomorrow_shadow_calendar_attestation_v1"
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        spec = TOMORROW_SHADOW_P1_SPEC
        if self.research_spec_hash != spec.content_hash:
            raise ValueError("calendar attestation research spec does not match")
        if _SHA256.fullmatch(self.authority_document_hash) is None:
            raise ValueError("calendar attestation authority document hash is invalid")
        if self.confirmed_on >= spec.historical_dates[0]:
            raise ValueError("calendar attestation must be sealed before the first planned date")
        if self.trading_dates != (*spec.historical_dates, *spec.forward_dates):
            raise ValueError("calendar attestation dates must exactly match the frozen window")
        if (
            self.authority != "shanghai_stock_exchange"
            or self.schema_version != "score_tomorrow_shadow_calendar_attestation_v1"
        ):
            raise ValueError("calendar attestation authority or schema is invalid")
        object.__setattr__(self, "content_hash", _canonical_hash(self))


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(_canonical(value), ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _canonical(value: object) -> object:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: _canonical(getattr(value, field.name)) for field in dataclasses.fields(value) if field.init}
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_canonical(item) for item in value]
    return value


TOMORROW_SHADOW_P1_SPEC = TomorrowShadowPreregistration(
    research_identity="score_tomorrow_shadow_p1_v1",
    preregistered_on=date(2026, 8, 28),
    historical_dates=_weekdays(date(2027, 6, 14), date(2027, 8, 6)),
    forward_dates=_weekdays(date(2027, 8, 9), date(2027, 9, 3)),
    challengers=_CHALLENGERS,
)


__all__ = [
    "TOMORROW_SHADOW_CHALLENGER_FAMILY",
    "TOMORROW_SHADOW_P1_SPEC",
    "TomorrowShadowCalendarAttestation",
    "TomorrowShadowChallenger",
    "TomorrowShadowChallengerId",
    "TomorrowShadowPreregistration",
]
