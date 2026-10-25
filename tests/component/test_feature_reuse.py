"""Candidate references and industry reads keep one accepted market population."""

from dataclasses import replace
from datetime import timedelta

from tests.component.market_data_test_support import (
    FEATURE_WEIGHT_POLICY,
    LONG_POLICY,
    MARKET_REGIME_POLICY,
    NEWS_POLICY,
    NOW,
    TAIL_POLICY,
    StaticGateway,
    StaticHistoryClient,
    _history_bars,
    _quote,
    _service,
)
from trader.recommendation.domain.market.models import Evidence
from trader.recommendation.infra.normalization import feature_cross_section
from trader.recommendation.infra.normalization.features import FeatureBuilder


def _builder():
    return FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY)


def test_candidate_full_reference_reuses_population_values_and_metadata_without_sorting(monkeypatch):
    builder = _builder()
    quotes = tuple(replace(_quote(f"60000{index}"), speed=float(index)) for index in range(4))
    histories = {quote.code: _history_bars() for quote in quotes}
    market = builder.build(quotes, histories, NOW)
    reference = {item.quote.code: item.values for item in market}
    normalization = {item.quote.code: item.normalization for item in market}

    def forbidden(*args, **kwargs):
        raise AssertionError("candidate read recalculated an accepted population percentile")

    monkeypatch.setattr(feature_cross_section, "PercentileScoresWithMetadata", forbidden)
    monkeypatch.setattr(feature_cross_section, "_industry_scores", forbidden)
    changed_quotes = tuple(
        replace(quote, speed=99.0, price=13.0, received_time=NOW + timedelta(seconds=1)) for quote in quotes[:2]
    )
    candidates = builder.build(
        changed_quotes,
        histories,
        NOW + timedelta(seconds=1),
        cross_section_reference=reference,
        cross_section_normalization_reference=normalization,
    )
    for feature in candidates:
        expected = next(item for item in market if item.quote.code == feature.quote.code)
        assert feature.values["speed_percentile"] == expected.values["speed_percentile"]
        assert feature.values["relative_strength_5d"] == expected.values["relative_strength_5d"]
        assert feature.values["industry_breadth"] == expected.values["industry_breadth"]
        assert feature.normalization == expected.normalization
        assert feature.normalization["speed_percentile"].sample_size == 4
        assert feature.values["return_5d"] != expected.values["return_5d"]
        assert feature.quote.received_time == NOW + timedelta(seconds=1)
    revised = {code: {**values, "industry_strength": None} for code, values in reference.items()}
    refreshed = builder.build(
        changed_quotes,
        histories,
        NOW,
        cross_section_reference=revised,
        cross_section_normalization_reference=normalization,
    )
    assert all(item.values["industry_strength"] is None for item in refreshed)


def test_partial_reference_computes_only_available_population_and_preserves_explicit_missing_values():
    builder = _builder()
    quotes = tuple(replace(_quote(f"60000{index}"), speed=float(index)) for index in range(4))
    histories = {quote.code: _history_bars() for quote in quotes}
    market = builder.build(quotes, histories, NOW)
    candidates = builder.build(
        quotes[:2],
        histories,
        NOW,
        cross_section_reference={
            market[0].quote.code: market[0].values,
            market[1].quote.code: {"industry_strength": None},
        },
        cross_section_normalization_reference={market[0].quote.code: market[0].normalization},
    )
    independent = builder.build(quotes[:2], histories, NOW)
    assert candidates[0].normalization == market[0].normalization
    assert candidates[0].values["speed_percentile"] == market[0].values["speed_percentile"]
    assert candidates[1].normalization == independent[1].normalization
    assert candidates[1].values["speed_percentile"] == independent[1].values["speed_percentile"]
    assert candidates[1].values["industry_strength"] is None


def test_industry_read_retains_research_age_expiry_and_cannot_overwrite_a_new_market(monkeypatch):
    clock = [0.0]
    builder = _builder()
    service = _service(StaticGateway((_quote(),)), StaticHistoryClient(), builder, monotonic=lambda: clock[0])
    initial = service.fetch_market_features(NOW, force=True)[0]
    evidence = Evidence("announcement:1", "announcement", "风险公告", "official", NOW)
    research = replace(
        initial, values={**initial.values, "negative_announcement_level": 80.0}, evidence=(*initial.evidence, evidence)
    )
    reference = service.reference_version()
    published = service.quotes.publish_market_features((research,), reference_epoch=reference)

    def forbidden(*args, **kwargs):
        raise AssertionError("industry read must not rebuild history, factors or market publication")

    monkeypatch.setattr(builder, "build", forbidden)
    monkeypatch.setattr(service.history, "cached", forbidden)
    monkeypatch.setattr(service.quotes, "publish_market_features", forbidden)
    clock[0] = 29.0
    assert service.refresh_industry_heat(NOW + timedelta(seconds=29)) == published
    assert service.quotes.market_features() is published
    clock[0] = 31.0
    assert service.quotes.cached_market_features(force=False, reference_epoch=reference) is None
    monkeypatch.undo()
    newer = (replace(research, quote=replace(research.quote, price=13.0)),)
    eligibility = service._eligible_codes

    def race(codes, observed_at):
        service.quotes.publish_market_features(newer, reference_epoch=reference)
        return eligibility(codes, observed_at)

    monkeypatch.setattr(service, "_eligible_codes", race)
    assert service.refresh_industry_heat(NOW + timedelta(seconds=31)) == published
    assert service.quotes.market_features() is newer
