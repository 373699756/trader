from __future__ import annotations
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from tests.helpers.recommendation.scoring_helpers import profile_for
from trader.recommendation.application.pipeline.local_score.model_router import ModelScoringRouter
from trader.recommendation.application.pipeline.local_score.model_scoring import ProductionModelScoringService
from trader.recommendation.application.ports.loaded_profile import ModelInput, ModelPrediction
from trader.recommendation.application.ports.scoring import ScoredNativeInput, TomorrowNativeInput
from trader.recommendation.domain.market.models import FeatureSnapshot
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.residualization import (
    LEGACY_EXPOSURE_CONTRACT,
    TRAINED_HEAD_EXPOSURE_CONTRACT,
)
from trader.recommendation.domain.selection.execution_cost import ExecutionCostPolicy

SHANGHAI = ZoneInfo("Asia/Shanghai")

TRADE_DATE = date(2026, 7, 29)

EVALUATED_AT = datetime(2026, 7, 29, 14, 40, tzinfo=SHANGHAI)

PROJECT_ROOT = Path(__file__).resolve().parents[4]


class _ProductionPredictor:
    profile_id = "v2"
    model_id = "daily_reconstructible_ensemble"
    model_hash = "b" * 64
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
        return tuple(ModelPrediction(item.code, 0.01 + index / 1000.0) for index, item in enumerate(inputs))


class _RecordingProductionPredictor(_ProductionPredictor):
    def __init__(self) -> None:
        self.codes: tuple[str, ...] = ()

    def predict(self, inputs: tuple[ModelInput, ...]) -> tuple[ModelPrediction, ...]:
        self.codes = tuple(item.code for item in inputs)
        return super().predict(inputs)


class _NonPositiveProductionPredictor(_ProductionPredictor):
    def predict(self, inputs: tuple[ModelInput, ...]) -> tuple[ModelPrediction, ...]:
        return tuple(ModelPrediction(item.code, 0.001 + (index % 3) * 0.0001) for index, item in enumerate(inputs))


class _IndustryProductionPredictor(_ProductionPredictor):
    exposure_contract = TRAINED_HEAD_EXPOSURE_CONTRACT
    industry_ids = ("capco:J66货币金融服务",)


def _router(predictor: object) -> ModelScoringRouter:
    service = ProductionModelScoringService(
        profile_for(predictor),
        Strategy.TOMORROW,
        cost_policy=ExecutionCostPolicy(0.002, 1.0),
    )  # type: ignore[arg-type]
    return ModelScoringRouter("v2", {Strategy.TOMORROW: service})


def _native_input(
    features: tuple[FeatureSnapshot, ...],
    strategy: type[ScoredNativeInput] = TomorrowNativeInput,
) -> ScoredNativeInput:
    return strategy(
        trade_date=TRADE_DATE,
        phase="final_review",
        data_version="candidate-data:fixture",
        config_version="runtime-current+strategy-fixture",
        evaluated_at=EVALUATED_AT,
        market_features=features,
        requested_codes=tuple(feature.quote.code for feature in features),
        candidate_features=features,
        preselect_max_age_seconds=30.0,
        score_max_age_seconds=30.0,
    )


def _verified_feature(feature: FeatureSnapshot) -> FeatureSnapshot:
    return replace(
        feature,
        quote=replace(
            feature.quote,
            cross_source_verified=True,
            cross_source_deviation_pct=0.1,
        ),
    )


def _with_model_features(feature: FeatureSnapshot, index: int) -> FeatureSnapshot:
    values = dict(feature.values)
    offset = index / 1000.0
    values.update(
        {
            "qfq_return_1d": 0.01 + offset,
            "qfq_return_3d": 0.02 + offset,
            "qfq_return_5d": 0.03 + offset,
            "qfq_momentum_20d_skip5": 0.04 + offset,
            "qfq_momentum_40d_skip5": 0.05 + offset,
            "qfq_momentum_60d_skip5": 0.06 + offset,
            "qfq_amihud_20d": 0.001 + offset,
            "qfq_average_amount_20d": 100_000_000.0 + index,
        }
    )
    return replace(feature, values=values, history_days=61)
