from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from trader.application.ports.v2_runtime import V2CycleRequest, V2DecisionUnavailableError
from trader.application.schedule import SHANGHAI
from trader.application.v2_input_runtime import V2MarketDataAdapter
from trader.bootstrap import _recommendation_policy
from trader.domain.market.models import Board
from trader.domain.recommendation.models import Strategy
from trader.infra.settings import load_strategy_settings

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class _Market:
    def __init__(self, features):
        self._features = tuple(features)
        self.market_fetch_count = 0
        self.candidate_quote_refresh_count = 0
        self.candidate_reads: list[tuple[tuple[str, ...], bool, bool]] = []
        self.requested_codes: tuple[str, ...] = ()

    def fetch_market_features(self, _observed_at, *, force=False):
        del force
        self.market_fetch_count += 1
        return self._features

    def refresh_candidate_quotes(self, codes, _observed_at, *, force=False, deadline=None):
        del force, deadline
        self.candidate_quote_refresh_count += 1
        self.requested_codes = tuple(codes)
        return ()

    def read_candidate_features(
        self,
        codes,
        _observed_at,
        *,
        include_intraday_tail=False,
        include_structured_research=False,
    ):
        self.candidate_reads.append((tuple(codes), include_intraday_tail, include_structured_research))
        requested = set(codes)
        return tuple(feature for feature in self._features if feature.quote.code in requested)

    def refresh_intraday_tail(self, codes, _observed_at):
        del codes
        raise AssertionError("the local decision path must not wait for an intraday network refresh")


class _LongRuntime:
    def offer_refresh(self, _request):
        return True


def _request(
    observed_at: datetime,
    *,
    strategy: Strategy = Strategy.TOMORROW,
    phase: str = "close_fallback",
) -> V2CycleRequest:
    return V2CycleRequest(
        strategy,
        observed_at.date(),
        observed_at,
        phase,
        1,
        f"test:{strategy.value}:{observed_at:%Y%m%dT%H%M%S}",
        False,
        observed_at.replace(hour=14, minute=48),
    )


def test_production_adapter_rejects_transient_invalid_empty_projection(
    application_feature_factory,
) -> None:
    observed_at = datetime(2026, 8, 12, 15, 5, tzinfo=SHANGHAI)
    feature = application_feature_factory("600001", observed_at - timedelta(minutes=1))
    stale = replace(feature, quote=replace(feature.quote, board=Board.MAIN))
    adapter = V2MarketDataAdapter(
        _Market((stale,)),
        config_version="test-config",
        candidate_pool_size=1,
        long_runtime=_LongRuntime(),
        policy=_policy(),
    )
    request = _request(observed_at)

    adapter.refresh(request)

    with pytest.raises(V2DecisionUnavailableError, match="transient_invalid_empty"):
        adapter.build_local(request)


def test_production_adapter_preserves_publishable_business_empty_projection(
    application_feature_factory,
) -> None:
    observed_at = datetime(2026, 8, 12, 14, 40, tzinfo=SHANGHAI)
    feature = application_feature_factory("600001", observed_at - timedelta(seconds=1))
    blocked = replace(feature, quote=replace(feature.quote, board=Board.MAIN, is_st=True))
    adapter = V2MarketDataAdapter(
        _Market((blocked,)),
        config_version="test-config",
        candidate_pool_size=1,
        long_runtime=_LongRuntime(),
        policy=_policy(),
    )
    request = _request(observed_at, phase="afternoon")

    adapter.refresh(request)
    decision = adapter.build_local(request)

    assert decision is not None
    assert decision.items == ()


def test_three_scored_strategies_share_one_fast_market_input_cycle(
    application_feature_factory,
) -> None:
    observed_at = datetime(2026, 8, 12, 10, 0, tzinfo=SHANGHAI)
    codes = (("600001", Board.MAIN), ("300001", Board.CHINEXT), ("688001", Board.STAR))
    features = tuple(
        replace(feature, quote=replace(feature.quote, board=board, is_st=True))
        for code, board in codes
        for feature in (application_feature_factory(code, observed_at),)
    )
    market = _Market(features)
    adapter = V2MarketDataAdapter(
        market,
        config_version="test-config",
        candidate_pool_size=1,
        long_runtime=_LongRuntime(),
        policy=_policy(),
    )
    requests = tuple(
        _request(observed_at, strategy=strategy, phase="morning")
        for strategy in (Strategy.TOMORROW, Strategy.D25, Strategy.TODAY)
    )
    entered = threading.Barrier(len(requests))

    def refresh(request: V2CycleRequest) -> None:
        entered.wait(timeout=1.0)
        adapter.refresh(request)

    with ThreadPoolExecutor(max_workers=3) as executor:
        tuple(executor.map(refresh, requests))

    decisions = tuple(adapter.build_local(request) for request in requests)

    assert market.market_fetch_count == 1
    assert market.candidate_quote_refresh_count == 1
    assert set(market.requested_codes) == {"600001", "300001", "688001"}
    assert all(decision is not None for decision in decisions)
    assert all(decision.items == () for decision in decisions if decision is not None)
    assert any(read[1] for read in market.candidate_reads)
    assert all(read[2] for read in market.candidate_reads)


def test_candidate_pool_limit_is_applied_per_supported_board(
    application_feature_factory,
) -> None:
    observed_at = datetime(2026, 8, 12, 10, 0, tzinfo=SHANGHAI)
    codes = (
        ("600001", Board.MAIN),
        ("600002", Board.MAIN),
        ("300001", Board.CHINEXT),
        ("300002", Board.CHINEXT),
        ("688001", Board.STAR),
        ("688002", Board.STAR),
    )
    features = tuple(
        replace(feature, quote=replace(feature.quote, board=board))
        for code, board in codes
        for feature in (application_feature_factory(code, observed_at),)
    )
    market = _Market(features)
    adapter = V2MarketDataAdapter(
        market,
        config_version="test-config",
        candidate_pool_size=1,
        long_runtime=_LongRuntime(),
        policy=_policy(),
    )

    adapter.refresh(_request(observed_at, phase="morning"))

    assert len(market.requested_codes) == 3
    assert {code[:3] for code in market.requested_codes} == {"600", "300", "688"}


def test_unexpected_shared_input_failure_releases_single_flight_owner(
    application_feature_factory,
) -> None:
    observed_at = datetime(2026, 8, 12, 10, 0, tzinfo=SHANGHAI)
    feature = application_feature_factory("600001", observed_at)
    feature = replace(feature, quote=replace(feature.quote, board=Board.MAIN, is_st=True))

    class RetryMarket(_Market):
        def fetch_market_features(self, _observed_at, *, force=False):
            if self.market_fetch_count == 0:
                self.market_fetch_count += 1
                raise KeyError("unexpected implementation failure")
            return super().fetch_market_features(_observed_at, force=force)

    market = RetryMarket((feature,))
    adapter = V2MarketDataAdapter(
        market,
        config_version="test-config",
        candidate_pool_size=1,
        long_runtime=_LongRuntime(),
        policy=_policy(),
    )
    request = _request(observed_at, phase="morning")

    with pytest.raises(KeyError, match="unexpected implementation failure"):
        adapter.refresh(request)
    adapter.refresh(request)

    assert market.market_fetch_count == 2
    assert adapter.build_local(request) is not None


def _policy():
    return _recommendation_policy(load_strategy_settings(PROJECT_ROOT / "config" / "v2" / "strategy.json"))
