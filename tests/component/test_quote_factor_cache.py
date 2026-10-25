"""Base arithmetic reuse must preserve fresh full features and corrected facts."""

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from tests.component.market_data_test_support import (
    FEATURE_WEIGHT_POLICY,
    LONG_POLICY,
    MARKET_REGIME_POLICY,
    NEWS_POLICY,
    NOW,
    TAIL_POLICY,
    _history_bars,
    _quote,
)
from trader.infra.cache import BoundedLruCache
from trader.infra.market_data.history.history import BuildHistoryContext
from trader.infra.settings import LoadRuntimeSettings
from trader.recommendation.infra.normalization.features import FeatureBuilder


def _builder(cache=None):
    return FeatureBuilder(
        NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY, cache=cache
    )


@pytest.fixture
def factor_cache():
    policy = LoadRuntimeSettings(Path("config/runtime.json")).market_data.cache_policy
    return BoundedLruCache(policy, wall_clock=lambda: NOW)


@pytest.mark.parametrize(
    ("changes", "recomputes"),
    (
        ({"received_time": NOW + timedelta(seconds=1)}, False),
        ({"source_time": NOW + timedelta(seconds=1)}, False),
        ({"data_version": "corrected"}, False),
        ({"source": "fallback"}, False),
        ({"is_st": True, "cross_source_verified": False, "execution_restrictions": ("buy_blocked",)}, False),
        ({"speed": 3.0, "industry": "银行", "turnover_rate": 5.0}, False),
        ({"price": 12.1}, True),
        ({"high": 12.8}, True),
        ({"low": 11.0}, True),
        ({"pct_change": 6.0}, True),
        ({"amount": 100_000_000.0}, True),
        ({"amplitude": 8.0}, True),
        ({"volume": 2_000_000.0}, True),
        ({"exchange_limit_pct": 20.0}, True),
        ({"has_price_limit": False}, True),
        ({"source_time": NOW + timedelta(days=1)}, True),
    ),
)
def test_cached_full_features_match_uncached_after_quote_changes(factor_cache, monkeypatch, changes, recomputes):
    builder = _builder(factor_cache)
    original = builder._base_features
    calls = []

    def counted(*args):
        calls.append(args[0].code)
        return original(*args)

    monkeypatch.setattr(builder, "_base_features", counted)
    quote = replace(_quote(), volume=1_000_000.0)
    history = {quote.code: _history_bars()}
    builder.build((quote,), history, NOW)
    changed = replace(quote, **changes)
    at = max(NOW + timedelta(seconds=1), changed.source_time)
    got = builder.build((changed,), history, at)
    expected = _builder().build((changed,), history, at)
    assert got == expected
    assert got[0].quote == changed
    assert got[0].observed_at == got[0].evidence[0].published_at == at
    assert len(calls) == 1 + recomputes


def test_history_corrections_and_policy_changes_invalidate_equal_quotes(factor_cache, monkeypatch):
    builder = _builder(factor_cache)
    quote = _quote()
    bars = _history_bars()
    context = BuildHistoryContext(bars)
    original = builder._base_features
    calls = []

    def counted(*args):
        calls.append(args[0].code)
        return original(*args)

    monkeypatch.setattr(builder, "_base_features", counted)
    builder._raw_features(quote, bars[-20:], context)
    corrected_context = replace(context, profile=replace(context.profile, median_amount_20d=50_000_000.0))
    assert builder._raw_features(quote, bars[-20:], corrected_context) == _builder()._raw_features(
        quote, bars[-20:], corrected_context
    )
    corrected_bars = (*bars[-20:-1], replace(bars[-1], high=100.0))
    assert builder._raw_features(quote, corrected_bars, corrected_context) == _builder()._raw_features(
        quote, corrected_bars, corrected_context
    )
    assert len(calls) == 3
    policy = replace(FEATURE_WEIGHT_POLICY, trend_score={"ma20_60_position": 1.0, "ma_slope": 0.0})
    changed = FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, policy, cache=factor_cache)
    uncached = FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, policy)
    assert changed._raw_features(quote, bars[-20:], context) == uncached._raw_features(quote, bars[-20:], context)
    assert factor_cache.status().datasets["quote_factor_inputs"]["local_history"].entries == 2


def test_timestamp_updates_keep_one_bounded_entry_per_stock(factor_cache):
    builder = _builder(factor_cache)
    quote = _quote()
    bars = _history_bars()
    for tick in range(105):
        at = NOW + timedelta(seconds=tick)
        current = replace(quote, source_time=at, received_time=at, data_version=f"delivery:{tick}")
        assert builder._raw_features(current, bars) == _builder()._raw_features(current, bars)
    assert factor_cache.status().datasets["quote_factor_inputs"]["local_history"].entries == 1


@pytest.mark.parametrize("same_facts", (False, True))
def test_concurrent_input_join_rechecks_facts_and_keeps_newer_cache(factor_cache, monkeypatch, same_facts):
    builder = _builder(factor_cache)
    original = builder._base_features
    newer = replace(_quote(), source_time=NOW + timedelta(seconds=1), received_time=NOW + timedelta(seconds=1))
    older = replace(_quote(), price=newer.price if same_facts else 13.0)
    bars = _history_bars()
    entered, joined, release = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def blocked(*args):
        calls.append(args[0].price)
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)
        return original(*args)

    claim = factor_cache._claim_inflight

    def observed_claim(identity):
        result = claim(identity)
        if not result[1]:
            joined.set()
        return result

    monkeypatch.setattr(builder, "_base_features", blocked)
    monkeypatch.setattr(factor_cache, "_claim_inflight", observed_claim)
    with ThreadPoolExecutor(max_workers=2) as executor:
        owner = executor.submit(builder._raw_features, newer, bars)
        assert entered.wait(3)
        waiter = executor.submit(builder._raw_features, older, bars)
        try:
            assert joined.wait(3)
        finally:
            release.set()
        assert owner.result(3) == _builder()._raw_features(newer, bars)
        assert waiter.result(3) == _builder()._raw_features(older, bars)
    assert len(calls) == (1 if same_facts else 2)
    count = len(calls)
    assert builder._raw_features(newer, bars) == _builder()._raw_features(newer, bars)
    assert len(calls) == count
