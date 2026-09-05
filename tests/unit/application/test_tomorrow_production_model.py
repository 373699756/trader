from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from trader.application.ports.tomorrow_model import TomorrowModelInput, TomorrowModelPrediction
from trader.application.recommendation.tomorrow_model_scoring import TomorrowProductionModelScoringService
from trader.domain.market.models import Board, FeatureSnapshot
from trader.domain.recommendation.model_scoring import V1_V2_EXPOSURE_CONTRACT, V3_EXPOSURE_CONTRACT

NOW = datetime(2026, 8, 31, 14, 50, tzinfo=ZoneInfo("Asia/Shanghai"))


class _Predictor:
    profile_id = "v2"
    model_id = "daily_reconstructible_ensemble_v1"
    model_hash = "a" * 64
    feature_ids = (
        "qfq_return_1d",
        "qfq_return_3d",
        "qfq_return_5d",
        "qfq_residual_momentum_20d_skip5",
        "qfq_residual_momentum_40d_skip5",
        "qfq_residual_momentum_60d_skip5",
    )
    exposure_contract = V1_V2_EXPOSURE_CONTRACT
    industry_ids: tuple[str, ...] = ()

    def predict(self, inputs: tuple[TomorrowModelInput, ...]) -> tuple[TomorrowModelPrediction, ...]:
        return tuple(
            TomorrowModelPrediction(
                code=item.code,
                predicted_excess_return=0.006 + item.alpha_features[0],
                model_disagreement=0.001,
            )
            for item in inputs
        )


def _model_feature(feature: FeatureSnapshot, *, offset: float, amihud: float) -> FeatureSnapshot:
    values = dict(feature.values)
    values.update(
        {
            "p2_return_1d": 0.01 + offset,
            "p2_return_3d": 0.02 + offset,
            "p2_return_5d": 0.03 + offset,
            "p2_momentum_20d_skip5": 0.04 + offset,
            "p2_momentum_40d_skip5": 0.05 + offset,
            "p2_momentum_60d_skip5": 0.06 + offset,
            "p2_amihud_20d": amihud,
            "p2_average_amount_20d": 100_000_000.0 + offset * 1_000_000.0,
        }
    )
    return replace(feature, values=values, history_days=61)


def test_production_model_residualizes_the_bound_features_and_maps_net_utility_to_local_score(
    application_feature_factory,
) -> None:
    features = tuple(
        _model_feature(
            application_feature_factory(f"60000{index}", NOW),
            offset=index / 100.0,
            amihud=float(index + 1),
        )
        for index in range(3)
    )

    batch = TomorrowProductionModelScoringService(_Predictor()).score(features)

    assert batch.model_version == f"daily_reconstructible_ensemble_v1:{'a' * 64}"
    assert tuple(item.code for item in batch.predictions) == ("600000", "600001", "600002")
    assert batch.scores["600002"].base_score == 100.0
    assert batch.scores["600001"].base_score == 50.0
    assert batch.scores["600000"].base_score == 0.0
    assert batch.diagnostics["600002"].predicted_net_excess_pct == pytest.approx(3.2)
    assert batch.scores["600002"].components["model_net_utility_rank"] == 100.0


def test_v1_profile_receives_only_the_residual_momentum_feature_family(application_feature_factory) -> None:
    class _V1Predictor(_Predictor):
        profile_id = "v1"
        model_id = "v1_manual_residual_momentum_v1"
        feature_ids = (
            "qfq_residual_momentum_20d_skip5",
            "qfq_residual_momentum_40d_skip5",
            "qfq_residual_momentum_60d_skip5",
        )

        def __init__(self) -> None:
            self.widths: tuple[int, ...] = ()

        def predict(self, inputs: tuple[TomorrowModelInput, ...]) -> tuple[TomorrowModelPrediction, ...]:
            self.widths = tuple(len(item.alpha_features) for item in inputs)
            return super().predict(inputs)

    predictor = _V1Predictor()
    features = tuple(
        _model_feature(
            application_feature_factory(f"60000{index}", NOW),
            offset=index / 100.0,
            amihud=float(index + 1),
        )
        for index in range(3)
    )

    batch = TomorrowProductionModelScoringService(predictor).score(features)

    assert predictor.widths == (3, 3, 3)
    assert batch.model_version == f"v1_manual_residual_momentum_v1:{'a' * 64}"


def test_v1_profile_does_not_require_the_unselected_reversal_family(application_feature_factory) -> None:
    class _V1Predictor(_Predictor):
        profile_id = "v1"
        model_id = "v1_manual_residual_momentum_v1"
        feature_ids = (
            "qfq_residual_momentum_20d_skip5",
            "qfq_residual_momentum_40d_skip5",
            "qfq_residual_momentum_60d_skip5",
        )

    complete = _model_feature(application_feature_factory("600001", NOW), offset=0.01, amihud=1.0)
    values = dict(complete.values)
    values.update({"p2_return_1d": None, "p2_return_3d": None, "p2_return_5d": None})

    batch = TomorrowProductionModelScoringService(_V1Predictor()).score((replace(complete, values=values),))

    assert set(batch.scores) == {"600001"}


def test_model_service_owns_its_history_and_profile_field_eligibility(application_feature_factory) -> None:
    service = TomorrowProductionModelScoringService(_Predictor())
    complete = _model_feature(application_feature_factory("600001", NOW), offset=0.01, amihud=1.0)
    short = replace(complete, history_days=60)
    values = dict(complete.values)
    values["p2_momentum_60d_skip5"] = None
    missing = replace(complete, values=values)

    assert service.history_required_sessions == 61
    assert service.is_input_eligible(complete) is True
    assert service.is_input_eligible(short) is False
    assert service.is_input_eligible(missing) is False


def test_production_model_does_not_fall_back_to_the_legacy_score_when_bound_features_are_missing(
    application_feature_factory,
) -> None:
    complete = _model_feature(application_feature_factory("600001", NOW), offset=0.01, amihud=1.0)
    incomplete_values = dict(complete.values)
    incomplete_values["p2_momentum_60d_skip5"] = None
    incomplete = replace(complete, quote=replace(complete.quote, code="600002"), values=incomplete_values)

    batch = TomorrowProductionModelScoringService(_Predictor()).score((complete, incomplete))

    assert set(batch.scores) == {"600001"}
    assert batch.missing_codes == ("600002",)


def test_v3_routes_each_input_to_its_current_industry_model(application_feature_factory) -> None:
    class _V3Predictor(_Predictor):
        profile_id = "v3"
        model_id = "tomorrow_v3_industry_ensemble_v1"
        industry_ids = ("银行",)
        exposure_contract = V3_EXPOSURE_CONTRACT

        def __init__(self) -> None:
            self.industries: tuple[str, ...] = ()

        def predict(self, inputs: tuple[TomorrowModelInput, ...]) -> tuple[TomorrowModelPrediction, ...]:
            self.industries = tuple(item.industry for item in inputs)
            return super().predict(inputs)

    predictor = _V3Predictor()
    supported = _model_feature(application_feature_factory("600001", NOW), offset=0.01, amihud=1.0)
    supported = replace(supported, quote=replace(supported.quote, industry="银行"))
    unsupported = _model_feature(application_feature_factory("600002", NOW), offset=0.02, amihud=2.0)
    unsupported = replace(unsupported, quote=replace(unsupported.quote, industry="未知行业"))

    batch = TomorrowProductionModelScoringService(predictor).score((supported, unsupported))

    assert predictor.industries == ("银行",)
    assert set(batch.scores) == {"600001"}
    assert batch.missing_codes == ("600002",)


def test_v3_rejects_blank_industry_before_cross_sectional_prediction(application_feature_factory) -> None:
    class _V3Predictor(_Predictor):
        profile_id = "v3"
        industry_ids = ("银行",)
        exposure_contract = V3_EXPOSURE_CONTRACT

    complete = _model_feature(application_feature_factory("600001", NOW), offset=0.01, amihud=1.0)
    complete = replace(complete, quote=replace(complete.quote, industry="银行"))
    missing_industry = replace(complete, quote=replace(complete.quote, code="600002", industry=""))

    service = TomorrowProductionModelScoringService(_V3Predictor())
    batch = service.score((complete, missing_industry))

    assert set(batch.scores) == {"600001"}
    assert batch.missing_codes == ("600002",)
    assert service.is_input_eligible(complete) is True
    assert service.is_input_eligible(missing_industry) is False

    missing_amount_values = dict(complete.values)
    missing_amount_values["p2_average_amount_20d"] = None
    assert service.is_input_eligible(replace(complete, values=missing_amount_values)) is False


def test_production_model_rejects_an_unsupported_board_from_its_cross_section(
    application_feature_factory,
) -> None:
    complete = _model_feature(application_feature_factory("600001", NOW), offset=0.01, amihud=1.0)
    unsupported = replace(
        complete,
        quote=replace(complete.quote, code="830001", board=Board.UNSUPPORTED),
    )

    batch = TomorrowProductionModelScoringService(_Predictor()).score((complete, unsupported))

    assert set(batch.scores) == {"600001"}
    assert batch.missing_codes == ("830001",)
