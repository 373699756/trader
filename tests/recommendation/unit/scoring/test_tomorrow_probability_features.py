from dataclasses import replace
from datetime import timedelta

import pytest

from tests.helpers.recommendation.publication.tomorrow_projection import EVALUATED_AT
from trader.recommendation.application.pipeline.local_score.tomorrow_probability import TomorrowProbabilityCapability
from trader.recommendation.application.ports.loaded_profile import ModelScoringContext, ModelScoringDeadlineError
from trader.recommendation.application.tomorrow.v1.plan import V1_TOMORROW_PLAN
from trader.recommendation.application.tomorrow.v2.plan import V2_TOMORROW_PLAN
from trader.recommendation.application.tomorrow.v3.plan import V3_TOMORROW_PLAN
from trader.recommendation.application.tomorrow.v5.plan import V5_TOMORROW_PLAN
from trader.recommendation.domain.market.history_alignment import ModelHistoryQuality
from trader.recommendation.domain.market.tomorrow_probability import (
    TOMORROW_FEATURE_NAMES,
    TOMORROW_SEQUENCE_FEATURE_NAMES,
)
from trader.recommendation.domain.market.v5.features import SEQUENCE_CHANNELS, SEQUENCE_STEPS, ModelPriceVolumeSequence
from trader.recommendation.domain.publication.preview import PreviewContext


class _RecordingPredictor:
    identity = "a" * 64

    def __init__(self) -> None:
        self.batches: list[tuple[tuple[float, ...], ...]] = []

    def predict_probability(self, features: tuple[float, ...]) -> float:
        return 0.75

    def predict_probabilities(self, rows: tuple[tuple[float, ...], ...]) -> tuple[float, ...]:
        self.batches.append(rows)
        return tuple(self.predict_probability(row) for row in rows)


@pytest.mark.parametrize("profile", ("v1", "v2", "v3", "v5"))
def test_each_tomorrow_profile_supplies_the_model_input_contract(profile, application_feature_factory):
    feature = application_feature_factory("600001", EVALUATED_AT)
    quote = replace(feature.quote, open_price=10.0, high=12.0, low=9.0, price=11.0, volume=2_000_000.0)
    dates = tuple(EVALUATED_AT.date() - timedelta(days=61 - index) for index in range(61))
    values = tuple(float(index) for index in range(SEQUENCE_STEPS * len(SEQUENCE_CHANNELS)))
    sequence = ModelPriceVolumeSequence(quote.code, dates, values, "published-history")
    feature = replace(feature, quote=quote, model_sequence=sequence if profile == "v5" else None)
    predictor = _RecordingPredictor()
    capability = TomorrowProbabilityCapability(
        {"v1": V1_TOMORROW_PLAN, "v2": V2_TOMORROW_PLAN, "v3": V3_TOMORROW_PLAN, "v5": V5_TOMORROW_PLAN}[profile], predictor,
    )

    assert capability.history_required_sessions == (61 if profile == "v5" else 20)
    assert capability.is_input_eligible(feature)
    assert capability.score((feature,)).missing_codes == ()
    row = predictor.batches[0][0]
    assert row[0] == pytest.approx(0.1)
    if profile == "v5":
        assert len(row) == len(TOMORROW_SEQUENCE_FEATURE_NAMES) == 366
        assert row[len(TOMORROW_FEATURE_NAMES):] == sequence.values
    else:
        assert len(row) == len(TOMORROW_FEATURE_NAMES) == 6


def test_v5_tomorrow_keeps_incomplete_or_wrong_session_sequences_unready(application_feature_factory):
    feature = application_feature_factory("600001", EVALUATED_AT)
    quote = replace(feature.quote, open_price=10.0, high=12.0, low=9.0, price=11.0, volume=2_000_000.0)
    dates = tuple(EVALUATED_AT.date() - timedelta(days=61 - index) for index in range(61))
    values = (0.1,) * (SEQUENCE_STEPS * len(SEQUENCE_CHANNELS))
    sequence = ModelPriceVolumeSequence(quote.code, dates, values, "published-history")
    started = EVALUATED_AT + timedelta(days=2)
    preview = PreviewContext("tomorrow-preview", EVALUATED_AT.date(), started)
    feature = replace(feature, quote=quote, observed_at=started, preview_context=preview)
    predictor = _RecordingPredictor()
    capability = TomorrowProbabilityCapability(V5_TOMORROW_PLAN, predictor)

    assert capability.is_input_eligible(replace(feature, model_sequence=sequence))
    invalid_inputs = (
        feature,
        replace(feature, model_sequence=replace(sequence, code="600002")),
        replace(feature, model_sequence=replace(sequence, dates=(*dates[:-1], preview.market_trade_date))),
        replace(feature, model_sequence=sequence, model_history_quality=ModelHistoryQuality("history_model_date_mismatch")),
        replace(feature, model_sequence=sequence, quote=replace(quote, source_time=started)),
    )
    for invalid in invalid_inputs:
        assert not capability.is_input_eligible(invalid)
        assert capability.score((invalid,)).missing_codes == (quote.code,)
    assert predictor.batches == []


@pytest.mark.parametrize(("budget_seconds", "expected_batch_sizes"), ((0.0, ()), (1.0, (256,)), (3.0, (256, 1))))
def test_v5_tomorrow_bounds_native_batches_and_discards_expired_results(
    budget_seconds, expected_batch_sizes, application_feature_factory,
):
    current_time = 0.0

    class _TimedPredictor(_RecordingPredictor):
        def predict_probabilities(self, rows: tuple[tuple[float, ...], ...]) -> tuple[float, ...]:
            nonlocal current_time
            current_time += 1.0
            return super().predict_probabilities(rows)

    feature = application_feature_factory("600001", EVALUATED_AT)
    quote = replace(feature.quote, open_price=10.0, high=12.0, low=9.0, price=11.0, volume=2_000_000.0)
    dates = tuple(EVALUATED_AT.date() - timedelta(days=61 - index) for index in range(61))
    values = (0.1,) * (SEQUENCE_STEPS * len(SEQUENCE_CHANNELS))
    features = tuple(
        replace(
            feature,
            quote=replace(quote, code=code),
            model_sequence=ModelPriceVolumeSequence(code, dates, values, "published-history"),
        )
        for code in (f"{600000 + index}" for index in range(257))
    )
    predictor = _TimedPredictor()
    capability = TomorrowProbabilityCapability(V5_TOMORROW_PLAN, predictor, monotonic=lambda: current_time)
    context = ModelScoringContext(time_budget_seconds=budget_seconds)

    if budget_seconds < 2.0:
        with pytest.raises(ModelScoringDeadlineError, match="tomorrow_v5_model_scoring_deadline"):
            capability.score(features, context=context)
    else:
        assert len(capability.score(features, context=context).diagnostics) == len(features)
    assert tuple(len(batch) for batch in predictor.batches) == expected_batch_sizes
