"""Runtime feature and local-score ranking choices for the V2 D25 head."""

from trader.recommendation.application.pipeline.local_score.model_scoring import CrossSectionalScoringPlan

V2_D25_PLAN = CrossSectionalScoringPlan("v2", supports_extended_momentum=True)
