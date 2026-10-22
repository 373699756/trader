from dataclasses import replace
from datetime import timedelta

from tests.component.test_candidate_history_tail import CALENDAR, NOW, _Archive
from trader.download.application.read_published_history import ReadPublishedHistoryUseCase
from trader.recommendation.infra.market_data.published_history_cache import PublishedHistoryCache


def test_completed_v1_inputs_are_read_once_and_invalid_stock_does_not_block_the_population(monkeypatch) -> None:
    class History(_Archive):
        @staticmethod
        def window(code, dates):
            window = _Archive.window(code, dates)
            if code == "600002":
                cell = window.cells[-1]
                assert cell.qfq is not None
                window = replace(window, cells=(*window.cells[:-1], replace(cell, qfq=replace(cell.qfq, amount=0.0))))
            return window

    source = History(251, 0, ("600001", "600002"))
    history = PublishedHistoryCache(
        ReadPublishedHistoryUseCase(source),
        lookback_sessions=251,
        open_dates=lambda: CALENDAR,
        learned_rank_inputs=True,
    )
    history.initialize(cancelled=lambda: False)
    assert history.status().state == "active"
    assert len(history.entries()) == 2
    monkeypatch.setattr(
        source,
        "iter_windows",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("runtime reread history")),
    )
    values = history.completed_model_features(("600001", "600002"), NOW)
    assert tuple(values) == ("600001",)
    assert len(values["600001"]) == 10
    assert history.completed_model_features(("600001",), NOW) == values
    # Once a new market session becomes due, the pinned old input cannot impersonate it.
    assert history.completed_model_features(("600001",), NOW + timedelta(days=1)) == {}
