from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.unit.domain.test_decision_identity import decision
from trader.application.decisions.decision_drafts import UnifiedDecisionDraftIndex
from trader.application.market_data.v2_input_runtime import V2DecisionBuildDependencies, V2MarketDataAdapter
from trader.application.ports.v2_runtime import (
    V2CycleRequest,
    V2DataRefreshUnavailableError,
    V2DecisionUnavailableError,
    V2PipelineTaskRequest,
    V2RefreshOutcome,
)
from trader.application.runtime.cadence import PipelineTask
from trader.application.runtime.schedule import SHANGHAI
from trader.bootstrap import _recommendation_policy
from trader.domain.market.models import Board
from trader.domain.recommendation.decision_identity import DecisionOverlay
from trader.domain.recommendation.models import Strategy
from trader.infra.settings import load_strategy_settings

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class _Market:
    def __init__(self, features):
        self._features = tuple(features)
        self.market_fetch_count = 0
        self.candidate_quote_refresh_count = 0
        self.topk_quote_refresh_count = 0
        self.candidate_reads: list[tuple[tuple[str, ...], bool, bool]] = []
        self.reference_requests: list[tuple[tuple[str, ...], tuple[str, ...], datetime, bool]] = []
        self.requested_codes: tuple[str, ...] = ()

    def fetch_market_features(self, _observed_at, *, force=False, deadline=None):
        del force, deadline
        self.market_fetch_count += 1
        return self._features

    def refresh_candidate_quotes(self, codes, _observed_at, *, force=False, deadline=None):
        del force, deadline
        self.candidate_quote_refresh_count += 1
        self.requested_codes = tuple(codes)
        requested = set(codes)
        return tuple(feature for feature in self._features if feature.quote.code in requested)

    def refresh_topk_quotes(self, codes, _observed_at, *, force=False, deadline=None):
        del force, deadline
        self.topk_quote_refresh_count += 1
        self.requested_codes = tuple(codes)
        requested = set(codes)
        return tuple(feature for feature in self._features if feature.quote.code in requested)

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


class _RejectingLongRuntime:
    def offer_refresh(self, _request):
        return False


def _decision_build(
    drafts: UnifiedDecisionDraftIndex | None = None,
) -> V2DecisionBuildDependencies:
    return V2DecisionBuildDependencies(_LongRuntime(), _policy(), drafts or UnifiedDecisionDraftIndex())


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


def _prime_scoring_cache(adapter: V2MarketDataAdapter, observed_at: datetime) -> None:
    adapter.refresh_task(V2PipelineTaskRequest(PipelineTask.FULL_MARKET, observed_at))
    adapter.refresh_task(V2PipelineTaskRequest(PipelineTask.CANDIDATE_QUOTES, observed_at))


def test_refresh_outcome_is_versioned_and_identical_candidate_data_does_not_change(
    application_feature_factory,
) -> None:
    observed_at = datetime(2026, 8, 12, 10, 0, tzinfo=SHANGHAI)
    feature = application_feature_factory("600001", observed_at)
    feature = replace(feature, quote=replace(feature.quote, board=Board.MAIN))
    adapter = V2MarketDataAdapter(
        _Market((feature,)),
        config_version="test-config",
        candidate_pool_size=1,
        decision_build=_decision_build(),
    )

    market = adapter.refresh_task(V2PipelineTaskRequest(PipelineTask.FULL_MARKET, observed_at))
    first = adapter.refresh_task(V2PipelineTaskRequest(PipelineTask.CANDIDATE_QUOTES, observed_at))
    second = adapter.refresh_task(
        V2PipelineTaskRequest(PipelineTask.CANDIDATE_QUOTES, observed_at + timedelta(seconds=1))
    )

    assert isinstance(market, V2RefreshOutcome)
    assert market.changed is True
    assert first.changed is True
    assert first.data_version
    assert first.changed_codes == ("600001",)
    assert second.changed is False
    assert second.data_version == first.data_version


def test_refresh_outcome_normalizes_later_utc_completion_to_shanghai(
    application_feature_factory,
) -> None:
    observed_at = datetime(2026, 8, 12, 10, 0, tzinfo=SHANGHAI)
    completed_at = (observed_at + timedelta(seconds=3)).astimezone(timezone.utc)
    feature = application_feature_factory("600001", observed_at)
    feature = replace(
        feature,
        observed_at=completed_at,
        quote=replace(
            feature.quote,
            board=Board.MAIN,
            received_time=completed_at,
        ),
    )
    adapter = V2MarketDataAdapter(
        _Market((feature,)),
        config_version="test-config",
        candidate_pool_size=1,
        decision_build=_decision_build(),
    )

    outcome = adapter.refresh_task(V2PipelineTaskRequest(PipelineTask.CLOSE_QUOTES, observed_at))

    assert outcome.completed_at == completed_at.astimezone(SHANGHAI)
    assert outcome.completed_at.tzinfo is SHANGHAI


def test_long_refresh_rejection_is_visible_to_scheduler_recovery() -> None:
    observed_at = datetime(2026, 8, 12, 12, 15, tzinfo=SHANGHAI)
    adapter = V2MarketDataAdapter(
        _Market(()),
        config_version="test-config",
        candidate_pool_size=1,
        decision_build=V2DecisionBuildDependencies(
            _RejectingLongRuntime(),
            _policy(),
            UnifiedDecisionDraftIndex(),
        ),
    )

    with pytest.raises(V2DataRefreshUnavailableError, match="long_refresh_rejected"):
        adapter.refresh(_request(observed_at, strategy=Strategy.LONG, phase="midday_recovery"))


def test_reference_lane_can_start_before_the_full_market_universe_without_false_degradation() -> None:
    observed_at = datetime(2026, 8, 12, 9, 15, tzinfo=SHANGHAI)
    adapter = V2MarketDataAdapter(
        _Market(()),
        config_version="test-config",
        candidate_pool_size=1,
        decision_build=_decision_build(),
    )

    adapter.refresh_task(V2PipelineTaskRequest(PipelineTask.REFERENCE_DATA, observed_at))


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
        decision_build=_decision_build(),
    )
    request = _request(observed_at)

    _prime_scoring_cache(adapter, observed_at)
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
        decision_build=_decision_build(),
    )
    request = _request(observed_at, phase="afternoon")

    _prime_scoring_cache(adapter, observed_at)
    adapter.refresh(request)
    decision = adapter.build_local(request)

    assert decision is not None
    assert decision.items == ()


def test_production_adapter_builds_current_overlay_from_another_scored_lane_batch(
    application_feature_factory,
) -> None:
    observed_at = datetime(2026, 8, 12, 13, 5, tzinfo=SHANGHAI)
    network_completed_at = observed_at + timedelta(seconds=3)
    features = tuple(
        replace(
            application_feature_factory(code, network_completed_at),
            quote=replace(application_feature_factory(code, network_completed_at).quote, board=Board.MAIN),
        )
        for code in ("600001", "600003")
    )
    adapter = V2MarketDataAdapter(
        _Market(features),
        config_version="test-config",
        candidate_pool_size=2,
        decision_build=_decision_build(),
    )
    request = _request(observed_at, strategy=Strategy.TOMORROW, phase="afternoon")
    frozen = decision(Strategy.TODAY)
    frozen_quote = frozen.items[0].quote
    assert frozen_quote is not None
    frozen_at = observed_at.replace(hour=11, minute=19, second=59)
    items = tuple(
        replace(
            frozen.items[0],
            code=code,
            rank=rank,
            quote=replace(
                frozen_quote,
                code=code,
                source_time=frozen_at,
                data_version=f"frozen:{code}",
            ),
        )
        for rank, code in enumerate(("600001", "600002", "600003"), start=1)
    )
    frozen = replace(
        frozen,
        trade_date=observed_at.date(),
        observed_at=frozen_at,
        items=items,
    )
    previous = DecisionOverlay(
        frozen.strategy,
        frozen.trade_date,
        frozen.version,
        frozen_at,
        tuple(item.quote for item in items if item.quote is not None),
    )

    _prime_scoring_cache(adapter, observed_at)
    adapter.refresh(request)
    overlay = adapter.refreshed_overlay(frozen, request, previous)

    assert overlay is not None
    assert overlay.strategy is Strategy.TODAY
    assert overlay.parent_version == frozen.version
    assert overlay.observed_at == network_completed_at
    quotes = {quote.code: quote for quote in overlay.quotes}
    assert quotes["600001"].data_version == features[0].quote.data_version
    assert quotes["600003"].data_version == features[1].quote.data_version
    assert quotes["600002"].data_version == "frozen:600002"
    assert quotes["600002"].price == frozen_quote.price


def test_topk_overlay_batch_is_independent_from_full_market_and_scoring_cache(
    application_feature_factory,
) -> None:
    observed_at = datetime(2026, 8, 12, 15, 5, tzinfo=SHANGHAI)
    feature = application_feature_factory("600001", observed_at)
    market = _Market((feature,))
    adapter = V2MarketDataAdapter(
        market,
        config_version="test-config",
        candidate_pool_size=1,
        decision_build=_decision_build(),
    )
    source = decision(Strategy.TOMORROW)
    anchor = source.items[0].quote
    assert anchor is not None
    frozen_at = observed_at.replace(hour=14, minute=50)
    frozen = replace(
        source,
        trade_date=observed_at.date(),
        observed_at=frozen_at,
        items=(replace(source.items[0], quote=replace(anchor, source_time=frozen_at)),),
    )
    frozen_quote = frozen.items[0].quote
    assert frozen_quote is not None
    previous = DecisionOverlay(
        frozen.strategy,
        frozen.trade_date,
        frozen.version,
        frozen_at,
        (frozen_quote,),
    )
    request = _request(observed_at, strategy=Strategy.TOMORROW, phase="quote_overlay")

    adapter.refresh_task(V2PipelineTaskRequest(PipelineTask.TOPK_QUOTES, observed_at, ("600001",)))
    overlay = adapter.refreshed_overlay(frozen, request, previous)

    assert overlay is not None
    assert market.market_fetch_count == 0
    assert market.topk_quote_refresh_count == 1
    assert market.candidate_quote_refresh_count == 0
    assert market.candidate_reads == []
    assert overlay.quotes[0].data_version == feature.quote.data_version


def test_full_market_acquisition_exposes_pending_funnel_without_treating_unknown_stages_as_business_zero(
    application_feature_factory,
) -> None:
    observed_at = datetime(2026, 8, 12, 10, 0, tzinfo=SHANGHAI)
    source = application_feature_factory("600001", observed_at)
    feature = replace(source, quote=replace(source.quote, board=Board.MAIN))
    adapter = V2MarketDataAdapter(
        _Market((feature,)),
        config_version="test-config",
        candidate_pool_size=1,
        decision_build=_decision_build(),
    )

    adapter.refresh_task(V2PipelineTaskRequest(PipelineTask.FULL_MARKET, observed_at))

    statuses = adapter.input_quality_status()
    assert {status.strategy for status in statuses} == {Strategy.TODAY, Strategy.TOMORROW, Strategy.D25}
    assert all(status.status == "not_ready" for status in statuses)
    assert all(status.primary_blocker == "candidate_quotes_pending" for status in statuses)
    assert all(status.supply_funnel.requested_candidates == 1 for status in statuses)
    assert all(status.supply_funnel.candidate_features == 0 for status in statuses)

    adapter.refresh_task(V2PipelineTaskRequest(PipelineTask.CANDIDATE_QUOTES, observed_at))

    assert all(status.primary_blocker == "scoring_pending" for status in adapter.input_quality_status())


def test_production_adapter_rejects_vendor_future_time_without_local_observation_support(
    application_feature_factory,
) -> None:
    observed_at = datetime(2026, 8, 12, 13, 5, tzinfo=SHANGHAI)
    received_at = observed_at + timedelta(seconds=2)
    feature = application_feature_factory("600001", received_at)
    feature = replace(
        feature,
        quote=replace(
            feature.quote,
            board=Board.MAIN,
            source_time=received_at + timedelta(seconds=1),
            received_time=received_at,
            data_version="vendor-future",
        ),
        observed_at=received_at,
    )
    adapter = V2MarketDataAdapter(
        _Market((feature,)),
        config_version="test-config",
        candidate_pool_size=1,
        decision_build=_decision_build(),
    )
    request = _request(observed_at, strategy=Strategy.TOMORROW, phase="afternoon")
    frozen_at = observed_at - timedelta(minutes=1)
    source = decision(Strategy.TODAY)
    frozen_quote = source.items[0].quote
    assert frozen_quote is not None
    frozen_quote = replace(frozen_quote, source_time=frozen_at)
    frozen = replace(
        source,
        trade_date=observed_at.date(),
        observed_at=frozen_at,
        items=(replace(source.items[0], quote=frozen_quote),),
    )
    previous = DecisionOverlay(
        frozen.strategy,
        frozen.trade_date,
        frozen.version,
        frozen.observed_at,
        (frozen_quote,),
    )

    _prime_scoring_cache(adapter, observed_at)
    adapter.refresh(request)

    assert adapter.refreshed_overlay(frozen, request, previous) is None


def test_production_adapter_publishes_eligible_scores_with_partial_history_coverage(
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
    drafts = UnifiedDecisionDraftIndex()
    adapter = V2MarketDataAdapter(
        _Market((complete, incomplete)),
        config_version="test-config",
        candidate_pool_size=2,
        decision_build=_decision_build(drafts),
    )
    request = _request(observed_at, phase="afternoon")

    _prime_scoring_cache(adapter, observed_at)
    adapter.refresh(request)

    decision = adapter.build_local(request)

    assert decision is not None
    assert {item.code for item in decision.items} == {"600001"}
    assert drafts.snapshot(Strategy.TOMORROW) is None
    status = next(item for item in adapter.input_quality_status() if item.strategy is Strategy.TOMORROW)
    assert status.history_covered_count == 1
    assert status.history_coverage_ratio == 0.5
    assert status.candidate_scored_count == 1
    assert status.publishable is True
    assert status.primary_blocker != "history_coverage_incomplete"


def test_production_adapter_does_not_publish_business_empty_when_all_candidate_history_is_insufficient(
    application_feature_factory,
) -> None:
    observed_at = datetime(2026, 8, 12, 14, 40, tzinfo=SHANGHAI)
    incomplete = replace(
        application_feature_factory("600001", observed_at - timedelta(seconds=1)),
        quote=replace(application_feature_factory("600001", observed_at).quote, board=Board.MAIN),
        history_days=19,
    )
    adapter = V2MarketDataAdapter(
        _Market((incomplete,)),
        config_version="test-config",
        candidate_pool_size=1,
        decision_build=_decision_build(),
    )
    request = _request(observed_at, phase="afternoon")

    _prime_scoring_cache(adapter, observed_at)
    adapter.refresh(request)

    with pytest.raises(V2DecisionUnavailableError, match="transient_invalid_empty"):
        adapter.build_local(request)
    status = next(item for item in adapter.input_quality_status() if item.strategy is Strategy.TOMORROW)
    assert status.candidate_scored_count == 0
    assert status.status == "transient_invalid_empty"
    assert status.publishable is False
    assert status.primary_blocker == "strategy_history_unavailable"


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
        decision_build=_decision_build(),
    )
    request = _request(observed_at, phase="afternoon")

    _prime_scoring_cache(adapter, observed_at)
    adapter.refresh(request)

    with pytest.raises(V2DecisionUnavailableError, match="not_ready"):
        adapter.build_local(request)

    status = next(item for item in adapter.input_quality_status() if item.strategy is Strategy.TOMORROW)
    assert status.strategy is Strategy.TOMORROW
    assert status.security_master_covered_count == 0
    assert status.security_master_coverage_ratio == 0.0
    assert "security_master_coverage_incomplete" in status.degraded_reasons
    assert status.supply_funnel.requested_candidates == 1
    assert status.supply_funnel.security_master == 0
    assert status.primary_blocker == "security_master_coverage_incomplete"
    assert status.summary.quote_total_count == 1
    assert status.summary.trade_date == observed_at.date()
    assert status.summary.quote_covered_count == 1
    assert status.summary.quote_missing_count == 0
    assert status.summary.security_identity_missing_count == 1
    assert status.summary.latest_quote_source == degraded.quote.source
    assert status.summary.latest_quote_source_time == degraded.quote.source_time
    assert status.summary.highest_final_score is not None
    assert 0.0 <= status.summary.highest_final_score <= 100.0


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
        decision_build=_decision_build(),
    )
    request = _request(observed_at, phase="afternoon")

    _prime_scoring_cache(adapter, observed_at)
    adapter.refresh(request)
    decision = adapter.build_local(request)

    assert decision is not None
    status = next(item for item in adapter.input_quality_status() if item.strategy is Strategy.TOMORROW)
    assert status.history_covered_count == 99
    assert status.history_coverage_ratio == 0.99
    assert status.publishable is True
    assert status.supply_funnel.history == 99
    assert status.supply_funnel.filter_reject == 100
    assert status.primary_blocker == "no_scored_candidates"


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
        decision_build=_decision_build(),
    )
    request = _request(observed_at, phase="afternoon")

    _prime_scoring_cache(adapter, observed_at)
    adapter.refresh(request)

    with pytest.raises(V2DecisionUnavailableError, match="not_ready"):
        adapter.build_local(request)
    status = next(item for item in adapter.input_quality_status() if item.strategy is Strategy.TOMORROW)
    assert status.candidate_count == 2
    assert status.candidate_feature_count == 1
    assert status.candidate_feature_coverage_ratio == 0.5
    assert status.supply_funnel.requested_candidates == 2
    assert status.supply_funnel.candidate_features == 1
    assert status.primary_blocker == "candidate_feature_coverage_incomplete"
    assert status.summary.quote_total_count == 2
    assert status.summary.quote_covered_count == 1
    assert status.summary.quote_missing_count == 1
    assert "600" not in repr(status) and "300" not in repr(status)


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
        decision_build=_decision_build(),
    )
    requests = tuple(
        _request(observed_at, strategy=strategy, phase="morning")
        for strategy in (Strategy.TOMORROW, Strategy.D25, Strategy.TODAY)
    )
    _prime_scoring_cache(adapter, observed_at)
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
    assert len(market.candidate_reads) == 1
    assert market.candidate_reads[0][1] is True
    assert all(read[2] for read in market.candidate_reads)


def test_tomorrow_scores_a_content_addressed_snapshot_when_tail_changes_during_capture(
    application_feature_factory,
) -> None:
    observed_at = datetime(2026, 8, 12, 14, 40, tzinfo=SHANGHAI)
    feature = application_feature_factory("600001", observed_at)

    class MutatingTailMarket(_Market):
        on_first_read = None

        def read_candidate_features(self, codes, read_at, **options):
            if self.on_first_read is not None:
                callback = self.on_first_read
                self.on_first_read = None
                callback()
            return super().read_candidate_features(codes, read_at, **options)

        def refresh_intraday_tail(self, codes, _observed_at):
            assert tuple(codes) == ("600001",)

    market = MutatingTailMarket((replace(feature, quote=replace(feature.quote, board=Board.MAIN, is_st=True)),))
    adapter = V2MarketDataAdapter(
        market,
        config_version="test-config",
        candidate_pool_size=1,
        decision_build=_decision_build(),
    )
    request = _request(observed_at, phase="afternoon")
    _prime_scoring_cache(adapter, observed_at)
    market.on_first_read = lambda: adapter.refresh_task(
        V2PipelineTaskRequest(PipelineTask.INTRADAY_TAIL, observed_at + timedelta(seconds=1))
    )

    adapter.refresh(request)
    built = adapter.build_local(request)

    assert built is not None
    assert built.trade_date == observed_at.date()


def test_completed_immutable_input_remains_scoreable_after_a_new_quote_epoch(
    application_feature_factory,
) -> None:
    observed_at = datetime(2026, 8, 12, 14, 40, tzinfo=SHANGHAI)
    feature = application_feature_factory("600001", observed_at)
    market = _Market((replace(feature, quote=replace(feature.quote, board=Board.MAIN, is_st=True)),))
    adapter = V2MarketDataAdapter(
        market,
        config_version="test-config",
        candidate_pool_size=1,
        decision_build=_decision_build(),
    )
    request = _request(observed_at, strategy=Strategy.D25, phase="afternoon")
    _prime_scoring_cache(adapter, observed_at)
    adapter.refresh(request)
    changed = replace(
        market._features[0],
        quote=replace(
            market._features[0].quote,
            data_version="newer-quote",
            source_time=observed_at + timedelta(seconds=1),
            received_time=observed_at + timedelta(seconds=1),
        ),
        observed_at=observed_at + timedelta(seconds=1),
    )
    market._features = (changed,)
    adapter.refresh_task(V2PipelineTaskRequest(PipelineTask.CANDIDATE_QUOTES, observed_at + timedelta(seconds=1)))

    built = adapter.build_local(request)

    assert built is not None
    assert built.observed_at == observed_at


def test_topk_refresh_reuses_returned_features_without_a_second_feature_read(
    application_feature_factory,
) -> None:
    observed_at = datetime(2026, 8, 12, 10, 0, tzinfo=SHANGHAI)
    feature = application_feature_factory("600001", observed_at)
    market = _Market((replace(feature, quote=replace(feature.quote, board=Board.MAIN)),))
    adapter = V2MarketDataAdapter(
        market,
        config_version="test-config",
        candidate_pool_size=1,
        decision_build=_decision_build(),
    )

    outcome = adapter.refresh_task(
        V2PipelineTaskRequest(PipelineTask.TOPK_QUOTES, observed_at, selected_codes=("600001",))
    )

    assert outcome.changed is True
    assert market.topk_quote_refresh_count == 1
    assert market.candidate_reads == []


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
            completed = tuple(
                replace(
                    feature,
                    observed_at=completed_at,
                    quote=replace(
                        feature.quote,
                        source_time=completed_at,
                        received_time=completed_at,
                    ),
                )
                for feature in result
            )
            self._features = completed
            return completed

    market = AdvancingMarket(tuple(features))
    adapter = V2MarketDataAdapter(
        market,
        config_version="test-config",
        candidate_pool_size=1,
        decision_build=_decision_build(),
    )
    requests = tuple(
        _request(requested_at, strategy=strategy, phase="morning")
        for strategy in (Strategy.TOMORROW, Strategy.D25, Strategy.TODAY)
    )

    _prime_scoring_cache(adapter, requested_at)
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
        decision_build=_decision_build(),
    )
    request = _request(requested_at, phase="morning")

    _prime_scoring_cache(adapter, requested_at)
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
        decision_build=_decision_build(),
    )

    _prime_scoring_cache(adapter, observed_at)

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
        decision_build=_decision_build(),
    )
    request = _request(observed_at, phase="afternoon")

    _prime_scoring_cache(adapter, observed_at)
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
        decision_build=_decision_build(),
    )
    request = _request(observed_at, phase="afternoon")

    _prime_scoring_cache(adapter, observed_at)
    adapter.refresh(request)
    decision = adapter.build_local(request)

    assert decision is not None
    diagnostics = decision.selection_diagnostics
    assert diagnostics is not None
    status = next(item for item in adapter.input_quality_status() if item.strategy is Strategy.TOMORROW)
    assert status.supply_funnel.observation_threshold_met_count == sum(
        item.final_score >= diagnostics.observation_floor for item in decision.items
    )
    assert status.supply_funnel.executable_threshold_met_count == sum(
        item.final_score >= diagnostics.executable_threshold for item in decision.items
    )
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
        def fetch_market_features(self, _observed_at, *, force=False, deadline=None):
            if self.market_fetch_count == 0:
                self.market_fetch_count += 1
                raise KeyError("unexpected implementation failure")
            return super().fetch_market_features(_observed_at, force=force, deadline=deadline)

    market = RetryMarket((feature,))
    adapter = V2MarketDataAdapter(
        market,
        config_version="test-config",
        candidate_pool_size=1,
        decision_build=_decision_build(),
    )
    request = _request(observed_at, phase="morning")

    with pytest.raises(KeyError, match="unexpected implementation failure"):
        adapter.refresh_task(V2PipelineTaskRequest(PipelineTask.FULL_MARKET, observed_at))
    _prime_scoring_cache(adapter, observed_at)
    adapter.refresh(request)

    assert market.market_fetch_count == 2
    assert adapter.build_local(request) is not None


def _policy():
    return _recommendation_policy(load_strategy_settings(PROJECT_ROOT / "config" / "v2" / "strategy.json"))
