from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from trader.recommendation.application.ports.runtime import SettlementUnavailableError
from trader.recommendation.application.ports.market_data import MarketDataPendingError, MarketDataUnavailableError
from trader.recommendation.application.ports.outcomes import OutcomeEvidenceConflictError
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.application.outcome_settlement import OutcomeSettlementAdapter, OutcomeSettlementService
from trader.recommendation.domain.outcomes.models import (
    BenchmarkReturn,
    OutcomeBar,
    OutcomePrice,
    OutcomeTarget,
    OutcomeTradingStatus,
)

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


class _OutcomeIndex:
    def __init__(self) -> None:
        self.saved = ()
        self.benchmark = ()

    def benchmark_return(self, trade_date):
        return self.benchmark[0] if self.benchmark and self.benchmark[0].trade_date == trade_date else None

    def pending_outcome_targets(self, *, limit):
        assert limit == 500
        return (
            OutcomeTarget(
                "snapshot",
                Strategy.TOMORROW,
                "2026-07-20",
                "600001",
                10.0,
                2.0,
                entry_at=NOW.replace(day=20, hour=15, minute=0),
            ),
        )

    def record_benchmark_return(self, benchmark, *, observed_at):
        self.benchmark = (benchmark, observed_at)

    def benchmark_returns_after(self, recommend_date, *, limit):
        assert recommend_date == "2026-07-20"
        assert limit == 1
        return (BenchmarkReturn("2026-07-21", 0.5),)

    def save_recommendation_outcomes(self, outcomes):
        self.saved = tuple(outcomes)


def _service(market_data: _MarketData, index: _OutcomeIndex, *, elapsed: int = 1):
    return OutcomeSettlementService(
        market_data,
        index,
        index,
        session_distance=lambda _start, _end: elapsed,
    )


def test_after_close_settlement_records_benchmark_and_due_outcome(application_feature_factory) -> None:
    index = _OutcomeIndex()
    feature = application_feature_factory("600001", NOW)
    peer = application_feature_factory("600002", NOW)
    market_data = _MarketData()

    result = _service(market_data, index).settle(
        NOW,
        (feature, replace(peer, quote=replace(peer.quote, pct_change=1.0))),
    )

    assert result.target_count == 1
    assert result.completed_count == 1
    assert index.benchmark == (BenchmarkReturn("2026-07-21", 2.0), NOW)
    assert len(index.saved) == 1
    assert index.saved[0].net_excess_return_pct == pytest.approx(1.3)


def test_settlement_skips_horizons_that_are_not_due(application_feature_factory) -> None:
    index = _OutcomeIndex()
    market_data = _MarketData()

    result = _service(market_data, index, elapsed=0).settle(
        NOW,
        (application_feature_factory("600001", NOW),),
    )

    assert result.completed_count == 0
    assert index.saved == ()


def test_settlement_keeps_due_stock_outcome_retryable_when_benchmark_is_unavailable(
    application_feature_factory,
) -> None:
    index = _OutcomeIndex()
    index.benchmark_returns_after = lambda _date, *, limit: ()
    stale = application_feature_factory(
        "600001",
        datetime(2026, 7, 21, 14, 59, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    result = _service(_MarketData(), index).settle(NOW, (stale,))

    assert result.benchmark_recorded is False
    assert result.outcome_count == 1
    assert result.retryable_count == 1
    assert index.saved == ()


def test_equal_weight_benchmark_requires_every_market_return(application_feature_factory) -> None:
    index = _OutcomeIndex()
    first = application_feature_factory("600001", NOW)
    second = application_feature_factory("600002", NOW)
    incomplete = replace(second, quote=replace(second.quote, pct_change=None))

    result = _service(_MarketData(), index).settle(NOW, (first, incomplete))

    assert result.benchmark_recorded is False
    assert index.benchmark == ()


def test_settlement_preserves_complete_return_when_atr_risk_label_is_unavailable(
    application_feature_factory,
) -> None:
    class _InvalidAtrOutcomeIndex(_OutcomeIndex):
        def pending_outcome_targets(self, *, limit):
            return tuple(replace(target, atr20_pct=0.0) for target in super().pending_outcome_targets(limit=limit))

    index = _InvalidAtrOutcomeIndex()
    market_data = _MarketData()

    result = _service(market_data, index).settle(
        NOW,
        (application_feature_factory("600001", NOW),),
    )

    assert result.completed_count == 1
    assert result.retryable_count == 0
    assert len(index.saved) == 1
    outcome = index.saved[0]
    assert outcome.status == "complete"
    assert outcome.net_excess_return_pct == pytest.approx(1.3)
    assert outcome.mae_pct == pytest.approx(-4.0)
    assert outcome.atr20_pct is None
    assert outcome.mae_atr is None
    assert outcome.severe_drawdown is None
    assert outcome.quality_reason == "atr20_unavailable"
    assert outcome.risk_label_complete is False


def test_adapter_fetches_fresh_close_market_before_settlement(application_feature_factory) -> None:
    feature = application_feature_factory("600001", NOW)
    market_data = _MarketData((feature,))
    index = _OutcomeIndex()
    adapter = OutcomeSettlementAdapter(market_data, _service(market_data, index))

    adapter.settle(NOW)

    assert market_data.fetch_calls == [(NOW, True)]
    assert index.benchmark == (BenchmarkReturn("2026-07-21", 3.0), NOW)


def test_adapter_reuses_recorded_benchmark_without_fetching_changed_market(application_feature_factory):
    evidence = _OutcomeIndex()
    evidence.benchmark = (BenchmarkReturn("2026-07-21", 0.5), NOW)
    market = _MarketData((application_feature_factory("600002", NOW),))
    original = evidence.benchmark

    OutcomeSettlementAdapter(market, _service(market, evidence)).settle(NOW)

    assert market.fetch_calls == []
    assert evidence.benchmark == original
    assert len(evidence.saved) == 1
    assert evidence.saved[0].status == "complete"


@pytest.mark.parametrize(
    "failure, expected",
    (
        (MarketDataPendingError("private supplier payload"), "settlement_inputs_pending"),
        (MarketDataUnavailableError("private supplier payload"), "settlement_market_data_unavailable"),
        (OutcomeEvidenceConflictError("private business evidence"), "settlement_evidence_conflict"),
    ),
)
@pytest.mark.parametrize("boundary", ("fetch", "history", "benchmark"))
def test_adapter_translates_failures_and_preserves_cause_and_evidence(monkeypatch, failure, expected, boundary):
    evidence = _OutcomeIndex()
    market = _MarketData()

    def fail(*args, **kwargs):
        raise failure

    if boundary == "fetch":
        monkeypatch.setattr(market, "fetch_market_features", fail)
    elif boundary == "history":
        evidence.benchmark = (BenchmarkReturn("2026-07-21", 0.5), NOW)
        monkeypatch.setattr(market, "read_outcome_bars", fail)
    else:
        monkeypatch.setattr(evidence, "benchmark_return", fail)
    original = evidence.benchmark
    with pytest.raises(SettlementUnavailableError) as caught:
        OutcomeSettlementAdapter(market, _service(market, evidence)).settle(NOW)

    assert str(caught.value) == expected
    assert caught.value.__cause__ is failure
    assert evidence.benchmark == original
    assert evidence.saved == ()


def test_adapter_rejects_incomplete_settlement_without_poisoning_immutable_evidence(
    application_feature_factory,
) -> None:
    stale = application_feature_factory(
        "600001",
        datetime(2026, 7, 21, 14, 59, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    market_data = _MarketData((stale,))
    index = _OutcomeIndex()
    index.benchmark_returns_after = lambda _date, *, limit: ()
    adapter = OutcomeSettlementAdapter(market_data, _service(market_data, index))

    with pytest.raises(SettlementUnavailableError, match="settlement_outcomes_incomplete"):
        adapter.settle(NOW)

    assert index.saved == ()
    market_data.features = (application_feature_factory("600001", NOW),)
    index.benchmark_returns_after = lambda _date, *, limit: (BenchmarkReturn("2026-07-21", 0.5),)
    adapter.settle(NOW)
    assert len(index.saved) == 1
    assert index.saved[0].status == "complete"


def test_d25_settlement_includes_t4_but_only_evaluates_pending_horizons(application_feature_factory) -> None:
    class _D25OutcomeIndex(_OutcomeIndex):
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
                    entry_at=NOW.replace(day=20, hour=15, minute=0),
                ),
            )

        def benchmark_returns_after(self, recommend_date, *, limit):
            assert recommend_date == "2026-07-20"
            assert limit == 4
            return tuple(BenchmarkReturn(f"2026-07-{20 + offset:02d}", 0.0) for offset in range(1, 5))

    index = _D25OutcomeIndex()
    bars = tuple(_bar(f"2026-07-{20 + offset:02d}", 10.0, 10.5, 9.8, 10.0 + offset / 10) for offset in range(5))
    market_data = _MarketData(bars=bars)
    service = OutcomeSettlementService(
        market_data,
        index,
        index,
        session_distance=lambda start, end: int(end[-2:]) - int(start[-2:]),
    )

    result = service.settle(
        datetime(2026, 7, 24, 15, 10, tzinfo=ZoneInfo("Asia/Shanghai")),
        (application_feature_factory("600001", datetime(2026, 7, 24, 15, 10, tzinfo=ZoneInfo("Asia/Shanghai"))),),
    )

    assert result.outcome_count == 1
    assert tuple(item.horizon for item in index.saved) == (4,)
