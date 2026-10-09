from dataclasses import replace
from datetime import date, timedelta, timezone

import pytest

from tests.component.market_data_test_support import (
    FEATURE_WEIGHT_POLICY,
    LONG_POLICY,
    MARKET_REGIME_POLICY,
    NEWS_POLICY,
    NOW,
    TAIL_POLICY,
    CountingHistoryClient,
    FeatureBuilder,
    SQLiteDataPlane,
    StaticGateway,
    _history_bars,
    _quote,
    _service,
)
from trader.infra.market_data.providers.exchange_security_master import (
    ExchangeSecurityMasterClient,
    ExchangeSecurityMasterListing,
)
from trader.recommendation.application.ports.market_data import MarketDataUnavailableError
from trader.recommendation.domain.evidence.pipeline import SourceHealthState, StageState
from trader.recommendation.domain.market.eligibility import manual_blacklist_fact
from trader.recommendation.infra.market_data.official_static_reference import parse_official_static_reference
from trader.recommendation.infra.persistence.issuer_eligibility import SQLiteIssuerEligibilityRegistry
from trader.recommendation.infra.status_projection import _stage_snapshot_payload


def _listing(code="600001"):
    return ExchangeSecurityMasterListing(code, f"证券{code}", date(2020, 1, 2), "main", "SSE")


def _fixture(*, quotes=None, **options):
    state = {"now": NOW, "rows": (_listing(),), "fail": False, "tick": 0.0}

    def fetch_sse(_timeout):
        if state["fail"]:
            raise TimeoutError("fixture timeout")
        return state["rows"]

    client = ExchangeSecurityMasterClient(
        timeout_seconds=1,
        minimum_rows=2,
        sse_fetcher=fetch_sse,
        szse_fetcher=lambda _: (
            ExchangeSecurityMasterListing("300001", "证券300001", date(2020, 1, 2), "chinext", "SZSE"),
        ),
        wall_clock=lambda: state["now"],
    )
    gateway = StaticGateway((_quote(),) if quotes is None else quotes)
    history = CountingHistoryClient(_history_bars())
    service = _service(
        gateway,
        history,
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        exchange_security_master_client=client,
        monotonic=lambda: state["tick"],
        **options,
    )
    return state, client, gateway, history, service


def test_official_population_survives_missing_and_foreign_quotes_in_read_only_projection():
    _, client, gateway, history, service = _fixture(quotes=(_quote(), _quote("600999")))
    service.references.schedule_security_master_refresh(NOW)
    first = service.fetch_market_feature_batch(NOW)
    assert [(s.input_count, s.output_count) for s in first.static_stages] == [(2, 2)] * 4
    assert [f.quote.code for f in first.features] == ["600001"]
    assert history.calls == ["600001"]
    projected = _stage_snapshot_payload(first.dynamic_stage.snapshot)
    assert (projected["input_count"], projected["output_count"], projected["pending_count"]) == (2, 1, 1)
    assert projected["rejected_count"] == projected["failed_count"] == 0
    assert "300001" not in repr(projected) and "600999" not in repr(projected)

    gateway._quotes = ()
    empty = service.fetch_market_feature_batch(NOW + timedelta(seconds=10), force=True)
    assert empty.static_stages[1].reasons[0].code == "static_baseline_reused"
    assert empty.static_stages[0].source_health.age_seconds == 10
    assert empty.dynamic_stage.snapshot.input_count == empty.dynamic_stage.snapshot.pending_count == 2
    assert empty.dynamic_stage.snapshot.rejected_count == 0
    assert empty.dynamic_stage.snapshot.state is StageState.NOT_READY
    cached_empty = service.fetch_market_feature_batch(NOW + timedelta(seconds=11))
    assert cached_empty.dynamic_stage.snapshot.pending_count == 2
    assert client.health().planned_count == 1
    assert first.static_stages[0].source_health.age_seconds == 0


def test_no_official_reference_never_substitutes_quote_population(tmp_path):
    _, _, _, history, service = _fixture(data_plane=SQLiteDataPlane(tmp_path))
    batch = service.fetch_market_feature_batch(NOW)
    assert batch.features == () and history.calls == []
    assert all(s.state is StageState.NOT_READY for s in batch.static_stages)
    assert batch.static_stages[0].reasons[0].code == "static_reference_unavailable"
    assert batch.static_stages[0].source_health.state is SourceHealthState.UNAVAILABLE


def test_refresh_replaces_whole_population_and_invalidates_feature_cache():
    state, client, _, history, service = _fixture(quotes=(_quote(), _quote("600002")))
    service.references.schedule_security_master_refresh(NOW)
    first = service.fetch_market_feature_batch(NOW)
    first_read = service.references.static_reference()
    state.update(now=NOW + timedelta(seconds=1), rows=(_listing("600002"),))
    service.references.schedule_reference_data((), state["now"], force=True)
    next_read = service.references.static_reference()
    changed = service.fetch_market_feature_batch(state["now"])
    assert [f.quote.code for f in changed.features] == ["600002"]
    assert next_read.reference_epoch != first_read.reference_epoch
    assert first_read.reference.records[0].code == "300001"
    assert tuple(i.code for i in first_read.reference.records) == ("300001", "600001")
    assert tuple(i.code for i in next_read.reference.records) == ("300001", "600002")
    assert changed.static_stages[1].reasons[0].code == "static_baseline_loaded"
    assert first.features[0].quote.code == "600001"
    assert history.calls == ["600001", "600002"]
    assert client.health().planned_count == 2


@pytest.mark.parametrize("failure", ["timeout", "empty", "duplicate", "future"])
def test_failed_refresh_retains_population_and_age_with_explicit_degradation(failure):
    state, client, _, _, service = _fixture()
    service.references.schedule_security_master_refresh(NOW)
    first = service.fetch_market_feature_batch(NOW)
    original = service.references.static_reference()
    state["now"] = NOW + timedelta(seconds=10)
    if failure == "timeout":
        state["fail"] = True
    elif failure == "empty":
        state["rows"] = ()
    elif failure == "duplicate":
        state["rows"] = (_listing(), _listing())
    else:
        state["rows"] = (replace(_listing(), listing_date=NOW.date() + timedelta(days=1)),)
    with pytest.raises((RuntimeError, ValueError)):
        service.references.schedule_reference_data((), state["now"], force=True)
    retained_read = service.references.static_reference()
    assert retained_read.reference is original.reference
    assert retained_read.reference_epoch == original.reference_epoch
    assert retained_read.refresh_failed
    retained = service.fetch_market_feature_batch(state["now"])
    health = retained.static_stages[0].source_health
    assert health.state is SourceHealthState.DEGRADED
    assert health.latest_success_at == NOW and health.age_seconds == 10
    assert retained.static_stages[0].reasons[0].code == "static_reference_degraded"
    assert all(s.rejected_count == 0 for s in retained.static_stages)
    assert first.static_stages[0].source_health.state is SourceHealthState.READY
    assert client.health().error_count == 1


def test_refresh_ttl_and_retry_apply_even_when_all_quote_identities_are_present():
    state, client, _, _, service = _fixture()
    service.references.schedule_security_master_refresh(NOW)
    service.references.schedule_reference_data(("600001",), NOW, security_master_codes=("600001",))
    assert client.health().planned_count == 1
    state.update(now=NOW + timedelta(days=1), tick=86400.0, fail=True)
    with pytest.raises(RuntimeError):
        service.references.schedule_reference_data(("600001",), state["now"], security_master_codes=("600001",))
    service.references.schedule_security_master_refresh(state["now"])
    assert client.health().planned_count == 2
    state.update(now=state["now"] + timedelta(seconds=300), tick=86700.0, fail=False)
    service.references.schedule_reference_data(("600001",), state["now"], security_master_codes=("600001",))
    assert client.health().planned_count == 3
    assert not service.references.static_reference().refresh_failed


def test_successful_same_content_refresh_updates_age_without_changing_content_identity():
    state, _, _, _, service = _fixture()
    service.references.schedule_security_master_refresh(NOW)
    first = service.fetch_market_feature_batch(NOW)
    epoch = service.reference_version()
    state["now"] = NOW + timedelta(seconds=10)
    service.references.schedule_reference_data((), state["now"], force=True)
    updated = service.fetch_market_feature_batch(state["now"])
    assert service.reference_version() == epoch
    assert updated.static_stages[1].reasons[0].code == "static_baseline_reused"
    assert updated.static_stages[0].source_health.latest_success_at == state["now"]
    assert updated.static_stages[0].source_health.age_seconds == 0
    assert first.static_stages[0].source_health.latest_success_at == NOW


def test_unique_registry_filters_the_complete_population_before_quote_and_history_subset(tmp_path):
    registry = SQLiteIssuerEligibilityRegistry(tmp_path)
    registry.record((manual_blacklist_fact("300001", effective_at=NOW, config_hash="fixture"),))
    _, _, _, history, service = _fixture(eligibility=registry)
    service.references.schedule_security_master_refresh(NOW)
    batch = service.fetch_market_feature_batch(NOW)
    static = batch.static_stages[-1]
    assert (static.input_count, static.output_count, static.rejected_count) == (2, 1, 1)
    assert batch.dynamic_stage.snapshot.pending_count == 0
    assert history.calls == ["600001"]
    assert all(s.rejected_count == 0 for s in batch.static_stages[:3])


def test_parser_rejects_mixed_versions_duplicate_and_missing_name_before_acceptance():
    _, client, _, _, _ = _fixture()
    rows = client.fetch(NOW)
    invalid_batches = (
        (rows[0], replace(rows[1], data_version="other")),
        (rows[0], rows[0]),
        (replace(rows[0], fields={**rows[0].fields, "name": " "}), rows[1]),
    )
    for invalid in invalid_batches:
        with pytest.raises(ValueError):
            parse_official_static_reference(invalid)


def test_parser_compares_listing_day_in_shanghai_when_observed_time_is_utc():
    _, client, _, _, _ = _fixture()
    rows = client.fetch(NOW)
    midnight = NOW.replace(hour=0, minute=30).astimezone(timezone.utc)
    current = tuple(
        replace(item, observed_at=midnight, fields={**item.fields, "listing_date": NOW.date().isoformat()})
        for item in rows
    )
    assert len(parse_official_static_reference(current).records) == 2


def test_reference_replaced_during_quote_read_is_not_published_under_old_identity(monkeypatch):
    state, _, gateway, _, service = _fixture()
    service.references.schedule_security_master_refresh(NOW)
    first = service.fetch_market_feature_batch(NOW)

    def changing_quote_read(**_options):
        state.update(now=NOW + timedelta(seconds=1), rows=(_listing("600002"),))
        service.references.schedule_reference_data((), state["now"], force=True)
        return (_quote(),)

    monkeypatch.setattr(gateway, "fetch_market", changing_quote_read)
    with pytest.raises(MarketDataUnavailableError, match="reference_changed"):
        service.fetch_market_feature_batch(NOW, force=True)
    assert service.quotes.status().market_features == first.features


@pytest.mark.parametrize("offset", [-1, 0])
def test_old_or_conflicting_same_time_population_cannot_replace_accepted_reference(offset):
    state, _, _, _, service = _fixture()
    service.references.schedule_security_master_refresh(NOW)
    original = service.references.static_reference()
    state.update(now=NOW + timedelta(seconds=offset), rows=(_listing("600002"),))
    with pytest.raises(ValueError, match="older|conflicts"):
        service.references.schedule_reference_data((), state["now"], force=True)
    assert service.references.static_reference().reference is original.reference


def test_expired_reference_is_reused_with_degraded_health():
    _, _, _, _, service = _fixture()
    service.references.schedule_security_master_refresh(NOW)
    first = service.fetch_market_feature_batch(NOW)
    stale = service.fetch_market_feature_batch(NOW + timedelta(days=1))
    assert stale.static_stages[0].source_health.state is SourceHealthState.DEGRADED
    assert stale.static_stages[0].source_health.age_seconds == 86400
    assert stale.static_stages[1].reasons[0].code == "static_baseline_reused"
    assert first.static_stages[0].source_health.state is SourceHealthState.READY


def test_per_security_persistence_is_not_treated_as_complete_population_after_recovery(tmp_path):
    data_plane = SQLiteDataPlane(tmp_path)
    _, _, _, _, first_service = _fixture(data_plane=data_plane)
    first_service.references.schedule_security_master_refresh(NOW)
    _, _, _, history, recovered = _fixture(data_plane=data_plane)
    recovered.references.recover()
    batch = recovered.fetch_market_feature_batch(NOW)
    assert recovered.references.static_reference().reference is None
    assert batch.features == () and history.calls == []
    assert batch.static_stages[0].reasons[0].code == "static_reference_unavailable"
