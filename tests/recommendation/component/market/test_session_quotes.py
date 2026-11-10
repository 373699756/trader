from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.recommendation.market_data import CountingMarketClient, SequenceMarketClient, _quote, replace
from trader.recommendation.application.runtime.cadence import CadencePlanner, CadencePolicy
from trader.recommendation.domain.publication.preview import PreviewContext
from trader.recommendation.domain.market.quote_freshness import DEFAULT_QUOTE_SESSION_TIMES, QuoteSessionTimes
from trader.recommendation.domain.candidate.filters import LevelOneFilterRules
from trader.recommendation.application.pipeline.dynamic_market.market_snapshot_service import DynamicSourceHealth
from tests.helpers.recommendation.configuration import HardFilterPolicyFixture
from trader.recommendation.infra.market_data.gateway import MarketDataGateway
from trader.infra.settings import LoadRuntimeSettings

_SHANGHAI = ZoneInfo("Asia/Shanghai")


class _DirectedQuotes:
    def __init__(self, quotes):
        self.quotes = quotes
        self.calls = 0

    def fetch_quotes(self, codes, *, timeout_seconds=None, cancel_event=None):
        self.calls += 1
        return tuple(quote for quote in self.quotes if quote.code in codes)


def _gateway(at, source, *, market=None, quote_sessions=DEFAULT_QUOTE_SESSION_TIMES):
    quote = replace(_quote(), source_time=source, received_time=max(at[0], source))
    market = CountingMarketClient((quote,)) if market is None else market
    directed = _DirectedQuotes((quote,))
    backup = CountingMarketClient((quote,))
    gateway = MarketDataGateway(
        market, backup, directed, minimum_market_rows=1,
        circuit_breaker_failures=3, circuit_breaker_seconds=60, wall_clock=lambda: at[0],
        quote_sessions=quote_sessions,
    )
    return gateway, market, backup, directed


@pytest.mark.parametrize("hour,minute,second", [(0, 0, 0), (9, 15, 0), (9, 29, 59)])
def test_trading_day_before_open_has_no_quote_requests_or_scheduled_acquisition(hour, minute, second):
    at = [datetime(2026, 11, 10, hour, minute, second, tzinfo=_SHANGHAI)]
    gateway, market, backup, directed = _gateway(at, at[0])
    assert tuple(gateway.fetch_market(observed_at=at[0], force=True)) == ()
    assert tuple(gateway.fetch_candidates(("600001",), observed_at=at[0], force=True)) == ()
    assert tuple(gateway.fetch_long_quotes(("600001",), observed_at=at[0], force=True)) == ()
    assert (market.calls, backup.calls, directed.calls) == (0, 0, 0)
    settings = LoadRuntimeSettings(Path("config/runtime.json"))
    planner = CadencePlanner(CadencePolicy.from_seconds(settings.pipeline.cadence_seconds), started_at=at[0])
    assert planner.plan(at[0], is_trading_day=True).tasks == ()
    assert planner.plan_score_after_input(at[0], is_trading_day=True) is None


@pytest.mark.parametrize("hour,minute,source_hour,source_minute", [(12, 30, 11, 30), (22, 0, 15, 0)])
def test_fixed_session_reuses_canonical_quotes_across_market_candidate_and_long(
    hour, minute, source_hour, source_minute,
):
    at = [datetime(2026, 11, 10, hour, minute, tzinfo=_SHANGHAI)]
    source = at[0].replace(hour=source_hour, minute=source_minute)
    gateway, market, backup, directed = _gateway(at, source)
    first = tuple(gateway.fetch_market(observed_at=at[0], force=True))
    assert len(first) == 1
    for _ in range(3):
        assert tuple(gateway.fetch_market(observed_at=at[0], force=True)) == first
        assert tuple(gateway.fetch_candidates(("600001",), observed_at=at[0], force=True)) == first
        assert tuple(gateway.fetch_long_quotes(("600001",), observed_at=at[0], force=True)) == first
    assert first[0].source_time == source
    assert (market.calls, backup.calls, directed.calls) == (1, 0, 0)


def test_afternoon_open_resumes_physical_collection_and_replaces_lunch_quote():
    at = [datetime(2026, 11, 10, 12, 59, 59, tzinfo=_SHANGHAI)]
    lunch = replace(_quote(), source_time=at[0].replace(hour=11, minute=30, second=0), received_time=at[0])
    reopened_at = at[0].replace(hour=13, minute=0, second=0)
    reopened = replace(lunch, source_time=reopened_at, received_time=reopened_at, price=11.0, high=11.0)
    market = SequenceMarketClient(((lunch,), (reopened,)))
    gateway, _, _, _ = _gateway(at, lunch.source_time, market=market)
    first = tuple(gateway.fetch_market(observed_at=at[0], force=True))
    assert first[0].source_time == lunch.source_time
    at[0] = reopened.source_time
    second = tuple(gateway.fetch_market(observed_at=at[0], force=True))
    assert market.calls == 2
    assert second[0].source_time == reopened.source_time
    assert second[0].price == 11.0


def test_directed_quotes_alone_cannot_replace_a_full_market_snapshot():
    at = [datetime(2026, 11, 10, 12, 0, tzinfo=_SHANGHAI)]
    source = at[0].replace(hour=11, minute=30)
    gateway, market, _, directed = _gateway(at, source)
    assert tuple(gateway.fetch_candidates(("600001",), observed_at=at[0]))
    assert directed.calls == 1 and market.calls == 0
    assert tuple(gateway.fetch_market(observed_at=at[0]))
    assert market.calls == 1


def test_fixed_quote_reuse_keeps_expired_request_restricted_without_new_io():
    at = [datetime(2026, 11, 10, 12, 0, tzinfo=_SHANGHAI)]
    source = at[0].replace(hour=11, minute=30)
    gateway, market, _, directed = _gateway(at, source)
    assert tuple(gateway.fetch_market(observed_at=at[0]))
    retained = tuple(gateway.fetch_candidates(("600001",), observed_at=at[0], deadline=at[0]))
    assert retained[0].source_time == source
    assert "market_data_degraded" in retained[0].execution_restrictions
    assert market.calls == 1 and directed.calls == 0


def test_close_boundary_collects_actual_close_instead_of_pinning_last_intraday_quote():
    at = [datetime(2026, 11, 10, 14, 59, 59, tzinfo=_SHANGHAI)]
    intraday = replace(_quote(), source_time=at[0], received_time=at[0])
    closed_at = at[0].replace(hour=15, minute=0, second=0)
    close = replace(intraday, source_time=closed_at, received_time=closed_at, price=11.0, high=11.0)
    market = SequenceMarketClient(((intraday,), (close,)))
    gateway, _, _, _ = _gateway(at, intraday.source_time, market=market)
    assert tuple(gateway.fetch_market(observed_at=at[0]))[0].source_time == intraday.source_time
    at[0] = closed_at
    result = tuple(gateway.fetch_market(observed_at=at[0]))
    assert market.calls == 2
    assert result[0].source_time == closed_at
    assert tuple(gateway.fetch_market(observed_at=at[0])) == result
    assert market.calls == 2


def test_custom_sessions_reach_collection_merge_filters_and_source_health(application_feature_factory):
    sessions = QuoteSessionTimes(600, 730, 810, 885)
    at = [datetime(2026, 11, 10, 9, 45, tzinfo=_SHANGHAI)]
    source = at[0].replace(hour=12, minute=10)
    gateway, market, _, _ = _gateway(at, source, quote_sessions=sessions)
    assert tuple(gateway.fetch_market(observed_at=at[0])) == ()
    assert market.calls == 0
    at[0] = at[0].replace(hour=13, minute=20)
    quotes = tuple(gateway.fetch_market(observed_at=at[0], force=True))
    assert len(quotes) == 1 and quotes[0].source_time == source
    feature = replace(application_feature_factory("600001", at[0]), quote=quotes[0])
    policy = replace(HardFilterPolicyFixture(), quote_sessions=sessions)
    rule = next(rule for rule in LevelOneFilterRules(max_age_seconds=30, policy=policy)
                if rule.name == "invalid_quote_time")
    assert rule.predicate(feature, at[0]) is None
    assert DynamicSourceHealth((feature,), at[0], sessions=sessions).healthy_source_count == 1
    assert DynamicSourceHealth((feature,), at[0]).healthy_source_count == 0


def test_nontrading_day_acquires_previous_close_and_reuses_it_without_date_rewrite():
    at = [datetime(2026, 11, 8, 8, 0, tzinfo=_SHANGHAI)]
    close = datetime(2026, 11, 6, 15, 0, tzinfo=_SHANGHAI)
    context = PreviewContext("previous-close", date(2026, 11, 6), at[0])
    gateway, market, _, directed = _gateway(at, close)
    gateway.prepare_preview(context)
    first = tuple(gateway.fetch_market(observed_at=at[0], force=True))
    assert first and first[0].source_time == close
    assert tuple(gateway.fetch_market(observed_at=at[0])) == first
    assert tuple(gateway.fetch_long_quotes(("600001",), observed_at=at[0], preview_context=context)) == first
    assert market.calls == 1 and directed.calls == 0
