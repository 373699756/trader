"""Typed V2 native-input offers shared by scored strategy stages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING

from trader.application.ports.tomorrow import D25NativeInput, TodayNativeInput, TomorrowNativeInput
from trader.domain.market.models import FeatureSnapshot
from trader.domain.recommendation.models import Strategy

if TYPE_CHECKING:
    from trader.application.pipeline import RecommendationPipeline


@dataclass(frozen=True)
class ScoredNativeBatch:
    trade_date: date
    phase: str
    requested_codes: tuple[str, ...]
    candidate_features: tuple[FeatureSnapshot, ...]
    data_version: str
    evaluated_at: datetime
    market_features: tuple[FeatureSnapshot, ...]
    preselect_max_age_seconds: float
    score_max_age_seconds: float


def offer_scored_native_input(
    pipeline: RecommendationPipeline,
    strategy: Strategy,
    batch: ScoredNativeBatch,
) -> None:
    if not batch.market_features:
        return
    try:
        if strategy is Strategy.TODAY:
            today_sink = pipeline._today_native_inputs
            if today_sink is None:
                return
            accepted = today_sink.offer_native(
                TodayNativeInput(
                    batch.trade_date,
                    batch.phase,
                    batch.data_version,
                    pipeline._config_version,
                    batch.evaluated_at,
                    batch.market_features,
                    batch.requested_codes,
                    batch.candidate_features,
                    batch.preselect_max_age_seconds,
                    batch.score_max_age_seconds,
                    pipeline._candidate_pool_size,
                )
            )
        elif strategy is Strategy.TOMORROW:
            tomorrow_sink = pipeline._tomorrow_native_inputs
            if tomorrow_sink is None:
                return
            accepted = tomorrow_sink.offer_native(
                TomorrowNativeInput(
                    batch.trade_date,
                    batch.phase,
                    batch.data_version,
                    pipeline._config_version,
                    batch.evaluated_at,
                    batch.market_features,
                    batch.requested_codes,
                    batch.candidate_features,
                    batch.preselect_max_age_seconds,
                    batch.score_max_age_seconds,
                    pipeline._candidate_pool_size,
                )
                )
        elif strategy is Strategy.D25:
            d25_sink = pipeline._d25_native_inputs
            if d25_sink is None:
                return
            accepted = d25_sink.offer_native(
                D25NativeInput(
                    batch.trade_date,
                    batch.phase,
                    batch.data_version,
                    pipeline._config_version,
                    batch.evaluated_at,
                    batch.market_features,
                    batch.requested_codes,
                    batch.candidate_features,
                    batch.preselect_max_age_seconds,
                    batch.score_max_age_seconds,
                    batch.candidate_pool_size,
                )
            )
        else:
            raise ValueError("native input offer only supports Today, Tomorrow, and D25")
    except (RuntimeError, TypeError, ValueError) as exc:
        pipeline._state.increment(f"{strategy.value}_native_inputs_failed")
        pipeline._state.record_error(f"{strategy.value} native input degraded: {type(exc).__name__}")
        return
    suffix = "offered" if accepted else "rejected"
    pipeline._state.increment(f"{strategy.value}_native_inputs_{suffix}")


__all__ = ["ScoredNativeBatch", "offer_scored_native_input"]
