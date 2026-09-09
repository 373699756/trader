from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from tests.unit.application.scoring_helpers import profile_for
from trader.application.ports.model_scoring import (
    ModelInput,
    ModelPrediction,
    ModelScoringContext,
    ModelScoringDeadlineError,
)
from trader.application.recommendation.tomorrow_model_scoring import TomorrowProductionModelScoringService
from trader.domain.market.models import Board, FeatureSnapshot
from trader.domain.recommendation.model_scoring import V1_V2_EXPOSURE_CONTRACT, V3_EXPOSURE_CONTRACT

NOW = datetime(2026, 8, 31, 14, 50, tzinfo=ZoneInfo("Asia/Shanghai"))


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
    exposure_contract = V1_V2_EXPOSURE_CONTRACT
    industry_ids: tuple[str, ...] = ()

    def predict(self, inputs: tuple[ModelInput, ...]) -> tuple[ModelPrediction, ...]:
        return tuple(
            ModelPrediction(
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


def test_production_model_residualizes_bound_features_and_maps_prediction_rank_to_local_score(
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

    batch = TomorrowProductionModelScoringService(profile_for(_Predictor())).score(features)

    assert batch.model_version == f"daily_reconstructible_ensemble:{'a' * 64}"
    assert tuple(item.code for item in batch.predictions) == ("600000", "600001", "600002")
    assert batch.scores["600002"].base_score == 100.0
    assert batch.scores["600001"].base_score == 50.0
    assert batch.scores["600000"].base_score == 0.0
    assert batch.diagnostics["600002"].predicted_net_excess_pct == pytest.approx(3.2)
    assert batch.scores["600002"].components["model_prediction_rank"] == 100.0


def test_non_positive_net_utility_keeps_relative_scores_for_observability(
    application_feature_factory,
) -> None:
    class _NonPositivePredictor(_Predictor):
        def predict(self, inputs: tuple[ModelInput, ...]) -> tuple[ModelPrediction, ...]:
            return tuple(ModelPrediction(item.code, 0.001 + index * 0.0004, 0.0) for index, item in enumerate(inputs))

    features = tuple(
        _model_feature(
            application_feature_factory(f"60000{index}", NOW),
            offset=index / 100.0,
            amihud=float(index + 1),
        )
        for index in range(3)
    )

    batch = TomorrowProductionModelScoringService(profile_for(_NonPositivePredictor())).score(features)

    assert all(item.predicted_net_excess_pct < 0.0 for item in batch.diagnostics.values())
    assert {code: score.base_score for code, score in batch.scores.items()} == {
        "600000": 0.0,
        "600001": 50.0,
        "600002": 100.0,
    }
    assert batch.scores["600002"].components["model_prediction_rank"] == 100.0


def test_equal_predictions_and_cost_inputs_have_equal_scores_costs_and_utility(
    application_feature_factory,
) -> None:
    class _EqualPredictor(_Predictor):
        def predict(self, inputs: tuple[ModelInput, ...]) -> tuple[ModelPrediction, ...]:
            return tuple(ModelPrediction(item.code, 0.0025, 0.0) for item in inputs)

    features = tuple(
        _model_feature(
            application_feature_factory(f"60000{index}", NOW),
            offset=index / 100.0,
            amihud=1.0,
        )
        for index in range(3)
    )

    batch = TomorrowProductionModelScoringService(profile_for(_EqualPredictor())).score(features)

    assert {score.base_score for score in batch.scores.values()} == {50.0}
    assert tuple(item.estimated_cost_pct for item in batch.diagnostics.values()) == pytest.approx((0.3, 0.3, 0.3))
    assert tuple(item.predicted_net_excess_pct for item in batch.diagnostics.values()) == pytest.approx(
        (-0.05, -0.05, -0.05)
    )


def test_single_prediction_uses_neutral_rank_for_score_and_cost(application_feature_factory) -> None:
    class _SinglePredictor(_Predictor):
        def predict(self, inputs: tuple[ModelInput, ...]) -> tuple[ModelPrediction, ...]:
            return tuple(ModelPrediction(item.code, 0.0025, 0.0) for item in inputs)

    feature = _model_feature(application_feature_factory("600001", NOW), offset=0.0, amihud=1.0)

    batch = TomorrowProductionModelScoringService(profile_for(_SinglePredictor())).score((feature,))

    assert batch.scores["600001"].base_score == 50.0
    assert batch.diagnostics["600001"].estimated_cost_pct == pytest.approx(0.3)
    assert batch.diagnostics["600001"].predicted_net_excess_pct == pytest.approx(-0.05)


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

        def predict(self, inputs: tuple[ModelInput, ...]) -> tuple[ModelPrediction, ...]:
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

    batch = TomorrowProductionModelScoringService(profile_for(predictor)).score(features)

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
    values.update({"qfq_return_1d": None, "qfq_return_3d": None, "qfq_return_5d": None})

    batch = TomorrowProductionModelScoringService(profile_for(_V1Predictor())).score(
        (replace(complete, values=values),)
    )

    assert set(batch.scores) == {"600001"}


def test_model_service_owns_its_history_and_profile_field_eligibility(application_feature_factory) -> None:
    service = TomorrowProductionModelScoringService(profile_for(_Predictor()))
    complete = _model_feature(application_feature_factory("600001", NOW), offset=0.01, amihud=1.0)
    short = replace(complete, history_days=60)
    values = dict(complete.values)
    values["qfq_momentum_60d_skip5"] = None
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
    incomplete_values["qfq_momentum_60d_skip5"] = None
    incomplete = replace(complete, quote=replace(complete.quote, code="600002"), values=incomplete_values)

    batch = TomorrowProductionModelScoringService(profile_for(_Predictor())).score((complete, incomplete))

    assert set(batch.scores) == {"600001"}
    assert batch.missing_codes == ("600002",)


def test_production_model_skips_physical_prediction_when_every_input_is_ineligible(
    application_feature_factory,
) -> None:
    class _CountingPredictor(_Predictor):
        def __init__(self) -> None:
            self.calls = 0

        def predict(self, inputs: tuple[ModelInput, ...]) -> tuple[ModelPrediction, ...]:
            self.calls += 1
            return super().predict(inputs)

    predictor = _CountingPredictor()
    complete = _model_feature(application_feature_factory("600001", NOW), offset=0.01, amihud=1.0)
    incomplete_values = dict(complete.values)
    incomplete_values["qfq_momentum_60d_skip5"] = None

    batch = TomorrowProductionModelScoringService(profile_for(predictor)).score(
        (replace(complete, values=incomplete_values),)
    )

    assert predictor.calls == 0
    assert batch.scores == {}
    assert batch.missing_codes == ("600001",)


def test_v3_routes_each_input_to_its_current_industry_model(application_feature_factory) -> None:
    class _V3Predictor(_Predictor):
        profile_id = "v3"
        model_id = "industry_ensemble_training"
        industry_ids = ("银行",)
        exposure_contract = V3_EXPOSURE_CONTRACT

        def __init__(self) -> None:
            self.industries: tuple[str, ...] = ()

        def predict(self, inputs: tuple[ModelInput, ...]) -> tuple[ModelPrediction, ...]:
            self.industries = tuple(item.industry for item in inputs)
            return super().predict(inputs)

    predictor = _V3Predictor()
    supported = _model_feature(application_feature_factory("600001", NOW), offset=0.01, amihud=1.0)
    supported = replace(supported, quote=replace(supported.quote, industry="银行"))
    unsupported = _model_feature(application_feature_factory("600002", NOW), offset=0.02, amihud=2.0)
    unsupported = replace(unsupported, quote=replace(unsupported.quote, industry="未知行业"))

    batch = TomorrowProductionModelScoringService(profile_for(predictor)).score((supported, unsupported))

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

    service = TomorrowProductionModelScoringService(profile_for(_V3Predictor()))
    batch = service.score((complete, missing_industry))

    assert set(batch.scores) == {"600001"}
    assert batch.missing_codes == ("600002",)
    assert service.is_input_eligible(complete) is True
    assert service.is_input_eligible(missing_industry) is False

    missing_amount_values = dict(complete.values)
    missing_amount_values["qfq_average_amount_20d"] = None
    assert service.is_input_eligible(replace(complete, values=missing_amount_values)) is False


def test_production_model_rejects_an_unsupported_board_from_its_cross_section(
    application_feature_factory,
) -> None:
    complete = _model_feature(application_feature_factory("600001", NOW), offset=0.01, amihud=1.0)
    unsupported = replace(
        complete,
        quote=replace(complete.quote, code="830001", board=Board.UNSUPPORTED),
    )

    batch = TomorrowProductionModelScoringService(profile_for(_Predictor())).score((complete, unsupported))

    assert set(batch.scores) == {"600001"}
    assert batch.missing_codes == ("830001",)


def test_incremental_scoring_reuses_unchanged_groups_and_batches_prediction(
    application_feature_factory,
) -> None:
    class _CountingPredictor(_Predictor):
        def __init__(self) -> None:
            self.calls = 0

        def predict(self, inputs: tuple[ModelInput, ...]) -> tuple[ModelPrediction, ...]:
            self.calls += 1
            return super().predict(inputs)

    predictor = _CountingPredictor()
    service = TomorrowProductionModelScoringService(profile_for(predictor))
    features = tuple(
        _model_feature(application_feature_factory(f"60000{index}", NOW), offset=index / 100.0, amihud=index + 1.0)
        for index in range(3)
    )

    first = service.score(features)
    second = service.score(features)
    status = service.computation_status()

    assert second is first
    assert predictor.calls == 1
    assert status.candidate_count == 3
    assert status.request_count == 2
    assert status.cache_hit_count == 1
    assert status.predictor_batch_count == 1
    assert status.computed_groups == ()
    assert status.reused_groups == ("daily_return", "skip_recent_momentum", "cross_section_residual")


def test_incremental_scoring_recomputes_only_catalog_dependent_groups(
    application_feature_factory,
) -> None:
    class _CountingPredictor(_Predictor):
        def __init__(self) -> None:
            self.calls = 0

        def predict(self, inputs: tuple[ModelInput, ...]) -> tuple[ModelPrediction, ...]:
            self.calls += 1
            return super().predict(inputs)

    predictor = _CountingPredictor()
    service = TomorrowProductionModelScoringService(profile_for(predictor))
    features = tuple(
        _model_feature(application_feature_factory(f"60000{index}", NOW), offset=index / 100.0, amihud=index + 1.0)
        for index in range(3)
    )
    service.score(features)
    values = dict(features[0].values)
    values["qfq_return_1d"] = 0.123

    service.score((replace(features[0], values=values), *features[1:]))
    status = service.computation_status()

    assert predictor.calls == 2
    assert status.computed_groups == ("daily_return",)
    assert status.reused_groups == ("skip_recent_momentum", "cross_section_residual")


def test_incremental_scoring_propagates_momentum_changes_and_keeps_cost_outside_the_model(
    application_feature_factory,
) -> None:
    class _CountingPredictor(_Predictor):
        def __init__(self) -> None:
            self.calls = 0

        def predict(self, inputs: tuple[ModelInput, ...]) -> tuple[ModelPrediction, ...]:
            self.calls += 1
            return super().predict(inputs)

    predictor = _CountingPredictor()
    service = TomorrowProductionModelScoringService(profile_for(predictor))
    features = tuple(
        _model_feature(application_feature_factory(f"60000{index}", NOW), offset=index / 100.0, amihud=index + 1.0)
        for index in range(3)
    )
    service.score(features)
    momentum_values = dict(features[0].values)
    momentum_values["qfq_momentum_20d_skip5"] = 0.321
    momentum_features = (replace(features[0], values=momentum_values), *features[1:])

    service.score(momentum_features)
    momentum_status = service.computation_status()
    cost_values = dict(momentum_features[0].values)
    cost_values["qfq_amihud_20d"] = 99.0
    service.score((replace(momentum_features[0], values=cost_values), *momentum_features[1:]))
    cost_status = service.computation_status()

    assert momentum_status.computed_groups == ("skip_recent_momentum", "cross_section_residual")
    assert predictor.calls == 2
    assert cost_status.computed_groups == ()
    assert cost_status.reused_groups == ("daily_return", "skip_recent_momentum", "cross_section_residual")


def test_incremental_scoring_ignores_quote_overlay_when_model_facts_are_unchanged(
    application_feature_factory,
) -> None:
    class _CountingPredictor(_Predictor):
        def __init__(self) -> None:
            self.calls = 0

        def predict(self, inputs: tuple[ModelInput, ...]) -> tuple[ModelPrediction, ...]:
            self.calls += 1
            return super().predict(inputs)

    predictor = _CountingPredictor()
    service = TomorrowProductionModelScoringService(profile_for(predictor))
    feature = _model_feature(application_feature_factory("600001", NOW), offset=0.01, amihud=1.0)
    first = service.score((feature,))
    overlay = replace(
        feature,
        quote=replace(feature.quote, price=(feature.quote.price or 0.0) + 0.01, data_version="overlay"),
    )

    second = service.score((overlay,))

    assert second is first
    assert predictor.calls == 1


def test_incremental_scoring_fails_closed_at_its_deadline_and_retains_the_last_batch(
    application_feature_factory,
) -> None:
    service = TomorrowProductionModelScoringService(profile_for(_Predictor()))
    feature = _model_feature(application_feature_factory("600001", NOW), offset=0.01, amihud=1.0)
    accepted = service.score((feature,))

    with pytest.raises(ModelScoringDeadlineError, match="before_feature_computation"):
        service.score((feature,), context=ModelScoringContext(time_budget_seconds=0.0, input_age_seconds=2.5))

    status = service.computation_status()
    assert status.deadline_abandon_reason == "before_feature_computation"
    assert status.decision_age_ms >= 2500.0
    assert service.score((feature,)) is accepted
