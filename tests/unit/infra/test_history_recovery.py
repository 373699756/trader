from __future__ import annotations

from datetime import datetime, timedelta, timezone

from trader.infra.market_data.history.history import DailyBar, PriceAdjustment
from trader.recommendation.infra.market_data.history_recovery import HistoryRecovery
from trader.recommendation.infra.market_data.published_history_cache import PublishedHistoryCache


NOW = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)


def _bars(count: int, *, adjustment: PriceAdjustment = PriceAdjustment.QFQ) -> tuple[DailyBar, ...]:
    return tuple(
        DailyBar(
            trade_date=f"2026-07-{index + 1:02d}",
            open_price=10.0,
            close=10.0,
            high=10.5,
            low=9.5,
            volume=1_000.0,
            amount=100_000_000.0,
            pct_change=0.0,
            adjustment=adjustment,
            source="fixture",
        )
        for index in range(count)
    )


class _Source:
    def __init__(self, values: dict[str, tuple[DailyBar, ...]]) -> None:
        self.values = values
        self.calls: list[str] = []

    def fetch_history(self, code: str, *, days: int = 90) -> tuple[DailyBar, ...]:
        self.calls.append(code)
        return self.values.get(code, ())[-days:]


class _UnavailableArchive:
    def manifest(self):
        return None


def test_recovery_prefers_primary_and_returns_full_qfq_window() -> None:
    primary = _Source({"600001": _bars(61)})
    fallback = _Source({"600001": _bars(80)})
    recovery = HistoryRecovery(primary, fallback, worker_pool=None, workers=2, wall_clock=lambda: NOW)

    result = recovery.recover(("600001",), days=61, deadline=NOW + timedelta(seconds=5))

    assert len(result["600001"]) == 61
    assert primary.calls == ["600001"]
    assert fallback.calls == []
    assert recovery.status().success_count == 1


def test_recovery_falls_back_when_primary_has_insufficient_history() -> None:
    primary = _Source({"600001": _bars(19)})
    fallback = _Source({"600001": _bars(61)})
    recovery = HistoryRecovery(primary, fallback, worker_pool=None, workers=2, wall_clock=lambda: NOW)

    result = recovery.recover(("600001",), days=61, deadline=NOW + timedelta(seconds=5))

    assert len(result["600001"]) == 61
    assert primary.calls == ["600001"]
    assert fallback.calls == ["600001"]


def test_recovery_rejects_raw_bars_and_does_not_fabricate_history() -> None:
    primary = _Source({"600001": _bars(61, adjustment=PriceAdjustment.RAW)})
    fallback = _Source({"600001": ()})
    recovery = HistoryRecovery(primary, fallback, worker_pool=None, workers=1, wall_clock=lambda: NOW)

    result = recovery.recover(("600001",), days=61, deadline=NOW + timedelta(seconds=5))

    assert result == {}
    assert recovery.status().failure_count == 1


def test_published_history_uses_recovery_when_archive_has_no_snapshot() -> None:
    primary = _Source({"600001": _bars(61)})
    recovery = HistoryRecovery(primary, _Source({}), worker_pool=None, workers=1, wall_clock=lambda: NOW)
    history = PublishedHistoryCache(_UnavailableArchive(), lookback_sessions=61, recovery=recovery)
    restrictions: dict[str, set[str]] = {}

    loaded = history.load(("600001",), deadline=NOW + timedelta(seconds=5), action_restrictions=restrictions)

    assert len(loaded["600001"]) == 61
    assert restrictions == {}
    assert history.summaries(loaded, NOW)["600001"].profile.median_amount_20d == 100_000_000.0
