"""An explicit replay candidate preserves facts, fresh projections and shared limits."""

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from tests.helpers.recommendation.market_data import (
    AFTERNOON,
    FEATURE_WEIGHT_POLICY,
    LONG_POLICY,
    MARKET_REGIME_POLICY,
    NEWS_POLICY,
    NOW,
    TAIL_POLICY,
    _history_bars,
    _history_price_basis,
    _quote,
    _tail_minute_bars,
)
from trader.infra.cache import BoundedLruCache
from trader.recommendation.domain.market.history_metrics import BuildHistoryContext, HistoryAdjustmentError, PriceAdjustment
from trader.infra.settings import LoadRuntimeSettings
from trader.recommendation.domain.market.research import CorporateRiskCategory, CorporateRiskFact, FinancialReport, ResearchObservation
from trader.recommendation.infra.normalization.features import FeatureBuilder
from trader.recommendation.infra.performance.raw_factor_reuse import RAW_FACTOR_REUSE_CANDIDATE, RawFactorReuseBuilder


def _builder(cache=None, *, horizons=(20, 40, 60), weights=FEATURE_WEIGHT_POLICY, candidate=True):
    builder = RawFactorReuseBuilder if candidate else FeatureBuilder
    return builder(
        NEWS_POLICY,
        TAIL_POLICY,
        MARKET_REGIME_POLICY,
        LONG_POLICY,
        weights,
        model_momentum_horizons=horizons,
        cache=cache,
    )


@pytest.fixture
def cache():
    policy = LoadRuntimeSettings(Path("config/runtime.json")).market_data.cache_policy
    result = BoundedLruCache(policy, wall_clock=lambda: NOW)
    yield result
    result.stop()


def test_four_builders_compute_equal_raw_facts_once_and_keep_fresh_snapshots(cache, monkeypatch):
    quote = _quote()
    bars = _history_bars()
    context = BuildHistoryContext(bars)
    histories = {quote.code: bars}
    bases = {quote.code: _history_price_basis(10.59, 10.59)}
    calls = []
    original = FeatureBuilder._raw_features

    def counted(self, *args):
        calls.append(args[0].code)
        return original(self, *args)

    monkeypatch.setattr(FeatureBuilder, "_raw_features", counted)
    actual = tuple(
        _builder(cache).build((quote,), histories, NOW + timedelta(seconds=index), history_summaries={quote.code: context}, history_price_bases=bases)
        for index in range(4)
    )
    assert calls == [quote.code]
    assert cache.status().datasets["quote_factor_inputs"][RAW_FACTOR_REUSE_CANDIDATE].entries == 1
    monkeypatch.setattr(FeatureBuilder, "_raw_features", original)
    for index, snapshots in enumerate(actual):
        at = NOW + timedelta(seconds=index)
        assert snapshots == _builder(candidate=False).build((quote,), histories, at, history_summaries={quote.code: context}, history_price_bases=bases)
        assert snapshots[0].observed_at == snapshots[0].evidence[0].published_at == at
    assert _builder(cache)._raw_features(quote, bars, context)["news_sentiment"] is None


def test_reused_raw_facts_do_not_hide_new_company_risk_financial_loss_or_minutes(cache, monkeypatch):
    quote, bars = _quote(), _history_bars()
    original = FeatureBuilder._raw_features
    calls = []

    def counted(self, *args):
        calls.append(1)
        return original(self, *args)

    observation = ResearchObservation(
        financial=FinancialReport(NOW.date(), NOW + timedelta(seconds=1), parent_net_profit=-1.0, core_net_profit=-1.0),
        corporate_risk_window_complete=True,
        corporate_risk_facts=(CorporateRiskFact(
            CorporateRiskCategory.OFFICIAL_INVESTIGATION, NOW + timedelta(seconds=1), "case-1", "exchange_disclosure",
        ),),
    )
    builder = _builder(cache)
    bases = {quote.code: _history_price_basis(10.59, 10.59)}
    monkeypatch.setattr(FeatureBuilder, "_raw_features", counted)
    first = builder.build((quote,), {quote.code: bars}, NOW, history_price_bases=bases)
    options = {"research_observations": {quote.code: observation}, "intraday_minutes": {quote.code: _tail_minute_bars()}, "history_price_bases": bases}
    actual = builder.build((quote,), {quote.code: bars}, AFTERNOON, **options)
    assert calls == [1]
    monkeypatch.setattr(FeatureBuilder, "_raw_features", original)
    assert actual == _builder(candidate=False).build((quote,), {quote.code: bars}, AFTERNOON, **options)
    assert first[0].values["latest_disclosed_report_loss"] is None
    assert actual[0].values["latest_disclosed_report_loss"] == 1.0
    assert actual[0].values["official_investigation_history"] == 1.0
    assert actual[0].values["tail_return_30m"] is not None


def test_signed_zero_inputs_retain_distinct_canonical_identities(cache):
    bars = _history_bars()
    for amount in (0.0, -0.0):
        quote = replace(_quote(), amount=amount)
        actual = _builder(cache)._raw_features(quote, bars)
        expected = _builder(candidate=False)._raw_features(quote, bars)
        assert {name: value.hex() if isinstance(value, float) else value for name, value in actual.items()} == {
            name: value.hex() if isinstance(value, float) else value for name, value in expected.items()
        }
    assert cache.status().datasets["quote_factor_inputs"][RAW_FACTOR_REUSE_CANDIDATE].entries == 2


@pytest.mark.parametrize("change", ("price", "delivery", "version", "date", "bars", "summary", "horizons", "policy"))
def test_every_input_or_policy_change_recomputes_and_matches_original(cache, monkeypatch, change):
    quote, bars = _quote(), _history_bars()
    context = BuildHistoryContext(bars)
    _builder(cache)._raw_features(quote, bars, context)
    horizons = (20, 40, 60)
    weights = FEATURE_WEIGHT_POLICY
    if change == "price":
        quote = replace(quote, price=quote.price + 0.1)
    elif change == "delivery":
        quote = replace(quote, received_time=quote.received_time + timedelta(seconds=1))
    elif change == "version":
        quote = replace(quote, data_version="later")
    elif change == "date":
        quote = replace(quote, source_time=quote.source_time + timedelta(days=1))
    elif change == "bars":
        bars = (*bars[:-1], replace(bars[-1], high=bars[-1].high + 1.0))
    elif change == "summary":
        context = replace(context, profile=replace(context.profile, median_amount_20d=1.0))
    elif change == "horizons":
        horizons = (20, 60, 250)
    else:
        weights = replace(weights, trend_score={"ma20_60_position": 1.0, "ma_slope": 0.0})
    calls = []
    original = FeatureBuilder._raw_features

    def counted(self, *args):
        calls.append(args[0].code)
        return original(self, *args)

    expected = _builder(horizons=horizons, weights=weights, candidate=False)._raw_features(quote, bars, context)
    monkeypatch.setattr(FeatureBuilder, "_raw_features", counted)
    builder = _builder(cache, horizons=horizons, weights=weights)
    assert builder._raw_features(quote, bars, context) == expected
    assert builder._raw_features(quote, bars, context) == expected
    assert calls == [quote.code]


def test_concurrent_profiles_coalesce_without_sharing_mutable_outputs(cache, monkeypatch):
    entered, release, joined = threading.Event(), threading.Event(), threading.Event()
    original = FeatureBuilder._raw_features
    claim = cache._claim_inflight
    calls = []
    quote, bars = _quote(), _history_bars()

    def counted(self, *args):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return original(self, *args)

    def observed_claim(identity):
        result = claim(identity)
        if identity.source == RAW_FACTOR_REUSE_CANDIDATE and not result[1]:
            joined.set()
        return result

    monkeypatch.setattr(FeatureBuilder, "_raw_features", counted)
    monkeypatch.setattr(cache, "_claim_inflight", observed_claim)
    with ThreadPoolExecutor(max_workers=4) as executor:
        owner = executor.submit(_builder(cache)._raw_features, quote, bars)
        assert entered.wait(5)
        waiters = [executor.submit(_builder(cache)._raw_features, quote, bars) for _ in range(3)]
        try:
            assert joined.wait(5)
        finally:
            release.set()
        outputs = [owner.result(5), *(waiter.result(5) for waiter in waiters)]
    assert len(calls) == 1 and all(output == outputs[0] for output in outputs)
    outputs[0]["return_5d"] = -999.0
    assert all(output["return_5d"] != -999.0 for output in outputs[1:])


@pytest.mark.parametrize("failure", (RuntimeError("fixture_failure"), KeyboardInterrupt()))
def test_failure_or_cancellation_leaves_no_cached_result(cache, monkeypatch, failure):
    original = FeatureBuilder._raw_features
    quote, bars = _quote(), _history_bars()

    def interrupted(*args):
        raise failure

    monkeypatch.setattr(FeatureBuilder, "_raw_features", interrupted)
    with pytest.raises(type(failure)):
        _builder(cache)._raw_features(quote, bars)
    stats = cache.status().datasets.get("quote_factor_inputs", {}).get(RAW_FACTOR_REUSE_CANDIDATE)
    assert stats is None or stats.entries == 0
    monkeypatch.setattr(FeatureBuilder, "_raw_features", original)
    assert _builder(cache)._raw_features(quote, bars) == _builder(candidate=False)._raw_features(quote, bars)


def test_nonfinite_unused_input_preserves_original_calculation_and_raw_prices_remain_rejected(cache):
    quote, bars = _quote(), _history_bars()
    nonfinite = (replace(bars[0], open_price=float("nan")), *bars[1:])
    context = BuildHistoryContext(bars)
    assert _builder(cache)._raw_features(quote, nonfinite, context) == _builder(candidate=False)._raw_features(quote, nonfinite, context)
    assert RAW_FACTOR_REUSE_CANDIDATE not in cache.status().datasets.get("quote_factor_inputs", {})
    raw = (replace(bars[0], adjustment=PriceAdjustment.RAW), *bars[1:])
    with pytest.raises(HistoryAdjustmentError):
        _builder(cache).build(
            (quote,), {quote.code: raw}, NOW,
            history_price_bases={quote.code: _history_price_basis(10.59, 10.59)},
        )


def test_shared_budget_rejection_calculates_without_allocating_another_cache():
    policy = LoadRuntimeSettings(Path("config/runtime.json")).market_data.cache_policy
    groups = {**policy.groups, "feature_batches": replace(policy.groups["feature_batches"], max_bytes=1)}
    total = sum(group.max_bytes for group in groups.values())
    tiny = BoundedLruCache(
        replace(policy, groups=groups, total_bytes=total, pool_total_bytes=total + policy.runtime_reserve_bytes),
        wall_clock=lambda: NOW,
    )
    quote, bars = _quote(), _history_bars()
    try:
        assert _builder(tiny)._raw_features(quote, bars) == _builder(candidate=False)._raw_features(quote, bars)
        assert tiny.status().datasets["quote_factor_inputs"][RAW_FACTOR_REUSE_CANDIDATE].entries == 0
    finally:
        tiny.stop()
