from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from trader.recommendation.application.pipeline.data_source.input_identity import InputVersionClock
from trader.recommendation.application.runtime.schedule import SHANGHAI


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
