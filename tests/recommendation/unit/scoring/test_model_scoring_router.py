from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from tests.helpers.recommendation.scoring_helpers import profile_for
from tests.helpers.recommendation.scoring.tomorrow_production_model import NOW, _COST_POLICY
from trader.recommendation.application.pipeline.local_score.model_router import ModelScoringRouter
from trader.recommendation.application.pipeline.local_score.model_scoring import ProductionModelScoringService, SharedModelFeatureCache
from trader.recommendation.application.ports.loaded_profile import (
    ModelInput,
    ModelPrediction,
    ModelScoringContext,
    ScoringHeadRuntimeStatus,
)
from trader.recommendation.domain.market.models import ModelIndustryReference
from trader.recommendation.domain.market.history_alignment import ModelHistoryQuality
from trader.recommendation.domain.market.v5.features import ModelPriceVolumeSequence
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.residualization import TRAINED_HEAD_EXPOSURE_CONTRACT
from trader.training.infra.profile.v2.contracts import V2_TRAINING_PROFILE
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE
from trader.training.infra.profile.v5.contracts import V5_TRAINING_PROFILE
from trader.training.infra.sample_builder import _finalize_sample_day, _PendingSample


class _ScoringCapability:
    history_required_sessions = 61

    def __init__(self, profile_id: str, strategy: Strategy) -> None:
        self.profile_id = profile_id
        self.strategy = strategy
        self.eligible_calls = 0
        self.score_calls = 0
        self.status_calls = 0

    def is_input_eligible(self, feature) -> bool:
        del feature
        self.eligible_calls += 1
        return False

    def score(self, features, *, context=None):
        del features, context
        self.score_calls += 1
        return f"{self.strategy.value}-batch"

    def status(self) -> ScoringHeadRuntimeStatus:
        self.status_calls += 1
        return ScoringHeadRuntimeStatus(
            is_enabled=True,
            profile_id=self.profile_id,  # type: ignore[arg-type]
            model_id=f"{self.strategy.value}-model",
            model_hash="a" * 64,
            scoring_version="fixture",
            activation_basis="manual_user_override",
            historical_status="historical_data_insufficient",
            historical_failure_reasons=("daily_close_proxy_not_point_in_time",),
            monitoring_mode="automatic_t1_outcome_settlement",
            automatic_model_update=False,
            loss_probability_status="not_modeled",
            runtime_anchor="15:00",
        )


def test_v2_router_delegates_both_head_capabilities() -> None:
    tomorrow = _ScoringCapability("v2", Strategy.TOMORROW)
    d25 = _ScoringCapability("v2", Strategy.D25)
    router = ModelScoringRouter("v2", {Strategy.TOMORROW: tomorrow, Strategy.D25: d25})

    assert router.history_required_sessions(Strategy.TOMORROW) == 61
    assert router.history_required_sessions(Strategy.D25) == 61
    assert router.uses_model(Strategy.TOMORROW) is True
    assert router.uses_model(Strategy.D25) is True
    assert router.score(Strategy.TOMORROW, ()) == "tomorrow-batch"
    assert router.score(Strategy.D25, ()) == "d25-batch"


def test_v3_router_delegates_two_independent_head_capabilities() -> None:
    capabilities = {strategy: _ScoringCapability("v3", strategy) for strategy in (Strategy.TOMORROW, Strategy.D25)}
    router = ModelScoringRouter("v3", capabilities)

    for strategy in capabilities:
        assert router.uses_model(strategy) is True
        assert router.history_required_sessions(strategy) == 61
        assert router.score(strategy, ()) == f"{strategy.value}-batch"
    status = router.status()
    assert status is not None
    assert status.profile_id == "v3"
    assert tuple(status.heads) == (Strategy.TOMORROW, Strategy.D25)
    assert all(capability.score_calls == 1 for capability in capabilities.values())
    assert all(capability.status_calls == 2 for capability in capabilities.values())


def test_router_rejects_long_as_a_non_scoring_strategy() -> None:
    router = ModelScoringRouter("v2", {Strategy.TOMORROW: _ScoringCapability("v2", Strategy.TOMORROW)})

    with pytest.raises(ValueError, match="long strategy"):
        router.uses_model(Strategy.LONG)


class _InputRecordingPredictor:
    model_id = "population-parity-fixture"
    model_hash = "a" * 64
    industry_ids = ("capco:modeled",)
    exposure_contract = TRAINED_HEAD_EXPOSURE_CONTRACT

    def __init__(self, profile_id: str, feature_ids: tuple[str, ...]) -> None:
        self.profile_id = profile_id
        self.feature_ids = feature_ids
        self.inputs: tuple[ModelInput, ...] = ()

    def predict(self, inputs: tuple[ModelInput, ...]) -> tuple[ModelPrediction, ...]:
        self.inputs = (*self.inputs, *inputs)
        return tuple(ModelPrediction(item.code, 0.02) for item in inputs)


def _cross_section_population(application_feature_factory, profile):
    population = []
    pending = []
    sequence_dates = tuple(NOW.date() - timedelta(days=61 - offset) for offset in range(61))
    returns = (0.001, 0.003, 0.005)
    for code, board, industry, momentum in (
        ("600000", "main", "capco:modeled", 0.02),
        ("300000", "chinext", "capco:modeled", 0.04),
        ("600001", "main", "capco:reference_only", 0.06),
        ("300001", "chinext", "capco:reference_only", 0.04),
    ):
        momenta = tuple(momentum * horizon / 20 for horizon in profile.momentum_horizons)
        feature = application_feature_factory(code, NOW)
        sequence = ModelPriceVolumeSequence(code, sequence_dates, (0.0,) * 360, "population-parity-fixture")
        values = {
            **feature.values,
            **{f"qfq_return_{horizon}d": value for horizon, value in zip((1, 3, 5), returns, strict=True)},
            **{f"qfq_momentum_{horizon}d_skip5": value for horizon, value in zip(profile.momentum_horizons, momenta, strict=True)},
            "qfq_average_amount_20d": 100_000_000.0,
            "qfq_amihud_20d": 1.0,
        }
        population.append(replace(
            feature, values=values, history_days=251, model_sequence=sequence,
            model_industry=ModelIndustryReference(industry, "中国上市公司协会上市公司行业分类标准", NOW.date(), "fixture", "fixture"),
        ))
        pending.append(_PendingSample(
            code, NOW.date(), 251, board, industry, 100_000_000.0,
            (*returns, *momenta, *(sequence.values if profile.profile_id == "v5" else ())),
            10.0, [0.01, 0.02, 0.03, 0.04, 0.05],
        ))
    return tuple(population), tuple(pending)


@pytest.mark.parametrize("profile", (V2_TRAINING_PROFILE, V3_TRAINING_PROFILE, V5_TRAINING_PROFILE), ids=lambda item: item.profile_id)
@pytest.mark.parametrize("strategy", (Strategy.TOMORROW, Strategy.D25))
def test_model_inputs_match_training_population_before_industry_prediction_filter(
    application_feature_factory, profile, strategy,
):
    contract = profile.head_for_strategy(strategy)
    auxiliary_count = len(contract.feature_manifest.names) - (360 if profile.profile_id == "v5" else 0)
    predictor = _InputRecordingPredictor(profile.profile_id, contract.feature_manifest.names[:auxiliary_count])
    population, pending = _cross_section_population(application_feature_factory, profile)
    training = {sample.code: sample for sample in _finalize_sample_day(tuple(pending), profile)}
    modeled_codes = ("600000", "300000")
    residual_position = predictor.feature_ids.index("qfq_residual_momentum_20d_skip5")
    expected = {
        code: tuple(training[code].features[position] for position in contract.feature_positions)[:auxiliary_count]
        for code in modeled_codes
    }
    assert tuple(expected[code][residual_position] for code in modeled_codes) == pytest.approx((-0.01, 0.01))
    service = ProductionModelScoringService(profile_for(predictor, strategy), strategy, cost_policy=_COST_POLICY)
    router = ModelScoringRouter(profile.profile_id, {strategy: service})
    batch = router.score(strategy, tuple(population), context=ModelScoringContext(population_features=tuple(population)))
    assert batch is not None
    assert {item.code for item in predictor.inputs} == set(modeled_codes)
    assert {item.code for item in batch.predictions} == set(modeled_codes)
    assert set(batch.missing_codes) == {"600001", "300001"}
    observed = {item.code: item.alpha_features for item in predictor.inputs}
    assert tuple(observed[code][residual_position] for code in modeled_codes) == pytest.approx((-0.01, 0.01))
    for code in modeled_codes:
        assert observed[code] == pytest.approx(expected[code])
    if profile.market_state_momentum_horizons:
        for horizon in profile.market_state_momentum_horizons:
            position = predictor.feature_ids.index(f"market_qfq_momentum_{horizon}d_skip5")
            assert tuple(observed[code][position] for code in modeled_codes) == pytest.approx((0.04 * horizon / 20,) * 2)


@pytest.mark.parametrize("profile", (V2_TRAINING_PROFILE, V3_TRAINING_PROFILE, V5_TRAINING_PROFILE), ids=lambda item: item.profile_id)
@pytest.mark.parametrize("strategy", (Strategy.TOMORROW, Strategy.D25))
def test_unverified_history_price_basis_never_reaches_any_industry_predictor(
    application_feature_factory, profile, strategy,
):
    contract = profile.head_for_strategy(strategy)
    auxiliary_count = len(contract.feature_manifest.names) - (360 if profile.profile_id == "v5" else 0)
    predictor = _InputRecordingPredictor(profile.profile_id, contract.feature_manifest.names[:auxiliary_count])
    population, _ = _cross_section_population(application_feature_factory, profile)
    blocked = tuple(
        replace(feature, model_history_quality=ModelHistoryQuality("history_price_basis_unavailable"))
        for feature in population
    )
    service = ProductionModelScoringService(profile_for(predictor, strategy), strategy, cost_policy=_COST_POLICY)
    assert all(not service.is_input_eligible(feature) for feature in blocked)
    batch = service.score(blocked, context=ModelScoringContext(population_features=blocked))
    assert predictor.inputs == () and batch.predictions == ()
    assert set(batch.missing_codes) == {feature.quote.code for feature in blocked}


@pytest.mark.parametrize("strategy", (Strategy.TOMORROW, Strategy.D25))
@pytest.mark.parametrize("invalid", ("sequence", "raw", "history", "future_industry"))
def test_invalid_reference_peers_do_not_enter_the_v5_training_residual_population(
    application_feature_factory, strategy, invalid,
):
    profile = V5_TRAINING_PROFILE
    population, pending = _cross_section_population(application_feature_factory, profile)
    reference = []
    for feature in population[2:]:
        if invalid == "sequence":
            feature = replace(feature, model_sequence=None)
        elif invalid == "raw":
            feature = replace(feature, values={**feature.values, "qfq_momentum_20d_skip5": None})
        elif invalid == "history":
            feature = replace(feature, history_days=0)
        else:
            feature = replace(feature, model_industry=replace(feature.model_industry, effective_date=NOW.date() + timedelta(days=1)))
        reference.append(feature)
    contract = profile.head_for_strategy(strategy)
    auxiliary_count = len(contract.feature_manifest.names) - 360
    predictor = _InputRecordingPredictor("v5", contract.feature_manifest.names[:auxiliary_count])
    service = ProductionModelScoringService(profile_for(predictor, strategy), strategy, cost_policy=_COST_POLICY)
    batch = service.score((*population[:2], *reference), context=ModelScoringContext(population_features=(*population[:2], *reference)))
    expected = {sample.code: sample for sample in _finalize_sample_day(pending[:2], profile)}
    assert {item.code for item in predictor.inputs} == {feature.quote.code for feature in population[:2]}
    assert set(batch.missing_codes) == {feature.quote.code for feature in reference}
    for item in predictor.inputs:
        assert item.alpha_features == pytest.approx(tuple(
            expected[item.code].features[position] for position in contract.feature_positions
        )[:auxiliary_count])


def test_v5_model_coverage_and_head_replacement_do_not_change_shared_population_residuals(application_feature_factory):
    profile = V5_TRAINING_PROFILE
    population, pending = _cross_section_population(application_feature_factory, profile)
    expected = {sample.code: sample for sample in _finalize_sample_day(pending, profile)}
    shared = SharedModelFeatureCache()
    for strategy, industries in (
        (Strategy.TOMORROW, ("capco:modeled",)),
        (Strategy.D25, ("capco:modeled", "capco:reference_only")),
    ):
        contract = profile.head_for_strategy(strategy)
        auxiliary_count = len(contract.feature_manifest.names) - 360
        predictor = _InputRecordingPredictor("v5", contract.feature_manifest.names[:auxiliary_count])
        predictor.industry_ids = industries
        predictor.model_hash = ("a" if strategy is Strategy.TOMORROW else "b") * 64
        service = ProductionModelScoringService(
            profile_for(predictor, strategy), strategy, cost_policy=_COST_POLICY, shared_features=shared,
        )
        service.score(population[:2], context=ModelScoringContext(population_features=population))
        assert {item.code for item in predictor.inputs} == {feature.quote.code for feature in population[:2]}
        for item in predictor.inputs:
            assert item.alpha_features == pytest.approx(tuple(
                expected[item.code].features[position] for position in contract.feature_positions
            )[:auxiliary_count])


@pytest.mark.parametrize("strategy", (Strategy.TOMORROW, Strategy.D25))
def test_same_v5_service_invalidates_residual_cache_when_only_unmodeled_reference_peer_changes(
    application_feature_factory, strategy,
):
    profile = V5_TRAINING_PROFILE
    population, pending = _cross_section_population(application_feature_factory, profile)
    contract = profile.head_for_strategy(strategy)
    auxiliary_count = len(contract.feature_manifest.names) - 360
    predictor = _InputRecordingPredictor("v5", contract.feature_manifest.names[:auxiliary_count])
    service = ProductionModelScoringService(profile_for(predictor, strategy), strategy, cost_policy=_COST_POLICY)
    first = service.score(population[:2], context=ModelScoringContext(
        population_features=population, population_identity="fixed-population-identity",
    ))
    first_inputs = predictor.inputs
    assert len(first_inputs) == 2
    first_training = {sample.code: sample for sample in _finalize_sample_day(pending, profile)}
    for item in first_inputs:
        assert item.alpha_features == pytest.approx(tuple(
            first_training[item.code].features[position] for position in contract.feature_positions
        )[:auxiliary_count])

    peer = population[2]
    values = dict(peer.values)
    momenta = tuple(0.10 * horizon / 20 for horizon in profile.momentum_horizons)
    values.update({
        f"qfq_momentum_{horizon}d_skip5": value
        for horizon, value in zip(profile.momentum_horizons, momenta, strict=True)
    })
    corrected_population = (*population[:2], replace(peer, values=values, merge_epoch="reference-momentum-corrected"), population[3])
    corrected_pending = (*pending[:2], replace(
        pending[2], features=(*pending[2].features[:3], *momenta, *pending[2].features[6:]),
    ), pending[3])
    second = service.score(population[:2], context=ModelScoringContext(
        population_features=corrected_population, population_identity="fixed-population-identity",
    ))
    assert len(predictor.inputs) == 4
    changed_inputs = predictor.inputs[len(first_inputs):]
    expected = {sample.code: sample for sample in _finalize_sample_day(corrected_pending, profile)}
    first_by_code = {item.code: item.alpha_features for item in first_inputs}
    for item in changed_inputs:
        assert item.alpha_features != first_by_code[item.code]
        assert item.alpha_features == pytest.approx(tuple(
            expected[item.code].features[position] for position in contract.feature_positions
        )[:auxiliary_count])
    modeled_codes = {feature.quote.code for feature in population[:2]}
    assert {item.code for item in predictor.inputs} == modeled_codes
    for batch in (first, second):
        assert {item.code for item in batch.predictions} == modeled_codes
        assert batch.missing_codes == ()
    assert second.cost_reference_version == first.cost_reference_version
