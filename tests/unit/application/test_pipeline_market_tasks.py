from types import SimpleNamespace

import pytest

from trader.application import pipeline_market_tasks
from trader.application.pipeline_stages import strategies_for_phase, strategy_requires_scoring
from trader.application.schedule import MarketPhase
from trader.application.snapshot_workflow import score_strategy
from trader.domain.recommendation.models import Strategy


def test_realtime_candidate_quote_event_does_not_wait_for_intraday_history(monkeypatch, utc_now) -> None:
    pipeline = SimpleNamespace(
        _candidate_codes=("600001",),
        _long_codes=(),
        _candidate_features=(),
        _quotes=SimpleNamespace(refresh_candidate_quotes=object()),
    )
    slow_calls: list[object] = []
    monkeypatch.setattr(pipeline_market_tasks, "_run_urgent_market_data_task", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(
        pipeline_market_tasks,
        "_run_market_data_task",
        lambda *_args, **_kwargs: slow_calls.append(object()),
    )

    pipeline_market_tasks._refresh_candidate_quotes_on_workers(
        pipeline,
        utc_now,
        MarketPhase.AFTERNOON,
        deadline=None,
    )

    assert slow_calls == []


def test_realtime_candidate_quote_event_excludes_long_codes(monkeypatch, utc_now) -> None:
    pipeline = SimpleNamespace(
        _candidate_codes=("600001",),
        _long_codes=("688012", "300346"),
        _candidate_features=(),
        _quotes=SimpleNamespace(refresh_candidate_quotes=object()),
    )
    requested_codes: list[tuple[str, ...]] = []

    def run_urgent(_pipeline, _function, codes, *_args, **_kwargs):
        requested_codes.append(tuple(codes))
        return tuple(SimpleNamespace(quote=SimpleNamespace(code=code)) for code in codes)

    monkeypatch.setattr(pipeline_market_tasks, "_run_urgent_market_data_task", run_urgent)

    pipeline_market_tasks._refresh_candidate_quotes_on_workers(
        pipeline,
        utc_now,
        MarketPhase.AFTERNOON,
        deadline=None,
    )

    assert requested_codes == [("600001",)]
    assert tuple(feature.quote.code for feature in pipeline._candidate_features) == ("600001",)


def test_candidate_research_and_reference_inputs_exclude_long_codes() -> None:
    pipeline = SimpleNamespace(
        _candidate_codes=("600001",),
        _long_codes=("688012", "300346"),
    )

    assert pipeline_market_tasks._active_codes(pipeline) == ("600001",)


def test_long_cannot_enter_strategy_scoring(utc_now) -> None:
    with pytest.raises(ValueError, match="quote projection lane"):
        score_strategy(
            SimpleNamespace(),
            Strategy.LONG,
            utc_now,
            MarketPhase.AFTERNOON,
            "2026-07-16",
        )


def test_pre_afternoon_candidate_and_scoring_phases_include_tomorrow_and_d25() -> None:
    expected = (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25)

    assert pipeline_market_tasks._short_strategies_for_phase(MarketPhase.TODAY_MAIN) == expected
    assert strategies_for_phase(MarketPhase.TODAY_MAIN) == expected
    assert strategies_for_phase(MarketPhase.MIDDAY) == (
        Strategy.TOMORROW,
        Strategy.D25,
    )
    assert strategies_for_phase(MarketPhase.AFTERNOON) == (
        Strategy.TOMORROW,
        Strategy.D25,
    )


def test_midday_scoring_only_recovers_a_missing_current_trade_date() -> None:
    state = SimpleNamespace(latest=lambda _strategy: SimpleNamespace(trade_date="2026-07-27"))
    pipeline = SimpleNamespace(_state=state)

    assert not strategy_requires_scoring(
        pipeline,
        Strategy.TOMORROW,
        MarketPhase.MIDDAY,
        "2026-07-27",
    )
    assert strategy_requires_scoring(
        pipeline,
        Strategy.TOMORROW,
        MarketPhase.MIDDAY,
        "2026-07-28",
    )
    assert strategy_requires_scoring(
        pipeline,
        Strategy.TOMORROW,
        MarketPhase.AFTERNOON,
        "2026-07-27",
    )
