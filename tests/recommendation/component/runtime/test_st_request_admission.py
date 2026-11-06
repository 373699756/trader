"""ST admission precedes per-stock work while Long and round identities stay independent."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from tests.helpers.recommendation.market_data import NOW, _quote
from tests.helpers.recommendation.runtime.official_static_population import _fixture, _population, _reader
from tests.helpers.shared.reference_fixtures import PreparedFixture
from trader.download.domain.history_reference import HistoryStEvidence
from trader.infra.market_data.observations import SourceObservation
from trader.recommendation.application.runtime.round_inputs import ProfileRoundInputs, RoundInputs
from trader.recommendation.domain.market.eligibility import (
    HistoricalStEligibilitySnapshot,
    SelectQuotePopulation,
)
from trader.recommendation.infra.market_data.published_references import PublishedReferences


def _mixed_reference():
    population = (*_population(), replace(_population()[1], code="600002"))
    return replace(
        PreparedFixture(NOW, population, industry_version="industry-fixture"),
        st_evidence=(
            HistoryStEvidence("300001", NOW.date(), "clear"),
            HistoryStEvidence("600001", NOW.date(), "ever_st"),
            HistoryStEvidence("600002", NOW.date(), "unknown"),
        ),
    )


def _industry(code):
    return SourceObservation(
        source="baostock_industry", subject_key=code, observed_at=NOW, source_time=NOW, received_at=NOW,
        effective_at=NOW, data_version="industry-fixture",
        fields={
            "model_industry": "J66", "model_industry_classification": "证监会行业分类",
            "model_industry_effective_date": NOW.date().isoformat(), "model_industry_source": "fixture",
            "model_industry_data_version": "industry-fixture",
        },
        missing_reasons={}, payload_hash="", status="success", error_code=None,
    )


def test_industry_projection_only_constructs_qualified_issuers_from_a_public_batch():
    reference = _mixed_reference()
    # These rows deliberately have no fields: excluded stock content must never
    # reach the per-stock industry decoder.
    rejected = tuple(replace(_industry(code), fields={}) for code in ("600001", "600002"))
    reader = PublishedReferences(
        lambda: reference, lambda: reference.identity,
        lambda _: (_industry("300001"), *rejected), max_age_seconds=86400,
    )
    reader.initialize()
    assert reader.status().failure is None
    assert tuple(reader.model_industries(("300001", "600001", "600002"))) == ("300001",)
    assert reader.historical_st_eligibility().eligible_codes == ("300001",)


@pytest.mark.parametrize("status", ("ever_st", "unknown"))
def test_empty_st_admission_never_reads_the_industry_batch(status):
    reference = _mixed_reference()
    reference = replace(reference, st_evidence=tuple(
        replace(item, status=status) for item in reference.st_evidence
    ))

    def forbidden(_):
        raise AssertionError("industry read before a nonempty clear population")

    reader = PublishedReferences(lambda: reference, lambda: reference.identity, forbidden, max_age_seconds=86400)
    reader.initialize()
    assert reader.status().failure is None
    assert not reader.model_industries(("300001",))


@pytest.mark.parametrize("operation", ("refresh_candidate_quotes", "refresh_topk_quotes", "fetch_candidate_features"))
def test_directed_requests_never_include_st_or_unknown_reference_issuers(monkeypatch, operation):
    reference = _mixed_reference()
    _, gateway, history, service = _fixture(
        quotes=(_quote("300001"), _quote("600001"), _quote("600002")),
        published_references=_reader({"reference": reference}),
    )
    requested = []

    def fetch(codes, **kwargs):
        requested.append(tuple(codes))
        return gateway._quotes

    monkeypatch.setattr(gateway, "fetch_candidates", fetch)
    monkeypatch.setattr(gateway, "fetch_topk_quotes", fetch, raising=False)
    result = getattr(service, operation)(("300001", "600001", "600002"), NOW)
    assert requested == [("300001",)]
    assert [feature.quote.code for feature in result] == ["300001"]
    assert set(history.calls) == (set() if operation == "refresh_topk_quotes" else {"300001"})


@pytest.mark.parametrize("marker", ({"is_st": True}, {"name": "*ST新名称"}, {"name": "退市名称"}))
def test_new_st_quote_prevents_history_research_and_minute_work(monkeypatch, marker):
    _, gateway, history, service = _fixture()
    gateway._quotes = (replace(_quote(), **marker),)
    operations = []
    monkeypatch.setattr(service.research, "load", lambda codes, *args, **kwargs: operations.append(("company", tuple(codes))) or {})
    monkeypatch.setattr(service.intraday, "load", lambda codes, *args, **kwargs: operations.append(("minute", tuple(codes))) or {})
    monkeypatch.setattr(service.intraday, "refresh", lambda codes, *args, **kwargs: operations.append(("minute-refresh", tuple(codes))) or ())
    assert service.fetch_candidate_features(("600001",), NOW, include_intraday_tail=True) == ()
    assert history.calls == []
    assert operations == []
    service.refresh_intraday_tail(("600001",), NOW)
    assert operations == [("minute-refresh", ())]


def test_new_st_in_a_full_market_batch_is_rejected_without_becoming_a_missing_quote():
    _, gateway, history, service = _fixture(quotes=(_quote(), _quote("300001")))
    gateway._quotes = (replace(_quote(), is_st=True), _quote("300001"))
    batch = service.fetch_market_feature_batch(NOW, force=True)
    assert [feature.quote.code for feature in batch.features] == ["300001"]
    assert history.calls == ["300001"]
    assert batch.dynamic_stage.snapshot.rejected_count == 1
    assert batch.dynamic_stage.snapshot.pending_count == 0
    assert [item.code for item in batch.dynamic_stage.snapshot.reasons] == ["historical_st"]


def test_shared_candidate_batch_keeps_empty_profiles_and_one_source_cut(monkeypatch):
    _, gateway, _, service = _fixture(quotes=(_quote(), _quote("300001")))
    gateway._quotes = (
        replace(_quote(), is_st=True),
        replace(_quote("300001"), data_version="locked-candidate"),
    )
    requests = []

    def fetch(codes, **kwargs):
        requests.append(tuple(codes))
        locked = gateway._quotes
        gateway._quotes = tuple(replace(quote, data_version="next-cut", price=99) for quote in locked)
        return locked

    monkeypatch.setattr(gateway, "fetch_candidates", fetch)
    inputs = RoundInputs(("v1", "v2", "v3", "v5"), lambda: 5)
    def collect(profile):
        return service.refresh_candidate_quotes(
            () if profile == "v1" else ("300001", "600001"), NOW,
            round_inputs=ProfileRoundInputs(profile, inputs),
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = tuple(executor.map(collect, ("v1", "v2", "v3", "v5")))
    assert requests == [("300001", "600001")]
    assert results[0] == ()
    for features in results[1:]:
        assert [(item.quote.code, item.quote.data_version, item.quote.price) for item in features] == [
            ("300001", "locked-candidate", _quote("300001").price),
        ]


def test_long_quotes_do_not_wait_for_static_qualification_or_history(monkeypatch):
    _, gateway, history, service = _fixture(missing=True)
    requested = []

    def fetch(codes, **kwargs):
        requested.append(tuple(codes))
        return (replace(_quote(), is_st=True),)

    monkeypatch.setattr(gateway, "fetch_long_quotes", fetch, raising=False)
    features = service.refresh_long_quotes(("600001",), NOW)
    assert requested == [("600001",)]
    assert features[0].quote.is_st and features[0].values == {}
    assert history.calls == []
    assert service.risk_universe_codes(NOW) == ()


def test_unavailable_st_reference_never_falls_back_to_the_official_population():
    reference = _reader({"reference": PreparedFixture(NOW)})
    reference.initialize()
    assert SelectQuotePopulation(
        reference.static_reference().reference, (), HistoricalStEligibilitySnapshot.unavailable(),
    ) == ()


def test_empty_admission_does_not_start_a_full_market_request(monkeypatch):
    reference = _mixed_reference()
    reference = replace(reference, st_evidence=tuple(
        replace(item, status="unknown") for item in reference.st_evidence
    ))
    _, gateway, history, service = _fixture(published_references=_reader({"reference": reference}))

    def forbidden(**kwargs):
        raise AssertionError("market request without qualified issuers")

    monkeypatch.setattr(gateway, "fetch_market", forbidden)
    assert service.fetch_market_feature_batch(NOW).features == ()
    assert history.calls == []


def test_expired_st_reference_blocks_new_directed_requests(monkeypatch):
    from datetime import timedelta

    reference = PreparedFixture(NOW)
    reader = PublishedReferences(lambda: reference, lambda: reference.identity, lambda _: (), max_age_seconds=1)
    _, gateway, history, service = _fixture(published_references=reader)

    def forbidden(*args, **kwargs):
        raise AssertionError("quote request with expired ST evidence")

    monkeypatch.setattr(gateway, "fetch_candidates", forbidden)
    assert service.refresh_candidate_quotes(("600001",), NOW + timedelta(seconds=2)) == ()
    assert history.calls == []


def test_company_startup_uses_the_static_cut_before_any_history_initialization(monkeypatch):
    from tests.helpers.recommendation.market.candidate_history_tail import HistoryInput
    from tests.helpers.recommendation.publication.shared_history_projection import _cache
    from tests.helpers.shared.configuration import PublishedCompanyResearchFixture as PublishedCompanyResearch

    reference = _mixed_reference()
    class History(HistoryInput):
        decoded = []

        def window(self, code, dates):
            self.decoded.append(code)
            return super().window(code, dates)

    source = History(252, 0, ("300001", "600001", "600002"))
    cache = _cache(source, 61)
    _, _, _, service = _fixture(published_references=_reader({"reference": reference}), published_history=cache)
    history_admission, company_requests = [], []

    class Evidence:
        def publication_version(self):
            return "fixture-company"

        def visit(self, codes, consume, *, cancel_requested=None):
            company_requests.append(tuple(codes))

    service.research = PublishedCompanyResearch(
        Evidence(), wall_clock=lambda: NOW, open_dates=lambda: reference.open_dates,
        monotonic=lambda: 0, company_policy=service.research._company_policy,
    )
    initialize = cache.initialize

    def observe(*, cancelled, codes=None):
        history_admission.append(codes)
        initialize(cancelled=cancelled, codes=codes)

    monkeypatch.setattr(cache, "initialize", observe)
    service.initialize_published_inputs(lambda: False, model_version="fixture", config_version="fixture")
    assert history_admission == [frozenset({"300001"})]
    assert company_requests == [("300001",)]
    assert source.decoded == ["300001"]
    assert set(cache.entries()) == {"300001"}


@pytest.mark.parametrize("all_unknown", (False, True))
def test_four_profile_startup_passes_one_static_selection_and_never_reads_excluded_history(all_unknown):
    from tests.helpers.recommendation.market.candidate_history_tail import HistoryInput
    from tests.helpers.recommendation.publication.shared_history_projection import _cache
    from trader.download.application.read_published_history import ReadPublishedHistoryUseCase
    from trader.infra.settings.models import HistoryPreloadSettings
    from trader.recommendation.application.pipeline.static_market.static_market_cache import StaticMarketCache
    from trader.recommendation.infra.market_data.shared_history_projection import SharedHistoryProjection

    reference = _mixed_reference()
    if all_unknown:
        reference = replace(reference, st_evidence=tuple(replace(item, status="unknown") for item in reference.st_evidence))
    selected = frozenset() if all_unknown else frozenset({"300001"})

    class History(HistoryInput):
        scans = 0
        closed = 0

        def iter_windows(self, manifest, *, sessions, codes=None, check=None):
            assert manifest is self.current
            assert codes == selected
            self.scans += 1
            try:
                yield from super().iter_windows(manifest, sessions=sessions, codes=codes, check=check)
            finally:
                self.closed += 1

        def window(self, code, dates):
            assert code in selected
            return super().window(code, dates)

    source = History(252, 0, ("300001", "600001", "600002"))
    owner = SharedHistoryProjection(ReadPublishedHistoryUseCase(source), monotonic=lambda: 0.0, settings=HistoryPreloadSettings(warn_after_seconds=300))
    static_cache = StaticMarketCache()
    histories = tuple(_cache(source, sessions, owner, rank, sequence) for sessions, rank, sequence in (
        (251, True, False), (251, False, False), (61, False, False), (61, False, True),
    ))
    for history in histories:
        _, _, _, service = _fixture(published_references=_reader({"reference": reference}), published_history=history, static_cache=static_cache)
        service.initialize_published_inputs(lambda: False, model_version="fixture", config_version="fixture")
        assert set(history.entries()) == selected
        assert history.loaded_identity == source.current.snapshot_hash
    assert source.scans == source.closed == owner.scan_count == (0 if all_unknown else 1)
    assert source.reads == []


def test_cancelled_startup_does_not_load_references(monkeypatch):
    _, _, _, service = _fixture()

    def forbidden():
        raise AssertionError("reference work after cancellation")

    monkeypatch.setattr(service.references, "initialize", forbidden)
    service.initialize_published_inputs(lambda: True, model_version="fixture", config_version="fixture")


def test_st_rejection_and_missing_quotes_have_distinct_batch_identities_at_the_same_time():
    _, gateway, _, service = _fixture(quotes=())
    missing = service.fetch_market_feature_batch(NOW, force=True)
    gateway._quotes = (replace(_quote(), is_st=True),)
    excluded = service.fetch_market_feature_batch(NOW, force=True)
    assert missing.features == excluded.features == ()
    assert missing.dynamic_stage.snapshot.pending_count == 2
    assert excluded.dynamic_stage.snapshot.rejected_count == 1
    assert missing.dynamic_stage.snapshot.input_batch_id == excluded.dynamic_stage.snapshot.input_batch_id
    assert missing.dynamic_stage.snapshot.output_batch_id != excluded.dynamic_stage.snapshot.output_batch_id
