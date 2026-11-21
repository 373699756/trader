"""Input contract for the V1 cross-market Tomorrow ranker."""

from trader.recommendation.application.pipeline.local_score.tomorrow_probability import (
    BuildQuoteFeatures,
    TomorrowScoringPlan,
)

V1_TOMORROW_PLAN = TomorrowScoringPlan("v1", 20, BuildQuoteFeatures)
