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
        self.reference_requests: list[tuple[tuple[str, ...], tuple[str, ...], datetime, bool]] = []
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

    def schedule_reference_data(
        self,
        codes,
        observed_at,
        *,
        force=False,
        security_master_codes=None,
    ):
        self.reference_requests.append((tuple(codes), tuple(security_master_codes or ()), observed_at, force))

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


def test_production_adapter_rejects_partial_history_coverage_even_when_one_candidate_scores(
    application_feature_factory,
) -> None:
    observed_at = datetime(2026, 8, 12, 14, 40, tzinfo=SHANGHAI)
    complete = replace(
        application_feature_factory("600001", observed_at - timedelta(seconds=1)),
        quote=replace(application_feature_factory("600001", observed_at).quote, board=Board.MAIN),
    )
    incomplete = replace(
        application_feature_factory("600002", observed_at - timedelta(seconds=1)),
        quote=replace(application_feature_factory("600002", observed_at).quote, board=Board.MAIN),
        history_days=19,
    )
    adapter = V2MarketDataAdapter(
        _Market((complete, incomplete)),
        config_version="test-config",
        candidate_pool_size=2,
        long_runtime=_LongRuntime(),
        policy=_policy(),
    )
    request = _request(observed_at, phase="afternoon")

    adapter.refresh(request)

    with pytest.raises(V2DecisionUnavailableError, match="not_ready"):
        adapter.build_local(request)


def test_production_adapter_rejects_candidate_security_identity_degradation(
    application_feature_factory,
) -> None:
    observed_at = datetime(2026, 8, 12, 14, 40, tzinfo=SHANGHAI)
    feature = application_feature_factory("600001", observed_at - timedelta(seconds=1))
    degraded = replace(
        feature,
        quote=replace(
            feature.quote,
            board=Board.MAIN,
            execution_restrictions=("missing_listing_date",),
        ),
    )
    adapter = V2MarketDataAdapter(
        _Market((degraded,)),
        config_version="test-config",
        candidate_pool_size=1,
        long_runtime=_LongRuntime(),
        policy=_policy(),
    )
    request = _request(observed_at, phase="afternoon")

    adapter.refresh(request)

    with pytest.raises(V2DecisionUnavailableError, match="not_ready"):
        adapter.build_local(request)

    status = adapter.input_quality_status()["tomorrow"]
    assert status["security_master_covered_count"] == 0
    assert status["security_master_coverage_ratio"] == 0.0
    assert "security_master_coverage_incomplete" in status["degraded_reasons"]
    assert status["supply_funnel"]["requested_candidates"] == 1
    assert status["supply_funnel"]["security_master"] == 0
    assert status["primary_blocker"] == "security_master_coverage_incomplete"


def test_production_adapter_accepts_exactly_ninety_nine_percent_history_coverage(
    application_feature_factory,
) -> None:
    observed_at = datetime(2026, 8, 12, 14, 40, tzinfo=SHANGHAI)
    features = tuple(
        replace(
            application_feature_factory(f"600{index:03d}", observed_at - timedelta(seconds=1)),
            quote=replace(
                application_feature_factory(f"600{index:03d}", observed_at).quote,
                board=Board.MAIN,
                is_st=True,
            ),
            history_days=19 if index == 0 else 60,
        )
        for index in range(100)
    )
    adapter = V2MarketDataAdapter(
        _Market(features),
        config_version="test-config",
        candidate_pool_size=100,
        long_runtime=_LongRuntime(),
        policy=_policy(),
    )
    request = _request(observed_at, phase="afternoon")

    adapter.refresh(request)
    decision = adapter.build_local(request)

    assert decision is not None
    status = adapter.input_quality_status()["tomorrow"]
    assert status["history_covered_count"] == 99
    assert status["history_coverage_ratio"] == 0.99
    assert status["publishable"] is True
    assert status["supply_funnel"]["history"] == 99
    assert status["supply_funnel"]["filter_reject"] == 100
    assert status["primary_blocker"] == "no_scored_candidates"


def test_production_adapter_rejects_partial_candidate_feature_response(
    application_feature_factory,
) -> None:
    observed_at = datetime(2026, 8, 12, 14, 40, tzinfo=SHANGHAI)
    features = tuple(
        replace(
            application_feature_factory(code, observed_at - timedelta(seconds=1)),
            quote=replace(application_feature_factory(code, observed_at).quote, board=board),
        )
        for code, board in (("600001", Board.MAIN), ("300001", Board.CHINEXT))
    )

    class PartialCandidateMarket(_Market):
        def read_candidate_features(self, codes, observed_at, **options):
            return super().read_candidate_features(codes, observed_at, **options)[:1]

    adapter = V2MarketDataAdapter(
        PartialCandidateMarket(features),
        config_version="test-config",
        candidate_pool_size=1,
        long_runtime=_LongRuntime(),
        policy=_policy(),
    )
    request = _request(observed_at, phase="afternoon")

    adapter.refresh(request)

    with pytest.raises(V2DecisionUnavailableError, match="not_ready"):
        adapter.build_local(request)
    status = adapter.input_quality_status()["tomorrow"]
    assert status["candidate_count"] == 2
    assert status["candidate_feature_count"] == 1
    assert status["candidate_feature_coverage_ratio"] == 0.5
    assert status["supply_funnel"]["requested_candidates"] == 2
    assert status["supply_funnel"]["candidate_features"] == 1
    assert status["primary_blocker"] == "candidate_feature_coverage_incomplete"
    assert all("600" not in str(value) and "300" not in str(value) for value in status.values())


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
    assert market.reference_requests == [
        (
            market.requested_codes,
            tuple(feature.quote.code for feature in features),
            observed_at,
            False,
        )
    ]
    assert all(decision is not None for decision in decisions)
    assert all(decision.items == () for decision in decisions if decision is not None)
    assert any(read[1] for read in market.candidate_reads)
    assert all(read[2] for read in market.candidate_reads)


def test_three_scored_strategies_use_refresh_completion_as_the_decision_time(
    application_feature_factory,
) -> None:
    requested_at = datetime(2026, 8, 12, 10, 0, tzinfo=SHANGHAI)
    completed_at = requested_at + timedelta(seconds=5)
    features = []
    for code, board in (("600001", Board.MAIN), ("300001", Board.CHINEXT), ("688001", Board.STAR)):
        feature = application_feature_factory(code, requested_at)
        features.append(replace(feature, quote=replace(feature.quote, board=board, is_st=True)))

    class AdvancingMarket(_Market):
        def refresh_candidate_quotes(self, codes, _observed_at, *, force=False, deadline=None):
            result = super().refresh_candidate_quotes(codes, _observed_at, force=force, deadline=deadline)
            self._features = tuple(
                replace(
                    feature,
                    observed_at=completed_at,
                    quote=replace(
                        feature.quote,
                        source_time=completed_at,
                        received_time=completed_at,
                    ),
                )
                for feature in self._features
            )
            return result

    market = AdvancingMarket(tuple(features))
    adapter = V2MarketDataAdapter(
        market,
        config_version="test-config",
        candidate_pool_size=1,
        long_runtime=_LongRuntime(),
        policy=_policy(),
    )
    requests = tuple(
        _request(requested_at, strategy=strategy, phase="morning")
        for strategy in (Strategy.TOMORROW, Strategy.D25, Strategy.TODAY)
    )

    for request in requests:
        adapter.refresh(request)
    decisions = tuple(adapter.build_local(request) for request in requests)

    assert all(decision is not None for decision in decisions)
    assert all(decision.observed_at == completed_at for decision in decisions if decision is not None)


def test_decision_time_does_not_trust_a_future_vendor_source_time(
    application_feature_factory,
) -> None:
    requested_at = datetime(2026, 8, 12, 10, 0, tzinfo=SHANGHAI)
    feature = application_feature_factory("600001", requested_at)
    feature = replace(
        feature,
        quote=replace(
            feature.quote,
            board=Board.MAIN,
            is_st=True,
            source_time=requested_at + timedelta(minutes=1),
        ),
    )
    adapter = V2MarketDataAdapter(
        _Market((feature,)),
        config_version="test-config",
        candidate_pool_size=1,
        long_runtime=_LongRuntime(),
        policy=_policy(),
    )
    request = _request(requested_at, phase="morning")

    adapter.refresh(request)

    with pytest.raises(V2DecisionUnavailableError, match="future_input_time"):
        adapter.build_local(request)


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


def test_reference_refresh_scheduling_failure_does_not_block_local_decision(
    application_feature_factory,
) -> None:
    observed_at = datetime(2026, 8, 12, 14, 40, tzinfo=SHANGHAI)
    feature = application_feature_factory("600001", observed_at - timedelta(seconds=1))
    blocked = replace(feature, quote=replace(feature.quote, board=Board.MAIN, is_st=True))

    class ReferenceFailureMarket(_Market):
        def schedule_reference_data(
            self,
            codes,
            observed_at,
            *,
            force=False,
            security_master_codes=None,
        ):
            del codes, observed_at, force, security_master_codes
            raise RuntimeError("reference lane unavailable")

    adapter = V2MarketDataAdapter(
        ReferenceFailureMarket((blocked,)),
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


def test_research_intent_prioritizes_published_output_before_bounded_candidates(
    application_feature_factory,
) -> None:
    observed_at = datetime(2026, 8, 12, 14, 40, tzinfo=SHANGHAI)
    features = []
    for code in ("600001", "600002"):
        feature = application_feature_factory(code, observed_at - timedelta(seconds=1))
        features.append(
            replace(
                feature,
                quote=replace(
                    feature.quote,
                    board=Board.MAIN,
                    board_source="exchange_rule",
                    board_reliability="high",
                    exchange="SSE",
                    listing_date=observed_at.date() - timedelta(days=365),
                    listing_age_sessions=240,
                    is_relisted_first_session=False,
                    is_delisting_period_first_session=False,
                    has_price_limit=True,
                    exchange_limit_pct=10.0,
                    strategy_hot_cap_pct=8.0,
                    rule_version="test-rule",
                    rule_effective_date=observed_at.date() - timedelta(days=365),
                ),
            )
        )
    market = _Market(tuple(features))
    adapter = V2MarketDataAdapter(
        market,
        config_version="test-config",
        candidate_pool_size=2,
        long_runtime=_LongRuntime(),
        policy=_policy(),
    )
    request = _request(observed_at, phase="afternoon")

    adapter.refresh(request)
    decision = adapter.build_local(request)

    assert decision is not None
    intent = adapter.research_intent(decision)
    assert intent.priority_codes == tuple(item.code for item in decision.items)
    assert intent.candidate_codes == market.requested_codes


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
