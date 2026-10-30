from dataclasses import replace
from datetime import timedelta

from tests.component.test_candidate_history_tail import CALENDAR, NOW, HistoryInput
from trader.download.application.read_published_history import ReadPublishedHistoryUseCase
from trader.recommendation.infra.market_data.published_history_cache import PublishedHistoryCache


def test_completed_v1_inputs_are_read_once_and_invalid_stock_does_not_block_the_population(monkeypatch) -> None:
    class History(HistoryInput):
        @staticmethod
        def window(code, dates):
            window = HistoryInput.window(code, dates)
            if code == "600002":
                cell = window.cells[-1]
                assert cell.qfq is not None
                window = replace(window, cells=(*window.cells[:-1], replace(cell, qfq=None, status="qfq_missing")))
            return window

    source = History(251, 0, ("600001", "600002"))
    history = PublishedHistoryCache(
        ReadPublishedHistoryUseCase(source),
        lookback_sessions=251,
        monotonic=lambda: 0.0,
        open_dates=lambda: CALENDAR,
        learned_rank_inputs=True,
    )
    history.initialize(cancelled=lambda: False)
    assert history.status().state == "published"
    assert len(history.entries()) == 1
    assert history.model_history_quality("600002", NOW).failure == "history_invalid_prices"
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


def test_signal_day_publication_keeps_previous_251_day_v1_input():
    from trader.recommendation.domain.market.v1.features import CalculateLearnedRankFeatures, LearnedRankDailyPoint

    source = HistoryInput(252, 0, ("600001",))
    source.current = replace(
        source.current, data_cutoff=NOW.date(), calendar_dates=tuple(day for day in CALENDAR if day <= NOW.date())
    )
    history = PublishedHistoryCache(
        ReadPublishedHistoryUseCase(source),
        lookback_sessions=251,
        monotonic=lambda: 0.0,
        open_dates=lambda: CALENDAR,
        learned_rank_inputs=True,
    )
    history.initialize(cancelled=lambda: False)
    actual = history.completed_model_features(("600001",), NOW.replace(hour=20))
    assert history.status().maintenance_completed_units == 1
    expected_days = tuple(day for day in CALENDAR if day < NOW.date())[-251:]
    cells = source.window("600001", expected_days).cells
    expected = CalculateLearnedRankFeatures(
        tuple(LearnedRankDailyPoint(c.trade_date, c.qfq.close_price, c.qfq.amount) for c in cells)
    )
    assert tuple(value for _, value in actual["600001"]) == expected


def test_leading_absence_uses_earlier_valuation_without_compressing_calendar():
    from trader.download.domain.published_history import PublishedHistoryCell

    dates = tuple(day for day in CALENDAR if day < NOW.date())
    missing = dates[-252:-248]

    class History(HistoryInput):
        read_ranges = []

        def read_range(self, manifest, codes, start, end, *, check=None):
            self.read_ranges.append((start, end))
            return super().read_range(manifest, codes, start, end, check=check)

        @staticmethod
        def window(code, selected):
            result = HistoryInput.window(code, selected)
            return replace(
                result,
                cells=tuple(
                    PublishedHistoryCell(code, cell.trade_date, "tencent_inferred_suspended", None, None)
                    if cell.trade_date in missing
                    else cell
                    for cell in result.cells
                ),
            )

    source = History(252, 0, ("600001",))
    history = PublishedHistoryCache(
        ReadPublishedHistoryUseCase(source),
        lookback_sessions=251,
        monotonic=lambda: 0.0,
        open_dates=lambda: CALENDAR,
        learned_rank_inputs=True,
    )
    history.initialize(cancelled=lambda: False)
    assert source.reads == [("600001",)]
    assert source.read_ranges == [(dates[-315], dates[-253])]
    assert len(history.completed_model_features(("600001",), NOW)["600001"]) == 10
    quality = history.model_history_quality("600001", NOW)
    assert quality.failure is None and quality.estimated_sessions == 4
