"""Input contract for the V2 Tomorrow boosted-tree classifier."""

from trader.recommendation.application.pipeline.local_score.tomorrow_probability import (
    BuildQuoteFeatures,
    TomorrowScoringPlan,
)

V2_TOMORROW_PLAN = TomorrowScoringPlan("v2", 20, BuildQuoteFeatures)
