"""Execution-cost history keeps verified session positions and missing facts."""

from dataclasses import replace
from datetime import date, timedelta

import pytest

from trader.download.domain.published_history import PublishedHistoryCell, PublishedHistorySide, PublishedHistoryWindow
from trader.infra.market_data.history.history import HistorySessionKind, SummarizeHistoryMetrics
from trader.recommendation.infra.market_data.aligned_history_bars import BuildAlignedHistoryBars
from trader.recommendation.infra.market_data.published_history_bars import QfqBars
from trader.recommendation.infra.normalization.history_price_basis import RebaseHistoryPrices


def _published_window() -> tuple[PublishedHistoryWindow, tuple[date, ...]]:
    days = tuple(
        day for offset in range(29)
        if (day := date(2026, 6, 15) + timedelta(days=offset)).weekday() < 5
    )
    cells = []
    price = 10.0
    for index, day in enumerate(days):
        suspended = 1 <= index <= 10
        previous = price
        if index > 10:
            price *= 1.01
        raw = PublishedHistorySide(
            "600001",
            day,
            "unadjusted",
            price,
            price,
            price,
            price,
            0.0 if suspended else 20_000_000.0,
            0.0 if suspended else 200_000_000.0,
            previous,
            (price / previous - 1.0) * 100.0,
            0.0 if suspended else 2.0,
            "suspended" if suspended else "trading",
        )
        qfq = replace(raw, adjustment="qfq", preclose=None, pct_change=None, turnover=None)
        cells.append(PublishedHistoryCell(
            raw.code,
            day,
            "supplier_marked_suspended" if suspended else "complete",
            raw,
            qfq,
        ))
    return PublishedHistoryWindow("600001", tuple(cells)), days


def test_confirmed_suspensions_keep_twenty_session_amihud_denominator() -> None:
    window, days = _published_window()
    aligned = BuildAlignedHistoryBars(window, days)
    assert len(aligned.bars) == 21
    assert len(aligned.suspended_dates) == 10 and not aligned.estimated_dates
    for bars in (aligned.bars, QfqBars(window)):
        assert sum(bar.session_kind is HistorySessionKind.SUSPENDED for bar in bars) == 10
        profile = SummarizeHistoryMetrics(bars)
        assert profile.amihud_20d == pytest.approx(2.5e-5)
        assert profile.average_amount_20d == profile.median_amount_20d == 100_000_000.0
        rebased, _ = RebaseHistoryPrices(bars, None, 2.0)
        assert tuple(bar.session_kind for bar in rebased) == tuple(bar.session_kind for bar in bars)
        assert SummarizeHistoryMetrics(rebased).amihud_20d == profile.amihud_20d


@pytest.mark.parametrize("status", ["unknown_missing", "tencent_inferred_suspended"])
def test_unknown_history_is_not_promoted_to_a_confirmed_zero_flow_session(status) -> None:
    window, days = _published_window()
    unknown = PublishedHistoryCell("600001", days[5], status, None, None)
    window = replace(window, cells=(*window.cells[:5], unknown, *window.cells[6:]))
    aligned = BuildAlignedHistoryBars(window, days)
    assert len(aligned.bars) == 21 and days[5] in aligned.estimated_dates
    assert aligned.bars[5].session_kind is HistorySessionKind.ESTIMATED
    assert SummarizeHistoryMetrics(aligned.bars).amihud_20d is None


@pytest.mark.parametrize(
    ("amount", "pct_change", "kind"),
    [
        (-1.0, 0.0, HistorySessionKind.TRADING),
        (float("nan"), 0.0, HistorySessionKind.TRADING),
        (float("inf"), 0.0, HistorySessionKind.TRADING),
        (200_000_000.0, float("nan"), HistorySessionKind.TRADING),
        (200_000_000.0, float("inf"), HistorySessionKind.TRADING),
        (0.0, 0.0, HistorySessionKind.TRADING),
        (0.0, 1.0, HistorySessionKind.SUSPENDED),
        (200_000_000.0, 0.0, HistorySessionKind.SUSPENDED),
    ],
)
def test_invalid_cost_history_never_uses_a_shorter_average(amount, pct_change, kind) -> None:
    window, days = _published_window()
    bars = BuildAlignedHistoryBars(window, days).bars
    invalid = replace(bars[-1], amount=amount, pct_change=pct_change, session_kind=kind)
    assert SummarizeHistoryMetrics((*bars[:-1], invalid)).amihud_20d is None


def test_cost_history_requires_twenty_positions_but_keeps_confirmed_all_zero_window() -> None:
    window, days = _published_window()
    bars = BuildAlignedHistoryBars(window, days).bars
    assert SummarizeHistoryMetrics(bars[-19:]).amihud_20d is None
    suspended = tuple(replace(
        bar,
        amount=0.0,
        pct_change=0.0,
        session_kind=HistorySessionKind.SUSPENDED,
    ) for bar in bars[-20:])
    assert SummarizeHistoryMetrics(suspended).amihud_20d == 0.0
