"""Input and bounded prediction batches for the V5 Tomorrow sequence model."""

from trader.recommendation.application.pipeline.local_score.tomorrow_probability import (
    BuildQuoteFeatures,
    TomorrowScoringPlan,
)
from trader.recommendation.application.runtime.schedule import TradeDateAt
from trader.recommendation.domain.market.models import FeatureSnapshot
from trader.recommendation.domain.market.tomorrow_probability import BuildTomorrowSequenceFeatures
from trader.recommendation.domain.market.v5.features import SEQUENCE_STEPS


def _features(feature: FeatureSnapshot) -> tuple[float, ...] | None:
    base = BuildQuoteFeatures(feature)
    sequence = feature.model_sequence
    quality = feature.model_history_quality
    if base is None or sequence is None or sequence.code != feature.quote.code:
        return None
    if quality is not None and quality.failure is not None:
        return None
    signal_date = (
        feature.preview_context.market_trade_date
        if feature.preview_context is not None
        else TradeDateAt(feature.observed_at)
    )
    if TradeDateAt(feature.quote.source_time) != signal_date:
        return None
    return BuildTomorrowSequenceFeatures(base, sequence, signal_date=signal_date)


V5_TOMORROW_PLAN = TomorrowScoringPlan("v5", SEQUENCE_STEPS + 1, _features, batch_size=256)
