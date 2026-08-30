from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from trader.application.ports.tomorrow_model import TomorrowModelInput, TomorrowModelPrediction
from trader.application.tomorrow_model_scoring import TomorrowProductionModelScoringService
from trader.domain.market.models import Board, FeatureSnapshot

NOW = datetime(2026, 8, 31, 14, 50, tzinfo=ZoneInfo("Asia/Shanghai"))


class _Predictor:
    model_id = "daily_reconstructible_ensemble_v1"
    model_hash = "a" * 64

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
    assert "p2_predicted_net_excess_pct" not in batch.scores["600002"].components


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
