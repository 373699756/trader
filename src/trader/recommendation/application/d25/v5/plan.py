"""Completed sequence and model-percentile ranking choices for the V5 D25 head."""

from trader.recommendation.application.pipeline.local_score.model_scoring import CrossSectionalScoringPlan
from trader.recommendation.application.runtime.schedule import ShanghaiNow
from trader.recommendation.domain.market.models import FeatureSnapshot


def _sequence_eligible(feature: FeatureSnapshot) -> bool:
    sequence = feature.model_sequence
    market_day = (
        feature.preview_context.market_trade_date
        if feature.preview_context is not None
        else ShanghaiNow(feature.observed_at).date()
    )
    return sequence is not None and sequence.code == feature.quote.code and sequence.dates[-1] < market_day


V5_D25_PLAN = CrossSectionalScoringPlan(
    "v5", sequence_eligible=_sequence_eligible, rank_predictions=True, parallel_industries=True,
)
