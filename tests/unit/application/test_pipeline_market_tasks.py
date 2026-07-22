from types import SimpleNamespace

from trader.application import pipeline_market_tasks
from trader.application.schedule import MarketPhase


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
