"""Runtime feature and local-score ranking choices for the V3 D25 head."""

from trader.recommendation.application.pipeline.local_score.model_scoring import CrossSectionalScoringPlan

V3_D25_PLAN = CrossSectionalScoringPlan("v3")
