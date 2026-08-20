from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta

from trader.application.research.historical_screening import (
    HistoricalDownloadService,
    HistoricalSecurity,
)
from trader.domain.research.historical_screening import SCORE_H0_V1_SPEC, HistoricalPriceBar


def _bar(code: str) -> HistoricalPriceBar:
    return HistoricalPriceBar(
        trade_date=__import__("datetime").date(2026, 8, 19),
        open_price=10.0,
        close=10.5,
        high=10.8,
        low=9.9,
        volume=1000.0,
        amount=10000.0,
        pct_change=5.0,
        turnover_rate=None,
        adjustment="qfq",
        source=f"fake:{code}",
    )


def _bars(code: str) -> tuple[HistoricalPriceBar, ...]:
    last = _bar(code)
    required = SCORE_H0_V1_SPEC.minimum_history_sessions + SCORE_H0_V1_SPEC.label_horizon_sessions
    return tuple(
        replace(last, trade_date=last.trade_date - timedelta(days=offset)) for offset in reversed(range(required))
    )


class _Universe:
    def fetch(self):
        return (
            HistoricalSecurity("600001", "main", "甲", False, False),
            HistoricalSecurity("300001", "chinext", "乙", False, False),
            HistoricalSecurity("688001", "star", "丙", True, False),
            HistoricalSecurity("900001", "unsupported", "丁", False, False),
        )


class _History:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def fetch_history(self, code: str, *, days: int):
        self.calls.append((code, days))
        if code == "300001":
            raise TimeoutError("secret provider payload")
        return _bars(code)


@dataclass
class _Archive:
    complete: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        self.universe: tuple[HistoricalSecurity, ...] = ()
        self.saved: dict[str, tuple[HistoricalPriceBar, ...]] = {}
        self.failures: dict[str, str] = {}

    def register_universe(self, _spec, universe):
        self.universe = tuple(universe)

    def registered_universe(self, _identity):
        return self.universe

    def completed_codes(self, _identity):
        return self.complete

    def save_history(self, _spec, code, bars):
        self.saved[code] = tuple(bars)

    def record_failure(self, _spec, code, error_code):
        self.failures[code] = error_code


def test_history_download_is_bounded_resumable_and_does_not_persist_exception_text() -> None:
    history = _History()
    archive = _Archive(complete=frozenset({"600001"}))
    progress: list[tuple[int, int, str]] = []
    service = HistoricalDownloadService(_Universe(), history, archive, workers=2)

    result = service.execute(SCORE_H0_V1_SPEC, progress=lambda done, total, code: progress.append((done, total, code)))

    assert archive.universe == (
        HistoricalSecurity("300001", "chinext", "乙", False, False),
        HistoricalSecurity("600001", "main", "甲", False, False),
    )
    assert history.calls == [("300001", 640)]
    assert archive.saved == {}
    assert archive.failures == {"300001": "timeout"}
    assert result.universe_count == 2
    assert result.previously_completed == 1
    assert result.downloaded == 0
    assert result.failed == 1
    assert progress == [(1, 1, "300001")]


def test_history_download_rejects_non_qfq_or_dates_after_the_fixed_cutoff() -> None:
    class InvalidHistory:
        @staticmethod
        def fetch_history(_code: str, *, days: int):
            del days
            return (
                HistoricalPriceBar(
                    trade_date=__import__("datetime").date(2026, 8, 20),
                    open_price=10.0,
                    close=10.0,
                    high=10.0,
                    low=10.0,
                    volume=1.0,
                    amount=1.0,
                    pct_change=0.0,
                    turnover_rate=None,
                    adjustment="qfq",
                    source="raw",
                ),
            )

    archive = _Archive()
    service = HistoricalDownloadService(_Universe(), InvalidHistory(), archive, workers=1)

    result = service.execute(SCORE_H0_V1_SPEC)

    assert result.failed == 2
    assert set(archive.failures.values()) == {"invalid_history"}


def test_history_download_does_not_mark_short_history_as_complete() -> None:
    archive = _Archive()

    class ShortHistory:
        @staticmethod
        def fetch_history(code: str, *, days: int):
            del days
            return (_bar(code),)

    result = HistoricalDownloadService(_Universe(), ShortHistory(), archive, workers=1).execute(SCORE_H0_V1_SPEC)

    assert result.downloaded == 0
    assert result.failed == 2
    assert archive.saved == {}
    assert set(archive.failures.values()) == {"invalid_history"}


def test_history_download_reuses_the_frozen_universe_on_resume() -> None:
    archive = _Archive()
    archive.universe = (HistoricalSecurity("600001", "main", "冻结名称", False, False),)

    class ChangingUniverse:
        @staticmethod
        def fetch():
            raise AssertionError("frozen universe must not be fetched again")

    service = HistoricalDownloadService(ChangingUniverse(), _History(), archive, workers=1)

    result = service.execute(SCORE_H0_V1_SPEC)

    assert result.downloaded == 1
    assert set(archive.saved) == {"600001"}
