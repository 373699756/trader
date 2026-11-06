from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from trader.recommendation.application.pipeline.data_source.input_identity import InputVersionClock
from trader.recommendation.application.runtime.schedule import SHANGHAI
from trader.recommendation.domain.market.models import Evidence


def test_acceptance_reuses_delivery_only_changes_but_versions_same_supplier_corrections(application_feature_factory):
    clock = InputVersionClock()
    at = datetime(2026, 8, 12, 14, 0, tzinfo=SHANGHAI)
    feature = application_feature_factory("600001", at)
    original = clock.features("market", (feature,))
    delivered_again = replace(
        feature,
        observed_at=at + timedelta(seconds=1),
        quote=replace(feature.quote, received_time=at + timedelta(seconds=1)),
    )
    assert clock.features("market", (delivered_again,)) == original
    corrected = replace(delivered_again, quote=replace(delivered_again.quote, price=feature.quote.price + 1))
    assert clock.features("market", (corrected,)) != original
    assert clock.features("market", (corrected,)) == clock.features("market", (corrected,))


def test_input_kinds_have_independent_latest_values_and_distinct_revisions():
    clock = InputVersionClock()
    first = clock.accept("market", ("supplier", 1))
    second = clock.accept("candidate", ("supplier", 1))
    assert first != second
    assert clock.accept("market", ("supplier", 1)) == first
    assert clock.accept("market", ("supplier", 2)) != first


def test_generated_evidence_observation_is_not_a_new_fact_but_real_publication_is(application_feature_factory):
    at = datetime(2026, 8, 12, 14, 0, tzinfo=SHANGHAI)
    feature = application_feature_factory("600001", at)
    generated = Evidence("structured:600001:2026-08-12", "structured_point_in_time", "价格摘要", "fixture", at, at)
    announcement = Evidence("announcement:1", "announcement", "业绩公告", "official", at, at)
    feature = replace(feature, evidence=(generated, announcement))
    clock = InputVersionClock()
    original = clock.features("market", (feature,))
    later = at + timedelta(seconds=1)
    refreshed = replace(
        feature,
        observed_at=later,
        quote=replace(feature.quote, received_time=later),
        evidence=(replace(generated, published_at=later, received_at=later), announcement),
    )
    assert clock.features("market", (refreshed,)) == original
    for evidence in (
        (replace(generated, title="修正摘要"), announcement),
        (replace(generated, data_version="corrected"), announcement),
        (replace(generated, evidence_id="structured:600001:2026-08-13"), announcement),
        (generated, replace(announcement, published_at=later)),
        (generated, replace(announcement, received_at=later)),
    ):
        baseline = clock.features("market", (feature,))
        assert clock.features("market", (replace(feature, evidence=evidence),)) != baseline
    assert refreshed.evidence[0].published_at == later
    assert refreshed.quote.received_time == later


@pytest.mark.parametrize(
    "changes",
    (
        {"is_st": True},
        {"has_major_regulatory_risk": True},
        {"is_suspended": True},
        {"execution_restrictions": ("buy_blocked",)},
        {"cross_source_verified": False},
        {"cross_source_deviation_pct": 5.0},
        {"board_reliability": "missing"},
        {"rule_version": "corrected"},
        {"source": "fallback"},
        {"data_version": "corrected"},
    ),
)
def test_equal_numeric_factors_do_not_hide_risk_or_source_changes(application_feature_factory, changes):
    at = datetime(2026, 8, 12, 14, 0, tzinfo=SHANGHAI)
    feature = application_feature_factory("600001", at)
    clock = InputVersionClock()
    original = clock.features("market", (feature,))
    changed = replace(feature, quote=replace(feature.quote, **changes))
    assert changed.values == feature.values
    assert clock.features("market", (changed,)) != original


def test_source_time_and_time_dependent_derived_values_still_invalidate(application_feature_factory):
    at = datetime(2026, 8, 12, 14, 0, tzinfo=SHANGHAI)
    feature = application_feature_factory("600001", at)
    clock = InputVersionClock()
    original = clock.features("market", (feature,))
    changed = replace(feature, quote=replace(feature.quote, source_time=at + timedelta(seconds=1)))
    assert clock.features("market", (changed,)) != original
    baseline = clock.features("market", (feature,))
    aged = replace(feature, values={**feature.values, "evidence_freshness": 0.0})
    assert clock.features("market", (aged,)) != baseline
