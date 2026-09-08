from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from trader.application.outcomes.outcome_settlement import OutcomeSettlementAdapter, OutcomeSettlementService
from trader.domain.outcome.models import (
    BenchmarkReturn,
    OutcomeBar,
    OutcomePrice,
    OutcomeTarget,
    OutcomeTradingStatus,
)
from trader.domain.recommendation.models import Strategy

NOW = datetime(2026, 7, 21, 15, 10, tzinfo=ZoneInfo("Asia/Shanghai"))


def _bar(trade_date: str, open_price: float, high: float, low: float, close: float) -> OutcomeBar:
    prices = OutcomePrice(open_price, high, low, close)
    return OutcomeBar(trade_date, prices, prices, OutcomeTradingStatus.TRADABLE, "fixture")


class _MarketData:
    def __init__(self, features=(), *, bars=None) -> None:
        self.features = tuple(features)
        self.bars = bars
        self.fetch_calls = []

    def fetch_market_features(self, observed_at, *, force=False):
        self.fetch_calls.append((observed_at, force))
        return self.features

    def read_outcome_bars(self, codes, observed_at):
        assert tuple(codes) == ("600001",)
        if self.bars is not None:
            return {"600001": self.bars}
        assert observed_at == NOW
        return {
            "600001": (
                _bar("2026-07-20", 10.0, 10.1, 9.9, 10.0),
                _bar("2026-07-21", 10.0, 10.3, 9.6, 10.2),
            ),
        }


class _Repository:
    def __init__(self) -> None:
        self.saved = ()
        self.benchmark = ()

    def pending_outcome_targets(self, *, limit):
        assert limit == 500
        return (OutcomeTarget("snapshot", Strategy.TOMORROW, "2026-07-20", "600001", 10.0, 2.0),)

    def record_benchmark_return(self, benchmark, *, observed_at):
        self.benchmark = (benchmark, observed_at)

    def benchmark_returns_after(self, recommend_date, *, limit):
        assert recommend_date == "2026-07-20"
        assert limit == 1
        return (BenchmarkReturn("2026-07-21", 0.5),)

    def save_recommendation_outcomes(self, outcomes):
        self.saved = tuple(outcomes)


def _service(market_data: _MarketData, repository: _Repository, *, elapsed: int = 1):
    return OutcomeSettlementService(
        market_data,
        repository,
        repository,
        session_distance=lambda _start, _end: elapsed,
    )


def test_after_close_settlement_records_benchmark_and_due_outcome(application_feature_factory) -> None:
    repository = _Repository()
    feature = application_feature_factory("600001", NOW)
    peer = application_feature_factory("600002", NOW)
    market_data = _MarketData()

    result = _service(market_data, repository).settle(
        NOW,
        (feature, replace(peer, quote=replace(peer.quote, pct_change=1.0))),
    )

    assert result.target_count == 1
    assert result.completed_count == 1
    assert repository.benchmark == (BenchmarkReturn("2026-07-21", 2.0), NOW)
    assert len(repository.saved) == 1
    assert repository.saved[0].net_excess_return_pct == pytest.approx(1.3)


def test_settlement_skips_horizons_that_are_not_due(application_feature_factory) -> None:
    repository = _Repository()
    market_data = _MarketData()

    result = _service(market_data, repository, elapsed=0).settle(
        NOW,
        (application_feature_factory("600001", NOW),),
    )

    assert result.completed_count == 0
    assert repository.saved == ()


def test_settlement_records_due_stock_outcome_when_benchmark_is_unavailable(
    application_feature_factory,
) -> None:
    repository = _Repository()
    repository.benchmark_returns_after = lambda _date, *, limit: ()
    stale = application_feature_factory(
        "600001",
        datetime(2026, 7, 21, 14, 59, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    result = _service(_MarketData(), repository).settle(NOW, (stale,))

    assert result.benchmark_recorded is False
    assert result.outcome_count == 1
    assert repository.saved[0].status == "benchmark_missing"
    assert repository.saved[0].gross_return_pct == pytest.approx(2.0)


def test_equal_weight_benchmark_requires_every_market_return(application_feature_factory) -> None:
    repository = _Repository()
    first = application_feature_factory("600001", NOW)
    second = application_feature_factory("600002", NOW)
    incomplete = replace(second, quote=replace(second.quote, pct_change=None))

    result = _service(_MarketData(), repository).settle(NOW, (first, incomplete))

    assert result.benchmark_recorded is False
    assert repository.benchmark == ()


def test_adapter_fetches_fresh_close_market_before_settlement(application_feature_factory) -> None:
    feature = application_feature_factory("600001", NOW)
    market_data = _MarketData((feature,))
    repository = _Repository()
    adapter = OutcomeSettlementAdapter(market_data, _service(market_data, repository))

    adapter.settle(NOW)

    assert market_data.fetch_calls == [(NOW, True)]
    assert repository.benchmark == (BenchmarkReturn("2026-07-21", 3.0), NOW)


def test_d25_settlement_includes_t4_but_only_evaluates_pending_horizons(application_feature_factory) -> None:
    class _D25Repository(_Repository):
        def pending_outcome_targets(self, *, limit):
            assert limit == 500
            return (
                OutcomeTarget(
                    "snapshot",
                    Strategy.D25,
                    "2026-07-20",
                    "600001",
                    10.0,
                    2.0,
                    pending_horizons=(4,),
                ),
            )

        def benchmark_returns_after(self, recommend_date, *, limit):
            assert recommend_date == "2026-07-20"
            assert limit == 4
            return tuple(BenchmarkReturn(f"2026-07-{20 + offset:02d}", 0.0) for offset in range(1, 5))

    repository = _D25Repository()
    bars = tuple(_bar(f"2026-07-{20 + offset:02d}", 10.0, 10.5, 9.8, 10.0 + offset / 10) for offset in range(5))
    market_data = _MarketData(bars=bars)
    service = OutcomeSettlementService(
        market_data,
        repository,
        repository,
        session_distance=lambda start, end: int(end[-2:]) - int(start[-2:]),
    )

    result = service.settle(
        datetime(2026, 7, 24, 15, 10, tzinfo=ZoneInfo("Asia/Shanghai")),
        (application_feature_factory("600001", datetime(2026, 7, 24, 15, 10, tzinfo=ZoneInfo("Asia/Shanghai"))),),
    )

    assert result.outcome_count == 1
    assert tuple(item.horizon for item in repository.saved) == (4,)
