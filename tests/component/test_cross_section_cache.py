"""Shared population arithmetic must not mix facts, versions or caller overlays."""

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
from trader.infra.settings import LoadRuntimeSettings
from trader.recommendation.infra.normalization import feature_cross_section as cross_section
from trader.recommendation.infra.normalization.feature_cross_section import (
    BuildCrossSectionFeatures,
    CrossSectionReferences,
)
from trader.recommendation.infra.normalization.features import FeatureBuilder


@pytest.fixture
def population():
    quotes = tuple(
        replace(_quote(str(600001 + index)), industry="银行" if index < 2 else "医药", speed=float(index))
        for index in range(3)
    )
    raw = {
        quote.code: {name: float(index + 1) for name in cross_section._RAW_FIELDS}
        for index, quote in enumerate(quotes)
    }
    return quotes, raw


@pytest.fixture
def cache():
    policy = LoadRuntimeSettings(Path("config/runtime.json")).market_data.cache_policy
    return BoundedLruCache(policy, wall_clock=lambda: NOW)


def _build(quotes, raw, cache, version="market:1", references=None):
    return BuildCrossSectionFeatures(quotes, raw, version, references or CrossSectionReferences({}, {}), cache=cache)


def test_four_builders_share_population_and_preserve_fresh_snapshot_identity(cache, monkeypatch):
    quote = _quote()
    history = {quote.code: _history_bars()}
    horizons = ((20, 40, 60, 120, 250), (20, 40, 60), (20, 40, 60), (20, 40, 60))

    def builder(horizon, shared_cache):
        return FeatureBuilder(
            NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY,
            model_momentum_horizons=horizon, cache=shared_cache,
        )

    builders = [builder(horizon, cache) for horizon in horizons]
    expected = [
        builder(horizon, None).build((quote,), history, NOW + timedelta(seconds=tick))
        for tick, horizon in enumerate(horizons)
    ]
    original = cross_section._calculate_population
    calls = []

    def counted(*args):
        calls.append(1)
        return original(*args)

    monkeypatch.setattr(cross_section, "_calculate_population", counted)
    for tick, profile_builder in enumerate(builders):
        assert profile_builder.build((quote,), history, NOW + timedelta(seconds=tick)) == expected[tick]
    assert len(calls) == 1


@pytest.mark.parametrize("change", (*cross_section._RAW_FIELDS, "speed", "pct_change", "industry", "codes", "date", "version"))
def test_actual_population_dependencies_invalidate_cached_result(cache, population, monkeypatch, change):
    quotes, raw = population
    _build(quotes, raw, cache)
    if change in cross_section._RAW_FIELDS:
        raw[quotes[0].code][change] = 10.0
    elif change in {"speed", "pct_change", "industry"}:
        quotes = (replace(quotes[0], **{change: "科技" if change == "industry" else 10.0}), *quotes[1:])
    elif change == "codes":
        quotes = quotes[1:]
        raw = {quote.code: raw[quote.code] for quote in quotes}
    elif change == "date":
        quotes = tuple(replace(quote, source_time=quote.source_time + timedelta(days=1)) for quote in quotes)
    version = "market:2" if change == "version" else "market:1"
    expected = _build(quotes, raw, None, version)
    original = cross_section._calculate_population
    calls = []

    def counted(*args):
        calls.append(1)
        return original(*args)

    monkeypatch.setattr(cross_section, "_calculate_population", counted)
    assert _build(quotes, raw, cache, version) == expected
    assert len(calls) == 1


def test_shared_objects_ignore_delivery_and_model_fields_and_isolate_overrides(cache, population, monkeypatch):
    quotes, raw = population
    first = _build(quotes, raw, cache)
    code = quotes[0].code
    quotes = tuple(replace(quote, received_time=NOW + timedelta(seconds=2)) for quote in quotes)
    raw[code]["qfq_skip5_momentum_250d"] = 0.3

    def forbidden(*args):
        raise AssertionError("identical population facts must reuse cached arithmetic")

    monkeypatch.setattr(cross_section, "_calculate_population", forbidden)
    second = _build(quotes, raw, cache)
    assert second[code].values is first[code].values
    assert all(row.normalization is first[code].normalization for row in second.values())
    with pytest.raises(TypeError):
        second[code].values["market_breadth"] = 0.0
    with pytest.raises(TypeError):
        second[code].normalization["market_breadth"] = None
    stat = replace(first[code].normalization["market_breadth"], population_data_version="reference")
    partial = CrossSectionReferences({code: {"market_breadth": None}}, {code: {"market_breadth": stat}})
    overlaid = _build(quotes, raw, cache, references=partial)
    assert overlaid[code].values["market_breadth"] is None
    assert overlaid[code].normalization["market_breadth"] is stat
    assert _build(quotes, raw, cache) == first
    # Complete references bypass calculation even for another candidate population.
    complete = CrossSectionReferences(
        {code: overlaid[code].values}, {code: overlaid[code].normalization},
    )
    assert _build(quotes[:1], {}, cache, references=complete)[code] == overlaid[code]


def test_nonfinite_missing_values_share_the_same_safe_result(cache, population):
    quotes, raw = population
    code = quotes[0].code
    raw[code]["return_3d"] = None
    missing = _build(quotes, raw, cache)
    raw[code]["return_3d"] = float("nan")
    nonfinite = _build(quotes, raw, cache)
    assert nonfinite == _build(quotes, raw, None) == missing
    assert nonfinite[code].values is missing[code].values


def test_concurrent_profiles_compute_identical_population_once(cache, population, monkeypatch):
    quotes, raw = population
    original = cross_section._calculate_population
    entered, release, joined = threading.Event(), threading.Event(), threading.Event()
    calls = []
    claim = cache._claim_inflight
    join_count = []

    def observed_claim(identity):
        result = claim(identity)
        if not result[1]:
            join_count.append(1)
            if len(join_count) == 3:
                joined.set()
        return result

    def blocked(*args):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(cache, "_claim_inflight", observed_claim)
    monkeypatch.setattr(cross_section, "_calculate_population", blocked)
    with ThreadPoolExecutor(max_workers=4) as executor:
        owner = executor.submit(_build, quotes, raw, cache)
        assert entered.wait(5)
        waiters = [executor.submit(_build, quotes, raw, cache) for _ in range(3)]
        try:
            assert joined.wait(5)
        finally:
            release.set()
        first = owner.result(5)
        for waiter in waiters:
            assert waiter.result(5)[quotes[0].code].values is first[quotes[0].code].values
    assert len(calls) == 1


def test_population_cache_is_bounded_and_budget_rejection_keeps_outputs(cache, population):
    quotes, raw = population
    expected = _build(quotes, raw, None)
    for tick in range(6):
        _build(quotes, raw, cache, f"market:{tick}")
    stats = cache.status().datasets["market_cross_section"]["local_factors"]
    assert stats.entries == 4
    assert stats.eviction == 2
    policy = LoadRuntimeSettings(Path("config/runtime.json")).market_data.cache_policy
    groups = {**policy.groups, "feature_batches": replace(policy.groups["feature_batches"], max_bytes=1)}
    total = sum(group.max_bytes for group in groups.values())
    tiny = BoundedLruCache(
        replace(policy, groups=groups, total_bytes=total, pool_total_bytes=total + policy.runtime_reserve_bytes),
        wall_clock=lambda: NOW,
    )
    assert _build(quotes, raw, tiny) == expected
    assert tiny.status().datasets["market_cross_section"]["local_factors"].entries == 0
