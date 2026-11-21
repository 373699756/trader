"""Input contract for the V3 Tomorrow logistic classifier."""

from trader.recommendation.application.pipeline.local_score.tomorrow_probability import (
    BuildQuoteFeatures,
    TomorrowScoringPlan,
)

V3_TOMORROW_PLAN = TomorrowScoringPlan("v3", 20, BuildQuoteFeatures)
