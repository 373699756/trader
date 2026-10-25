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
from trader.recommendation.infra.normalization import factor_history


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


@pytest.mark.parametrize("published", (False, True))
def test_quote_changes_reuse_prepared_history_without_repeating_daily_statistics(factor_cache, monkeypatch, published):
    bars = _history_bars()
    context = BuildHistoryContext(bars) if published else None
    builder = _builder(factor_cache)
    calls = []
    original = factor_history.SummarizeHistoryMetrics

    def counted(history):
        calls.append(len(history))
        return original(history)

    monkeypatch.setattr(factor_history, "SummarizeHistoryMetrics", counted)
    for index in range(10):
        quote = replace(_quote(), price=12.0 + index / 10)
        got = builder._raw_features(quote, bars, context)
        assert got["ma20"] == sum(bar.close for bar in bars[-20:]) / 20
    assert len(calls) == (0 if published else 1)


def test_long_model_anchor_change_does_not_rebuild_common_base_factors(factor_cache, monkeypatch):
    bars = _history_bars()
    context = replace(BuildHistoryContext(bars), return_anchors=(*BuildHistoryContext(bars).return_anchors, (249, 8.0)))
    builder = FeatureBuilder(
        NEWS_POLICY,
        TAIL_POLICY,
        MARKET_REGIME_POLICY,
        LONG_POLICY,
        FEATURE_WEIGHT_POLICY,
        model_momentum_horizons=(20, 40, 60, 120, 250),
        cache=factor_cache,
    )
    first = builder._raw_features(_quote(), bars[-20:], context)

    def forbidden(*args):
        raise AssertionError("long model anchors must not invalidate the shared base")

    monkeypatch.setattr(builder, "_base_features", forbidden)
    corrected = replace(
        context, return_anchors=tuple((lag, 9.0 if lag == 249 else value) for lag, value in context.return_anchors)
    )
    second = builder._raw_features(_quote(), bars[-20:], corrected)
    assert first["return_20d"] == second["return_20d"]
    assert first != second


def test_unused_bar_fields_do_not_retain_raw_history_in_published_factor_cache(factor_cache, monkeypatch):
    bars = _history_bars()
    context = BuildHistoryContext(bars)
    builder = _builder(factor_cache)
    first = builder._raw_features(_quote(), bars[-20:], context)

    def forbidden(*args):
        raise AssertionError("unused raw metadata changed the numerical history inputs")

    monkeypatch.setattr(builder, "_base_features", forbidden)
    corrected = tuple(replace(bar, source="new-publication", open_price=bar.open_price + 1) for bar in bars[-20:])
    assert builder._raw_features(_quote(), corrected, context) == first
    # At this measured per-stock payload, all 5500 stocks fit the existing group budget.
    stats = factor_cache.status().datasets["quote_factor_inputs"]["local_history"]
    assert stats.estimated_bytes * 5500 < 51380224


def test_complete_market_base_factors_fit_existing_budget_and_reuse_every_stock(factor_cache, monkeypatch):
    bars = _history_bars()
    context = BuildHistoryContext(bars)
    builder = _builder(factor_cache)
    quotes = tuple(replace(_quote(str(600000 + index)), price=12.0 + index / 1000) for index in range(5500))
    for quote in quotes:
        builder._raw_features(quote, bars[-20:], context)
    cold = factor_cache.status().datasets["quote_factor_inputs"]["local_history"]
    assert cold.entries == 5500
    assert cold.eviction == 0
    assert cold.estimated_bytes < 51380224

    def forbidden(*args):
        raise AssertionError("full-market warm traversal evicted an unchanged stock")

    monkeypatch.setattr(builder, "_base_features", forbidden)
    for quote in quotes:
        got = builder._raw_features(quote, bars[-20:], context)
        assert got["return_5d"] == context.return_pct(5, quote.price, NOW.date().isoformat())
    warm = factor_cache.status().datasets["quote_factor_inputs"]["local_history"]
    assert warm.entries == cold.entries
    assert warm.estimated_bytes == cold.estimated_bytes
    assert warm.eviction == 0
