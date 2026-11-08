"""Raw quotes and frozen adjusted history require an observed paired price coordinate."""

from dataclasses import replace
from datetime import date, timedelta
from unittest.mock import Mock

import pytest

from tests.helpers.recommendation.market_data import (
    AFTERNOON, FEATURE_WEIGHT_POLICY, LONG_POLICY, MARKET_REGIME_POLICY, NEWS_POLICY, TAIL_POLICY,
    _history_price_basis, _quote, _tail_minute_bars,
)
from tests.helpers.recommendation.market.candidate_history_tail import HistoryInput, NOW
from tests.helpers.recommendation.publication.shared_history_projection import _cache
from trader.download.domain.published_history import PublishedHistoryCell, PublishedHistoryWindow
from trader.infra.cache import BoundedLruCache
from trader.recommendation.domain.market.history_metrics import BuildHistoryContext, DailyBar, PriceAdjustment
from trader.infra.settings import LoadRuntimeSettings
from trader.recommendation.domain.market.price_basis import HistoryPriceAnchor, HistoryPriceBasis, ResolveHistoryPriceScale
from trader.recommendation.domain.publication.preview import PreviewContext
from trader.recommendation.infra.market_data.aligned_history_bars import BuildAlignedHistoryBars
from trader.recommendation.infra.market_data.candidate_quote_cache import QuoteCache, QuoteCacheDependencies
from trader.recommendation.infra.market_data.gateway import MarketDataGateway
from trader.recommendation.infra.market_data.published_references import PublishedReferences
from trader.recommendation.infra.normalization.features import FeatureBuilder
from trader.recommendation.infra.normalization.history_price_basis import RebaseHistoryPrices


def _builder(cache=None):
    return FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY, cache=cache)


def _bars():
    return tuple(DailyBar(
        (AFTERNOON.date() - timedelta(days=61-index)).isoformat(),
        10, 10, 10.2, 9.8, 1_000_000, 100_000_000, 0,
        adjustment=PriceAdjustment.QFQ, source="fixture",
    ) for index in range(61))


def test_every_history_price_comparison_uses_raw_coordinate_and_preserves_quote_and_flow():
    bars = _bars()
    context = BuildHistoryContext(bars)
    original = (bars, context)
    raw_bars, raw_context = RebaseHistoryPrices(bars, context, 2)
    quote = replace(_quote(), source_time=AFTERNOON, received_time=AFTERNOON, price=21, previous_close=20,
                    open_price=20.5, high=21.5, low=20, volume=1_000_000)
    options = {"intraday_minutes": {quote.code: _tail_minute_bars()}}
    actual = _builder().build(
        (quote,), {quote.code: bars[-20:]}, AFTERNOON, history_summaries={quote.code: context},
        history_price_bases={quote.code: _history_price_basis(20, 10)}, **options,
    )[0]
    expected = _builder().build(
        (quote,), {quote.code: raw_bars[-20:]}, AFTERNOON, history_summaries={quote.code: raw_context},
        history_price_bases={quote.code: _history_price_basis(20, 20)}, **options,
    )[0]
    assert actual == expected and actual.quote is quote
    assert actual.model_history_quality is None
    assert actual.optional_value("qfq_return_1d") == pytest.approx(0.05)
    assert actual.optional_value("return_3d") == pytest.approx(5)
    assert actual.optional_value("ma20") == 20
    assert actual.optional_value("prior_high_20d") == pytest.approx(20.4)
    assert actual.optional_value("qfq_average_amount_20d") == 100_000_000
    assert raw_bars[-1].volume == bars[-1].volume and raw_bars[-1].amount == bars[-1].amount
    assert original == (bars, context) and bars[-1].close == 10
    assert actual.quote.source_time == AFTERNOON and actual.quote.data_version == quote.data_version


@pytest.mark.parametrize("previous", (5, 0, -1, float("nan"), float("inf"), None))
def test_changed_or_invalid_previous_close_never_guesses_an_adjustment_factor(previous):
    quote = replace(_quote(), price=5, previous_close=previous, pct_change=0)
    feature = _builder().build(
        (quote,), {quote.code: _bars()}, AFTERNOON,
        history_price_bases={quote.code: _history_price_basis(10, 10)},
    )[0]
    assert feature.quote is quote and feature.quote.price == 5 and feature.quote.pct_change == 0
    assert feature.model_history_quality.failure == "history_price_basis_unavailable"
    assert feature.optional_value("qfq_return_1d") is None
    assert feature.optional_value("return_3d") is None and feature.optional_value("ma20") is None
    assert feature.missing_reasons["history_price_basis"] == "history_price_basis_unavailable"


def test_missing_pair_or_conflicting_history_never_defaults_to_unit_scale():
    quote = replace(_quote(), previous_close=10)
    for bases in ({}, {quote.code: _history_price_basis(10, 9)}):
        feature = _builder().build((quote,), {quote.code: _bars()}, AFTERNOON, history_price_bases=bases)[0]
        assert feature.model_history_quality.failure == "history_price_basis_unavailable"
        assert feature.optional_value("qfq_return_1d") is None


def test_same_day_anchor_requires_a_real_close_quote_and_preview_keeps_its_market_day():
    previous, day = date(2026, 7, 15), date(2026, 7, 16)
    basis = HistoryPriceBasis(day, previous, (
        HistoryPriceAnchor(previous, 100, 50), HistoryPriceAnchor(day, 50, 50),
    ), "paired-snapshot")
    quote = replace(_quote(), price=50, previous_close=50, source_time=AFTERNOON)
    closes = {previous: 50, day: 50}
    assert ResolveHistoryPriceScale(quote, basis, observed_at=AFTERNOON.replace(hour=17), historical_closes=closes) is None
    closing = replace(quote, source_time=AFTERNOON.replace(hour=15, minute=0))
    assert ResolveHistoryPriceScale(closing, basis, observed_at=AFTERNOON.replace(hour=17), historical_closes=closes) == 1
    assert ResolveHistoryPriceScale(replace(closing, price=49), basis, observed_at=AFTERNOON.replace(hour=17), historical_closes=closes) is None
    weekend = AFTERNOON.replace(day=18)
    preview = PreviewContext("fixture-preview", day, weekend)
    assert ResolveHistoryPriceScale(closing, basis, observed_at=weekend, historical_closes=closes, preview_context=preview) == 1
    assert ResolveHistoryPriceScale(quote, basis, observed_at=weekend, historical_closes=closes, preview_context=preview) is None
    assert ResolveHistoryPriceScale(replace(closing, is_suspended=True), basis, observed_at=weekend, historical_closes=closes, preview_context=preview) is None


def test_estimated_or_unpaired_completed_session_cannot_supply_the_price_anchor():
    source = HistoryInput(61, 0, ("600001",))
    dates = source.current.calendar_dates[-3:]
    window = source.window("600001", dates)
    missing = PublishedHistoryCell("600001", dates[-1], "unknown_missing", None, None)
    aligned = BuildAlignedHistoryBars(PublishedHistoryWindow("600001", (*window.cells[:-1], missing)), dates)
    assert aligned.bars[-1].trade_date == dates[-1].isoformat()
    assert dates[-1] in aligned.estimated_dates
    assert all(anchor.trade_date != dates[-1] for anchor in aligned.price_anchors)
    basis = HistoryPriceBasis(
        NOW.date(), dates[-1], tuple(a for a in aligned.price_anchors if a.trade_date == dates[-1]),
        "paired-snapshot",
    )
    quote = replace(_quote(), source_time=NOW, previous_close=window.cells[-2].unadjusted.close_price)
    assert ResolveHistoryPriceScale(quote, basis, observed_at=NOW, historical_closes={d: 10 for d in dates}) is None


class _HalfQfqHistory(HistoryInput):
    @staticmethod
    def window(code, dates):
        original = HistoryInput.window(code, dates)
        return replace(original, cells=tuple(replace(cell, qfq=replace(
            cell.qfq, open_price=cell.qfq.open_price / 2, close_price=cell.qfq.close_price / 2,
            high_price=cell.qfq.high_price / 2, low_price=cell.qfq.low_price / 2,
        )) for cell in original.cells))


@pytest.mark.parametrize("sessions,rank,sequence", ((251, True, False), (61, False, True)))
def test_real_preload_and_quote_cache_keep_completed_v1_v5_inputs_but_preserve_price_failure(sessions, rank, sequence):
    source = _HalfQfqHistory(252, 0, ("600001",))
    history = _cache(source, sessions, rank=rank, sequence=sequence)
    history.initialize(cancelled=lambda: False)
    entry = history.entries()["600001"]
    completed = history.completed_model_inputs(("600001",), NOW)[0]
    assert completed.quality.failure is None
    basis = history.price_bases(("600001",), NOW)["600001"]
    anchor = next(a for a in basis.anchors if a.trade_date == basis.previous_trade_date)
    assert anchor.raw_close / anchor.qfq_close == 2
    quote = replace(_quote(), source_time=NOW, received_time=NOW, previous_close=anchor.raw_close, price=anchor.raw_close * 1.01)
    gateway, references = Mock(spec=MarketDataGateway), Mock(spec=PublishedReferences)
    references.model_industries.return_value = {}
    cache = QuoteCache(QuoteCacheDependencies(gateway, _builder(), history, references),
                       market_ttl_seconds=1, candidate_capacity=10, monotonic=lambda: 0)
    def build(value):
        return cache.build_market_features((value,), {"600001": entry.bars}, NOW, action_restrictions={})[0]
    good = build(quote)
    bad = build(replace(quote, previous_close=anchor.raw_close / 2))
    recovered = build(quote)
    assert good.model_history_quality.failure is None
    assert bad.model_history_quality.failure == "history_price_basis_unavailable"
    assert recovered == good
    assert good.quote.price == bad.quote.price == quote.price
    assert bad.model_sequence is completed.sequence and recovered.model_sequence is completed.sequence
    for name, value in completed.learned_rank_values:
        assert bad.values[name] == recovered.values[name] == value
    assert history.entries()["600001"] is entry
    assert history.completed_model_inputs(("600001",), NOW)[0] == completed


def test_equal_price_with_changed_previous_close_cannot_reuse_cached_factors():
    policy = LoadRuntimeSettings("config/runtime.json").market_data.cache_policy
    cache = BoundedLruCache(policy, wall_clock=lambda: AFTERNOON)
    try:
        builder = _builder(cache)
        quote = replace(_quote(), price=10.1, previous_close=10)
        bases = {quote.code: _history_price_basis(10, 10)}
        def build(previous):
            return builder.build((replace(quote, previous_close=previous),), {quote.code: _bars()}, AFTERNOON,
                                 history_price_bases=bases)[0]
        first, changed, restored = build(10), build(5), build(10)
        assert first.optional_value("qfq_return_1d") == pytest.approx(0.01)
        assert changed.optional_value("qfq_return_1d") is None
        assert changed.model_history_quality.failure == "history_price_basis_unavailable"
        assert restored == first
    finally:
        cache.stop()
