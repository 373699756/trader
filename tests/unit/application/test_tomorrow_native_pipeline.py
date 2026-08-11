from concurrent.futures import Future
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

from trader.application import pipeline_stages
from trader.application.pipeline_review_updates import ScoringContext
from trader.application.ports.tomorrow import TodayNativeInput
from trader.application.schedule import MarketPhase
from trader.domain.recommendation.models import Strategy


def test_today_v2_owned_strategy_receives_native_input_without_v1_prepare(
    application_feature_factory,
    utc_now,
) -> None:
    feature = application_feature_factory("600001", utc_now)
    strategy_data: Future[tuple[tuple[object, ...], str]] = Future()
    strategy_data.set_result(((feature,), "candidate-data-v1"))
    offered: list[TodayNativeInput] = []

    class Sink:
        def offer_native(self, native_input):
            offered.append(native_input)
            return True

    pipeline = SimpleNamespace(
        _today_native_inputs=Sink(),
        _tomorrow_native_inputs=None,
        _market_features=(feature,),
        _config_version="runtime:test",
        _candidate_pool_size=120,
        _now=lambda: utc_now,
        _state=SimpleNamespace(increment=lambda *_args: None, record_error=lambda *_args: None),
        _v2_owned_strategies=frozenset({Strategy.TODAY}),
    )
    context = ScoringContext(
        now=utc_now,
        phase=MarketPhase.TODAY_MAIN,
        trade_date=utc_now.date().isoformat(),
        started_at=0.0,
        completion_deadline=None,
    )

    result = pipeline_stages._prepare_strategy_futures(
        pipeline,
        context,
        [(Strategy.TODAY, ("600001",), strategy_data)],
    )

    assert result == []
    assert len(offered) == 1 and offered[0].strategy is Strategy.TODAY


def test_native_input_is_offered_before_v1_prepare_submission(
    application_feature_factory,
    utc_now,
    monkeypatch,
) -> None:
    feature = application_feature_factory("600001", utc_now)
    strategy_data: Future[tuple[tuple[object, ...], str]] = Future()
    strategy_data.set_result(((feature,), "candidate-data-v1"))
    offered = []

    class Sink:
        def offer_native(self, native_input):
            offered.append(native_input)
            return True

    submitted_after_offer: list[int] = []

    def submit_required(*_args, **_kwargs):
        submitted_after_offer.append(len(offered))
        return Future()

    monkeypatch.setattr(pipeline_stages, "submit_required", submit_required)
    pipeline = SimpleNamespace(
        _tomorrow_native_inputs=Sink(),
        _market_features=(feature,),
        _config_version="runtime:test",
        _candidate_pool_size=120,
        _now=lambda: utc_now,
        _state=SimpleNamespace(increment=lambda *_args: None, record_error=lambda *_args: None),
        _strategy_pool=object(),
        _engine=SimpleNamespace(prepare_snapshot=lambda *_args, **_kwargs: None),
        _filtered_count=0,
        _filter_reasons={},
        _filter_details=(),
    )
    context = ScoringContext(
        now=utc_now,
        phase=MarketPhase.AFTERNOON,
        trade_date=utc_now.date().isoformat(),
        started_at=0.0,
        completion_deadline=None,
    )

    result = pipeline_stages._prepare_strategy_futures(
        pipeline,
        context,
        [(Strategy.TOMORROW, ("600001",), strategy_data)],
    )

    assert len(result) == 1
    assert len(offered) == 1
    assert offered[0].candidate_features == (feature,)
    assert submitted_after_offer == [1]


def test_native_input_failure_does_not_block_v1_prepare_submission(
    application_feature_factory,
    utc_now,
    monkeypatch,
) -> None:
    feature = application_feature_factory("600001", utc_now)
    strategy_data: Future[tuple[tuple[object, ...], str]] = Future()
    strategy_data.set_result(((feature,), "candidate-data-v1"))

    class FailingSink:
        @staticmethod
        def offer_native(_native_input):
            raise RuntimeError("shadow unavailable")

    submitted: list[bool] = []
    counters: list[str] = []

    def submit_required(*_args, **_kwargs):
        submitted.append(True)
        return Future()

    monkeypatch.setattr(pipeline_stages, "submit_required", submit_required)
    pipeline = SimpleNamespace(
        _tomorrow_native_inputs=FailingSink(),
        _market_features=(feature,),
        _config_version="runtime:test",
        _candidate_pool_size=120,
        _now=lambda: utc_now,
        _state=SimpleNamespace(
            increment=lambda name: counters.append(name),
            record_error=lambda *_args: None,
        ),
        _strategy_pool=object(),
        _engine=SimpleNamespace(prepare_snapshot=lambda *_args, **_kwargs: None),
        _filtered_count=0,
        _filter_reasons={},
        _filter_details=(),
    )
    context = ScoringContext(
        now=utc_now,
        phase=MarketPhase.AFTERNOON,
        trade_date=utc_now.date().isoformat(),
        started_at=0.0,
        completion_deadline=None,
    )

    result = pipeline_stages._prepare_strategy_futures(
        pipeline,
        context,
        [(Strategy.TOMORROW, ("600001",), strategy_data)],
    )

    assert len(result) == 1
    assert submitted == [True]
    assert counters == ["tomorrow_native_inputs_failed"]


def test_native_and_v1_share_completion_watermark_when_candidate_finishes_after_cycle_start(
    application_feature_factory,
    utc_now,
    monkeypatch,
) -> None:
    completed_at = utc_now + timedelta(seconds=3)
    feature = application_feature_factory("600001", utc_now)
    completed_feature = replace(
        feature,
        observed_at=completed_at,
        quote=replace(
            feature.quote,
            source_time=completed_at,
            received_time=completed_at,
        ),
    )
    strategy_data: Future[tuple[tuple[object, ...], str]] = Future()
    strategy_data.set_result(((completed_feature,), "candidate-data-v2"))
    offered = []
    submitted_options: list[dict[str, object]] = []

    class Sink:
        def offer_native(self, native_input):
            offered.append(native_input)
            return True

    def submit_required(*_args, **kwargs):
        submitted_options.append(kwargs)
        return Future()

    monkeypatch.setattr(pipeline_stages, "submit_required", submit_required)
    pipeline = SimpleNamespace(
        _tomorrow_native_inputs=Sink(),
        _market_features=(feature,),
        _config_version="runtime:test",
        _candidate_pool_size=120,
        _now=lambda: completed_at,
        _state=SimpleNamespace(increment=lambda *_args: None, record_error=lambda *_args: None),
        _strategy_pool=object(),
        _engine=SimpleNamespace(prepare_snapshot=lambda *_args, **_kwargs: None),
        _filtered_count=0,
        _filter_reasons={},
        _filter_details=(),
    )
    context = ScoringContext(
        now=utc_now,
        phase=MarketPhase.AFTERNOON,
        trade_date=utc_now.date().isoformat(),
        started_at=0.0,
        completion_deadline=None,
    )

    result = pipeline_stages._prepare_strategy_futures(
        pipeline,
        context,
        [(Strategy.TOMORROW, ("600001",), strategy_data)],
    )

    assert len(result) == 1
    assert len(offered) == 1
    assert offered[0].evaluated_at == completed_at
    assert submitted_options[0]["now"] == completed_at
    assert tuple(submitted_options[0]["market_features"]) == (feature,)
