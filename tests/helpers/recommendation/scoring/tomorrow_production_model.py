from __future__ import annotations
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo
from tests.helpers.recommendation.scoring_helpers import profile_for
from trader.recommendation.application.pipeline.local_score.model_scoring import ProductionModelScoringService
from trader.recommendation.application.ports.loaded_profile import ModelInput, ModelPrediction
from trader.recommendation.domain.market.models import FeatureSnapshot
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.residualization import LEGACY_EXPOSURE_CONTRACT
from trader.recommendation.domain.selection.execution_cost import ExecutionCostPolicy

NOW = datetime(2026, 8, 31, 14, 50, tzinfo=ZoneInfo("Asia/Shanghai"))

_COST_POLICY = ExecutionCostPolicy(0.002, 1.0)


def _service(predictor: object) -> ProductionModelScoringService:
    return ProductionModelScoringService(
        profile_for(predictor),
        Strategy.TOMORROW,
        cost_policy=_COST_POLICY,
    )  # type: ignore[arg-type]


def _strategy_service(predictor: object, strategy: Strategy) -> ProductionModelScoringService:
    return ProductionModelScoringService(
        profile_for(predictor, strategy),
        strategy,
        cost_policy=_COST_POLICY,
    )  # type: ignore[arg-type]


class _Predictor:
    profile_id = "v2"
    model_id = "daily_reconstructible_ensemble"
    model_hash = "a" * 64
    feature_ids = (
        "qfq_return_1d",
        "qfq_return_3d",
        "qfq_return_5d",
        "qfq_residual_momentum_20d_skip5",
        "qfq_residual_momentum_40d_skip5",
        "qfq_residual_momentum_60d_skip5",
    )
    exposure_contract = LEGACY_EXPOSURE_CONTRACT
    industry_ids: tuple[str, ...] = ()

    def predict(self, inputs: tuple[ModelInput, ...]) -> tuple[ModelPrediction, ...]:
        return tuple(
            ModelPrediction(
                code=item.code,
                predicted_excess_return=0.006 + item.alpha_features[0],
            )
            for item in inputs
        )


def _model_feature(feature: FeatureSnapshot, *, offset: float, amihud: float) -> FeatureSnapshot:
    values = dict(feature.values)
    values.update(
        {
            "qfq_return_1d": 0.01 + offset,
            "qfq_return_3d": 0.02 + offset,
            "qfq_return_5d": 0.03 + offset,
            "qfq_momentum_20d_skip5": 0.04 + offset,
            "qfq_momentum_40d_skip5": 0.05 + offset,
            "qfq_momentum_60d_skip5": 0.06 + offset,
            "qfq_amihud_20d": amihud,
            "qfq_average_amount_20d": 100_000_000.0 + offset * 1_000_000.0,
        }
    )
    return replace(feature, values=values, history_days=61)
