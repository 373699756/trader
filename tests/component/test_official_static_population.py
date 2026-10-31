
from tests.helpers.configuration import SqlitePolicyFixture
import threading
from dataclasses import replace
from datetime import date, timedelta

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
    StaticGateway,
    _history_bars,
    _quote,
    _service,
)
from tests.reference_fixtures import PreparedFixture
from trader.download.application.company_exclusions import PublishCompanyExclusions
from trader.download.domain.baostock_daily import BaoStockSecurity
from tests.helpers.configuration import SQLiteCompanyFactsIndexFixture as SQLiteCompanyFactsIndex
from trader.download.infra.reference_index import SQLiteReferenceIndex
from trader.recommendation.domain.evidence.pipeline import SourceHealthState, StageState
from trader.recommendation.domain.market.eligibility import HistoricalStEligibilitySnapshot, ManualBlacklistFact
from trader.recommendation.infra.market_data.company_eligibility import CompanyEligibility
from trader.recommendation.infra.market_data.published_references import PublishedReferences
from trader.recommendation.infra.market_data.trading_calendar import (
    ChinaTradingCalendar,
    TradingCalendarUnavailableError,
)
from trader.recommendation.infra.status_projection import _stage_snapshot_payload


def _population():
    return (
        BaoStockSecurity("300001", "证券300001", "chinext", date(2020, 1, 2), None, "fixture"),
        BaoStockSecurity("600001", "证券600001", "main", date(2020, 1, 2), None, "fixture"),
    )


def _reader(state):
    return PublishedReferences(
        lambda: state["reference"],
        lambda: state["reference"].identity if state["reference"] else None,
        lambda _: (),
        max_age_seconds=86400,
    )


def _fixture(*, quotes=None, missing=False, population=None, **options):
    state = {"reference": None if missing else PreparedFixture(NOW, population or _population())}
    gateway = StaticGateway((_quote(),) if quotes is None else quotes)
    history = CountingHistoryClient(_history_bars())
    service = _service(
        gateway,
        history,
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        published_references=_reader(state),
        **options,
    )
    return state, gateway, history, service


def test_official_population_survives_missing_and_foreign_quotes():
    _, gateway, history, service = _fixture(quotes=(_quote(), _quote("600999")))
    first = service.fetch_market_feature_batch(NOW)
    assert [(s.input_count, s.output_count) for s in first.static_stages] == [(2, 2)] * 2
    assert [f.quote.code for f in first.features] == ["600001"]
    assert history.calls == ["600001"]
    projected = _stage_snapshot_payload(first.dynamic_stage.snapshot)
    assert (projected["input_count"], projected["output_count"], projected["pending_count"]) == (2, 1, 1)
    assert projected["rejected_count"] == projected["failed_count"] == 0
    assert "300001" not in repr(projected) and "600999" not in repr(projected)
    gateway._quotes = ()
    empty = service.fetch_market_feature_batch(NOW + timedelta(seconds=10), force=True)
    assert empty.static_stages[0].operation_durations[0][0] == "startup_reuse"
    assert empty.static_stages[0].source_health.age_seconds == 10
    assert empty.dynamic_stage.snapshot.input_count == empty.dynamic_stage.snapshot.pending_count == 2
    assert empty.dynamic_stage.snapshot.state is StageState.NOT_READY


def test_no_official_reference_never_substitutes_quote_population():
    _, _, history, service = _fixture(missing=True)
    batch = service.fetch_market_feature_batch(NOW)
    assert batch.features == () and history.calls == []
    assert all(s.state is StageState.NOT_READY for s in batch.static_stages)
    assert batch.static_stages[0].reasons[0].code == "static_reference_unavailable"


def test_new_publication_is_adopted_only_by_new_process():
    state, _, history, service = _fixture(quotes=(_quote(), _quote("600002")))
    first = service.fetch_market_feature_batch(NOW)
    original = service.references.static_reference()
    state["reference"] = PreparedFixture(
        NOW,
        (
            _population()[0],
            replace(_population()[1], code="600002"),
        ),
    )
    service.references.initialize()
    retained = service.fetch_market_feature_batch(NOW + timedelta(seconds=1), force=True)
    assert service.references.static_reference().reference is original.reference
    assert not service.references.status().restart_required
    assert service.references.status().disk_identity == original.reference.data_version
    assert [item.quote.code for item in retained.features] == ["600001"]
    assert first.features[0].quote == retained.features[0].quote
    assert set(history.calls) == {"600001"}
    restarted = _reader(state)
    restarted.initialize()
    assert tuple(item.code for item in restarted.static_reference().reference.records) == ("300001", "600002")
    assert not restarted.status().restart_required


def test_loaded_population_and_age_do_not_depend_on_later_disk_access():
    _, _, _, service = _fixture()
    original = service.references.static_reference()

    def broken_identity():
        pytest.fail("loaded references must not read the publication again")

    service.references._read_identity = broken_identity
    service.references.initialize()
    assert service.references.static_reference().reference is original.reference
    assert service.references.status().failure is None
    stale = service.fetch_market_feature_batch(NOW + timedelta(days=1))
    assert stale.static_stages[0].source_health.state is SourceHealthState.DEGRADED
    assert stale.static_stages[0].source_health.age_seconds == 86400


def test_non_st_risk_fact_is_deferred_to_second_level_filter(tmp_path):
    eligibility = CompanyEligibility(
        PublishCompanyExclusions(SQLiteCompanyFactsIndex(tmp_path / "company_facts.sqlite3", sqlite_policy=SqlitePolicyFixture()))
    )
    eligibility.record((ManualBlacklistFact("300001", effective_at=NOW, config_hash="fixture"),))
    _, _, history, service = _fixture(quotes=(_quote(), _quote("300001")), eligibility=eligibility)
    batch = service.fetch_market_feature_batch(NOW)
    static = batch.static_stages[-1]
    assert (static.input_count, static.output_count, static.rejected_count) == (2, 2, 0)
    assert sorted(history.calls) == ["300001", "600001"]
    blocked = next(item for item in batch.features if item.quote.code == "300001")
    assert "issuer_risk:manual_permanent_blacklist" in blocked.quote.execution_restrictions


def test_risk_universe_excludes_st_without_waiting_for_quotes():
    _, _, _, service = _fixture(
        quotes=(),
        population=(_population()[0], replace(_population()[1], name="*ST测试")),
    )
    assert service.risk_universe_codes(NOW) == ("300001",)


def test_published_reference_st_eligibility_precedes_per_stock_history():
    qualification = HistoricalStEligibilitySnapshot.ready("a" * 64, ("600001",))
    _, _, history, service = _fixture(historical_st_eligibility=qualification)
    batch = service.fetch_market_feature_batch(NOW)
    static = batch.static_stages[-1]
    assert (static.input_count, static.output_count, static.pending_count) == (2, 1, 1)
    assert [item.quote.code for item in batch.features] == ["600001"]
    assert history.calls == ["600001"]


def test_reference_st_revision_is_adopted_only_on_restart_before_history_reads(tmp_path):
    first = PreparedFixture(NOW, _population())
    revised = replace(
        first,
        st_evidence=(
            replace(first.st_evidence[0], status="ever_st"),
            replace(first.st_evidence[1], status="unknown"),
        ),
    )
    state = {"reference": first}
    reference = _reader(state)
    reference.initialize()
    state["reference"] = revised
    reference.initialize()
    assert not reference.status().restart_required
    assert reference.historical_st_eligibility().eligible_codes == ("300001", "600001")
    restarted = _reader(state)
    history = CountingHistoryClient(_history_bars())
    service = _service(
        StaticGateway((_quote("300001"), _quote("600001"))),
        history,
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        published_references=restarted,
        eligibility=CompanyEligibility(
            PublishCompanyExclusions(SQLiteCompanyFactsIndex(tmp_path / "company_facts.sqlite3", sqlite_policy=SqlitePolicyFixture()))
        ),
    )
    batch = service.fetch_market_feature_batch(NOW)
    assert batch.features == () and history.calls == []
    assert batch.static_stages[-1].rejected_count == 1
    assert batch.static_stages[-1].pending_count == 1


def test_preload_retries_changed_publication_and_never_accepts_mixed_identity():
    first = PreparedFixture(NOW, _population())
    second = PreparedFixture(NOW + timedelta(days=1), _population())
    reads = iter((first, second))
    references = PublishedReferences(
        lambda: next(reads),
        lambda: second.identity,
        lambda _: (),
        max_age_seconds=86400,
    )
    references.initialize()
    assert references.reference() is second
    assert not references.status().restart_required


def test_concurrent_initializers_wait_for_one_complete_reference():
    entered, release, second_done = threading.Event(), threading.Event(), threading.Event()
    calls = []
    reference = PreparedFixture(NOW)

    def read():
        calls.append(True)
        entered.set()
        assert release.wait(2)
        return reference

    references = PublishedReferences(read, lambda: reference.identity, lambda _: (), max_age_seconds=86400)
    first = threading.Thread(target=references.initialize)
    second = threading.Thread(target=lambda: (references.initialize(), second_done.set()))
    first.start()
    try:
        assert entered.wait(1)
        second.start()
        assert not second_done.wait(0.05)
    finally:
        release.set()
        first.join(2)
        second.join(2)
    assert second_done.is_set() and calls == [True]
    assert references.reference() is reference


def test_reference_publication_round_trip_and_corruption_fail_closed(tmp_path):
    from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction

    publication = SQLiteReferenceIndex(tmp_path / "company_st.sqlite3", sqlite_policy=SqlitePolicyFixture())
    reference = PreparedFixture(NOW)
    assert publication.read() is None
    publication.publish(reference, reference.st_evidence)
    assert publication.read() == reference
    assert publication.identity() == reference.identity
    with SqliteSession(publication.path) as connection, SqliteTransaction(connection):
        connection.run("UPDATE security SET name='篡改名称'")
    with pytest.raises(ValueError, match="hash_conflict"):
        publication.read()


def test_calendar_is_memory_only_uses_shanghai_day_and_rejects_stale_inputs():
    reference = PreparedFixture(NOW)
    at = reference.checked_at.replace(hour=0, minute=30)
    clock = {"now": at}
    calendar = ChinaTradingCalendar(lambda: reference, now=lambda: clock["now"])
    clock["now"] = reference.checked_at
    assert calendar.is_trading_day(NOW.date())
    assert calendar.session_distance("2026-07-16", "2026-07-20") == 2
    clock["now"] = reference.checked_at + timedelta(days=31)
    with pytest.raises(TradingCalendarUnavailableError):
        calendar.open_dates()
    missing = ChinaTradingCalendar(lambda: None, now=lambda: NOW)
    with pytest.raises(TradingCalendarUnavailableError):
        missing.open_dates()
