"""The candidate round freezes inputs before pure publication and exact recovery."""

from dataclasses import replace
from datetime import date, timedelta
import json
from pathlib import Path

import pytest

from tests.helpers.download.company import company_evidence as fixtures
from tests.helpers.download.company.company_annual_income import _contract
from tests.helpers.download.company_resources import CompanyDocumentFilesFixture, SQLiteCompanyEvidenceReaderFixture, SQLiteCompanyFactsIndexFixture
from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.download.application.company_financial_producer import BuildCompanyFinancialRoundQuery, CompanyFinancialProducer
from trader.download.application.company_sync import RunCompanySyncRound
from trader.download.domain.company_sync import CompanySyncBudget, CompanySyncFamily, CompanySyncRoundScope
from trader.download.infra.company_sync_journal import SQLiteCompanySyncRoundJournal
from trader.download.domain.company_evidence import CompanyFinancial, CompanyInputValidationFailure, DisclosurePage, DisclosurePartition, StructuredCompanyQuery
from trader.download.domain.company_source_records import (
    CompanyFinancialMissingField, CompanyFinancialScoringObservation, CompanyFinancialSourceObservation, CompanyFinancialSourcePage,
    CompanyFinancialSourceRecordHash, CompanyFinancialSourceValues,
)
from trader.download.infra.company_fact_rows import ReadCompanyFactRows
from trader.download.infra.company_field_result_rows import ReadCompanyFieldResults
from trader.download.infra.company_field_publication import CompanyFieldProofRulesHash, PrepareCompanyFieldResults
from trader.download.infra.company_facts_index import CompanyInputReceipt, ReadCompanyPublicationIdentity
from trader.download.infra.company_facts_protocol import DecodeCompanyLabel, EVIDENCE_SOURCES, RequireCompanyTime
from trader.download.infra.company_financial_observations import ReadCommittedCompanyFinancialReports, ReadCompanyFinancialObservations
from trader.download.infra.company_material_index import SQLiteCompanyMaterialIndex
from trader.download.infra.company_normalized_index import CompanyNormalizationBaseline, SQLiteCompanyNormalizedIndex
from trader.download.infra.company_raw_files import CompanyRawFiles
from trader.download.infra.company_sync_inputs import SQLiteCompanySyncInputs
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession
from trader.infra.sqlite_publication import PublishSqliteFile, SqlitePublicationError


_CUTOFF = fixtures.NOW + timedelta(days=31)
_NOTICE = fixtures.NOW + timedelta(days=14)
_QUERY = StructuredCompanyQuery("financial", _NOTICE.date(), _NOTICE.date())
_CODES = ("600001",)


def _baseline(tmp_path):
    legacy = fixtures._index(tmp_path / "baseline")
    _contract(legacy)
    risk = fixtures._disclosure("known-risk", "关于收到中国证监会立案告知书的公告")
    legacy.commit_page(DisclosurePartition("sse", fixtures.NOW.date(), fixtures.NOW.date()), DisclosurePage((risk,), 1, False), fixtures.NOW)
    legacy.commit_document(risk, fixtures._extraction("本公司收到中国证监会立案告知书，公司涉嫌信息披露违法违规。"), fixtures.NOW)
    legacy.publish(_CODES, fixtures.NOW)
    with SqliteSession(legacy.path) as raw, SqliteSession(legacy.facts_path) as formal:
        identity = ReadCompanyPublicationIdentity(formal)
        facts = ReadCompanyFactRows(formal, _CODES)
        fields = tuple(field for fact in facts for field in PrepareCompanyFieldResults(
            raw, formal, fact, (), observed_at=fixtures.NOW, extraction_hash=identity.extraction_hash,
            event_policy=fixtures.CompanyEventPolicy(), read_document=legacy._documents.read, revalidate=True,
        ).results)
        receipts = tuple(CompanyInputReceipt(row[0], row[1], row[2], row[3], row[4], row[5], RequireCompanyTime(row[6]), row[7], row[8])
                         for row in formal.run("SELECT * FROM company_input_receipt ORDER BY source,source_key,code,record_hash,extraction_hash"))
        sources = tuple((row[0], DecodeCompanyLabel(EVIDENCE_SOURCES, row[1])) for row in formal.run("SELECT source_key,source FROM company_evidence_ref ORDER BY source_key"))
    return CompanyNormalizationBaseline(facts, fields, receipts, sources, identity, CompanyFieldProofRulesHash())


def _index(tmp_path, baseline=None, clean=None):
    facts = SQLiteCompanyFactsIndexFixture(tmp_path / "formal.sqlite3")
    index = SQLiteCompanyNormalizedIndex(
        tmp_path / "normalized.sqlite3", facts, event_policy=fixtures.CompanyEventPolicy(),
        sqlite_policy=SqlitePolicyFixture(), clean_material=clean or (lambda: None),
    )
    index.initialize()
    if baseline is not None:
        index.seed(baseline)
    return index, facts


def _observation(*, code="600001", at=_NOTICE, cutoff=_CUTOFF, period=date(2026, 9, 30), eps=2.0, parent=100.0,
                 update_delay=timedelta(0)):
    financial = CompanyFinancial(code, period, at, eps, 5, 1, 3, 4, 5, parent, 90)
    source_key = code + ".SH"
    updated = at + update_delay if update_delay is not None else None
    return CompanyFinancialSourceObservation(
        financial, source_key, at, updated, cutoff, "CNY",
        CompanyFinancialSourceRecordHash(financial, source_key, at, updated, "CNY"),
        tuple(CompanyFinancialMissingField(field) for field, value in CompanyFinancialSourceValues(financial) if value is None),
    )


def _page(*observations, page=1, pages=1, total=None):
    return CompanyFinancialSourcePage(page, pages, len(observations) if total is None else total, len(observations), tuple(observations), "a" * 64)


def test_working_source_writes_preserve_the_formal_publication_seal(tmp_path):
    baseline = _baseline(tmp_path / "source-baseline")
    index, facts = _index(tmp_path / "candidate", baseline)
    raw_root = tmp_path / "materials"
    raw_files = CompanyRawFiles(
        raw_root,
        capacity=10_000_000,
        analysis_reserve=1_000_000,
        free_reserve=1000,
        maximum_document_bytes=1024,
    )
    material = SQLiteCompanyMaterialIndex(
        index.path,
        facts=facts,
        documents=CompanyDocumentFilesFixture(raw_root / "text"),
        event_policy=fixtures.CompanyEventPolicy(),
        sqlite_policy=SqlitePolicyFixture(),
        raw_files=raw_files,
    )
    inputs = SQLiteCompanySyncInputs(index.path, sqlite_policy=SqlitePolicyFixture())
    material.initialize()
    index.validate_existing()
    inputs.validate_existing()
    material.set_population(_CODES)
    disclosure = fixtures._disclosure("separate-raw-dir", "公司回购公告")
    original = raw_files.directory(disclosure) / "download.part"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"%PDF-fixture")
    document = raw_files.complete(disclosure, original)
    material.document_stage(disclosure, "downloaded")
    inputs.freeze_pledge_day("scope", _NOTICE.date(), _CUTOFF)
    index.accept(_QUERY, _page(_observation()), _CODES, _CUTOFF)

    assert material.raw_root == raw_root
    assert inputs.pledge_day("scope") == _NOTICE.date()
    assert index.latest_financial_observations(_QUERY, _CODES, _CUTOFF) == (_observation(),)
    assert not Path(f"{index.path}.sha256").exists()
    assert Path(f"{facts.path}.sha256").is_file()
    facts.verify_publication()
    with SqliteSession(index.path) as connection:
        assert connection.run("SELECT code FROM company_population").require_one()[0] == _CODES[0]
        assert connection.run("SELECT path FROM document_tasks").require_one()[0] == str(document.path.relative_to(raw_root))
        assert connection.run("SELECT name FROM sqlite_schema WHERE name IN ('annual_income', 'structured_progress', 'ratios')").fetchall() == []
    assert SQLiteCompanyEvidenceReaderFixture(facts.path).read(_CODES) == baseline.facts


def test_financial_batch_coverage_counts_latest_observations_separately_from_pages_and_formal_facts(tmp_path):
    index, facts = _index(tmp_path, _baseline(tmp_path / "baseline"))
    reader = SQLiteCompanyEvidenceReaderFixture(facts.path)
    before = reader.publication_identity()
    codes = ("600001", "600002", "600003", "600005", "600006")
    query = BuildCompanyFinancialRoundQuery(_CUTOFF)
    index.accept(
        query,
        _page(
            _observation(period=date(2026, 6, 30)),
            _observation(code="600002", parent=0.0, update_delay=timedelta(days=-1)),
            _observation(code="600003", update_delay=None),
            _observation(code="600004", update_delay=timedelta(days=1)),
            _observation(code="600005", update_delay=timedelta(hours=3)),
            pages=2,
            total=6,
        ),
        codes,
        _CUTOFF,
    )
    partial = index.financial_batch(query, codes, _CUTOFF)
    assert (partial.accepted_pages, partial.expected_pages, partial.accepted_raw_rows) == (1, 2, 5)
    assert not partial.pages_complete
    assert partial.latest_complete_stocks == 4
    assert (partial.supplier_updates_same_day, partial.supplier_updates_later,
            partial.supplier_updates_earlier, partial.supplier_updates_missing) == (2, 0, 1, 1)
    assert index.financial_observation_watermark(query, codes, _CUTOFF) is None
    assert index.latest_financial_observations(query, codes, _CUTOFF) == ()

    successor = index.cursor(query, codes, _CUTOFF)
    assert successor is not None
    index.accept(
        successor, _page(_observation(eps=None, parent=-2.0, update_delay=timedelta(days=1)), page=2, pages=2, total=6),
        codes, _CUTOFF,
    )
    complete = index.financial_batch(query, codes, _CUTOFF)
    assert complete.pages_complete and complete.accepted_raw_rows == 6
    assert (complete.population, complete.latest_observed_stocks, complete.latest_complete_stocks) == (5, 4, 3)
    assert complete.latest_profit_pair_stocks == 4
    assert (complete.supplier_updates_same_day, complete.supplier_updates_later,
            complete.supplier_updates_earlier, complete.supplier_updates_missing) == (1, 1, 1, 1)
    assert tuple((name, count) for name, count in complete.missing_fields if count) == (("basic_eps", 1),)
    closed = index.financial_observation_watermark(query, codes, _CUTOFF)
    assert closed is not None
    assert index.financial_observation_watermark(successor, codes, _CUTOFF) == closed
    latest = index.latest_financial_observations(query, codes, _CUTOFF)
    assert tuple((item.financial.code, item.financial.parent_net_profit) for item in latest) == (
        ("600001", -2.0), ("600002", 0.0), ("600003", 100.0), ("600005", 100.0),
    )
    assert reader.publication_identity() == before
    next_cutoff = _CUTOFF + timedelta(days=1)
    assert index.financial_batch(BuildCompanyFinancialRoundQuery(next_cutoff), codes, next_cutoff).expected_pages is None


def test_financial_scoring_publishes_only_a_closed_visible_report_without_changing_formal_facts(tmp_path):
    baseline = _baseline(tmp_path / "baseline")
    index, facts = _index(tmp_path / "candidate", baseline)
    reader = SQLiteCompanyEvidenceReaderFixture(facts.path)
    observed_at = _CUTOFF + timedelta(minutes=2)
    older = _observation(period=date(2026, 6, 30))
    latest = _observation(eps=None, parent=-2.0)
    index.accept(_QUERY, _page(older, pages=2, total=2), _CODES, _CUTOFF)
    index.publish_financial_scoring(_QUERY, _CODES, _CUTOFF, eligible_codes=_CODES, available_at=observed_at)
    assert reader.read_financial_scoring_observations(_CODES, as_of=observed_at) == ()

    successor = replace(_QUERY, page=2, received_rows=1, expected_rows=2)
    index.accept(successor, _page(latest, page=2, pages=2, total=2), _CODES, _CUTOFF)
    index.publish_financial_scoring(_QUERY, _CODES, _CUTOFF, eligible_codes=_CODES, available_at=observed_at)
    identity = reader.publication_identity()
    assert reader.read_financial_scoring_observations(_CODES, as_of=_CUTOFF) == ()
    assert reader.read_financial_scoring_observations(_CODES, as_of=observed_at) == (
        CompanyFinancialScoringObservation(latest, observed_at),
    )
    coverage = reader.read_field_counts(codes=_CODES, as_of=_CUTOFF, scoring_as_of=observed_at)
    assert coverage is not None and coverage.coverage is not None
    assert coverage.coverage.financial_scoring_stocks == 1
    assert reader.read(_CODES) == baseline.facts
    with SqliteSession(facts.path, SqliteConnectionOptions(read_only=True)) as formal:
        assert ReadCommittedCompanyFinancialReports(formal, _CODES, observed_at, facts.extraction_hash) == ()
        assert ReadCompanyFinancialObservations(formal, _CODES) == (latest,)

    later = _CUTOFF + timedelta(days=1)
    index.accept(_QUERY, _page(_observation(cutoff=later, eps=None, parent=-2.0)), _CODES, later)
    index.publish_financial_scoring(_QUERY, _CODES, later, eligible_codes=_CODES, available_at=later)
    assert reader.publication_identity() == identity
    assert reader.read_financial_scoring_observations(_CODES, as_of=later)[0].available_at == observed_at

    later += timedelta(days=1)
    index.accept(_QUERY, _page(_observation(cutoff=later, eps=8.0, parent=-3.0)), _CODES, later)
    index.publish_financial_scoring(_QUERY, _CODES, later, eligible_codes=_CODES, available_at=later)
    assert reader.read_financial_scoring_observations(_CODES, as_of=later) == ()
    coverage = reader.read_field_counts(codes=_CODES, as_of=_CUTOFF, scoring_as_of=later)
    assert coverage is not None and coverage.coverage is not None
    assert coverage.coverage.financial_scoring_stocks == 0
    assert reader.read_financial_scoring_observations(_CODES, as_of=observed_at) == (
        CompanyFinancialScoringObservation(latest, observed_at),
    )


def test_financial_scoring_recovers_original_availability_after_formal_seal_failure(tmp_path, monkeypatch):
    baseline = _baseline(tmp_path / "baseline")
    index, facts = _index(tmp_path / "candidate", baseline)
    reader = SQLiteCompanyEvidenceReaderFixture(facts.path)
    observation = _observation()
    available_at = _CUTOFF + timedelta(hours=2)
    index.accept(_QUERY, _page(observation), _CODES, _CUTOFF)

    def fail_seal():
        raise OSError("injected formal seal failure")

    with monkeypatch.context() as patches:
        patches.setattr(facts, "publish_file", fail_seal)
        with pytest.raises(OSError, match="injected formal seal failure"):
            index.publish_financial_scoring(_QUERY, _CODES, _CUTOFF, eligible_codes=_CODES, available_at=available_at)
    assert index.recover_publication_seal()
    identity = reader.publication_identity()
    after_restart = available_at + timedelta(hours=1)
    index.publish_financial_scoring(_QUERY, _CODES, _CUTOFF, eligible_codes=_CODES, available_at=after_restart)
    assert reader.publication_identity() == identity
    assert reader.read_financial_scoring_observations(_CODES, as_of=after_restart) == (
        CompanyFinancialScoringObservation(observation, available_at),
    )
    assert reader.read(_CODES) == baseline.facts


def test_latest_observation_does_not_fall_back_to_earlier_row_when_newest_was_filtered(tmp_path):
    index, _ = _index(tmp_path)
    index.accept(_QUERY, _page(_observation(period=date(2026, 6, 30)), pages=2, total=2), _CODES, _CUTOFF)
    next_page = replace(_QUERY, page=2, received_rows=1, expected_rows=2)
    index.accept(next_page, _page(_observation(), page=2, pages=2, total=2), _CODES, _CUTOFF, eligible_codes=())
    assert index.financial_observation_watermark(_QUERY, _CODES, _CUTOFF) is not None
    assert index.latest_financial_observations(_QUERY, _CODES, _CUTOFF) == ()


@pytest.mark.parametrize("reason", (
    "company_round_page_number_mismatch", "company_round_total_changed",
    "company_round_observation_time_mismatch", "company_round_notice_outside_window",
))
def test_rejected_page_reports_precise_identity_failure_without_acceptance(tmp_path, reason):
    index, facts = _index(tmp_path, _baseline(tmp_path / "baseline"))
    reader = SQLiteCompanyEvidenceReaderFixture(facts.path)
    before = reader.publication_identity()
    query, observation = _QUERY, _observation()
    page = _page(observation)
    if reason == "company_round_page_number_mismatch":
        page = _page(observation, page=2, pages=2, total=2)
    elif reason == "company_round_total_changed":
        query = replace(query, expected_rows=2)
    elif reason == "company_round_observation_time_mismatch":
        page = _page(_observation(cutoff=_CUTOFF - timedelta(seconds=1)))
    else:
        query = replace(query, start=query.start - timedelta(days=2), end=query.end - timedelta(days=1))
    with pytest.raises(CompanyInputValidationFailure, match=reason):
        index.accept(query, page, _CODES, _CUTOFF)
    assert reader.publication_identity() == before
    with SqliteSession(index.path) as normalized:
        assert normalized.run("SELECT COUNT(*) FROM company_round_page").require_one()[0] == 0


class _Source:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def financial_observations(self, query, *, observed_at):
        self.calls.append(query.page)
        return self.pages[query.page]


def _run(index, source, *, budget=256, cancel=lambda: False):
    journal = SQLiteCompanySyncRoundJournal(index.path.with_suffix(".requests.sqlite3"), sqlite_policy=SqlitePolicyFixture(), publication_identity=lambda: "a" * 64)
    journal.initialize()
    return RunCompanySyncRound(journal, (CompanyFinancialProducer(index, source, _QUERY,
        qualification_gaps=("latest_financial_population_unverified",), publication_scope="isolated_candidate"),),
        CompanySyncRoundScope(_CODES, _CUTOFF, (CompanySyncFamily.FINANCIAL,)), CompanySyncBudget(budget, 2048, budget, 10),
        monotonic=lambda: 0, cancel_requested=cancel)


def test_new_report_publishes_values_and_exact_source_identity_with_risk_and_old_receipts_preserved(tmp_path):
    baseline = _baseline(tmp_path)
    cleaned = []
    index, facts = _index(tmp_path / "candidate", baseline, lambda: cleaned.append(True))
    observation = _observation(parent=-2)
    source = _Source({1: _page(observation)})
    result = _run(index, source)
    assert source.calls == [1] and result.families[0].result.changed_codes == _CODES and result.needs_retry
    with SqliteSession(facts.path) as formal:
        current, = ReadCompanyFactRows(formal, _CODES)
        assert current.latest_financial == observation.financial and current.financial == observation.financial
        assert current.risk_events == baseline.facts[0].risk_events and current.checked_at == baseline.facts[0].checked_at
        assert ReadCompanyFinancialObservations(formal, _CODES) == (observation,)
        assert ReadCompanyFieldResults(formal, _CODES) == tuple(sorted(baseline.fields, key=lambda item: (item.code, item.event_id, item.field)))
        assert formal.run("SELECT COUNT(*) FROM company_input_receipt").require_one()[0] == len(baseline.receipts) + 1
        assert formal.run("SELECT record_hash FROM company_input_receipt WHERE source='eastmoney_financial'").require_one()[0] == observation.record_hash
        before = ReadCompanyPublicationIdentity(formal)
    restarted, _ = _index(tmp_path / "candidate", baseline)
    assert _run(restarted, source).families[0].result.changed_codes == _CODES and source.calls == [1]
    with SqliteSession(facts.path) as formal:
        assert ReadCompanyPublicationIdentity(formal) == before
    assert cleaned == []
    index.clean()
    assert cleaned == [True]


def test_latest_incomplete_report_keeps_the_previous_complete_report_without_cross_filling(tmp_path):
    baseline = _baseline(tmp_path)
    index, facts = _index(tmp_path / "candidate", baseline)
    observation = _observation(eps=None, parent=-2)
    _run(index, _Source({1: _page(observation)}))
    with SqliteSession(facts.path) as formal:
        current, = ReadCompanyFactRows(formal, _CODES)
    assert current.financial == baseline.facts[0].financial
    assert current.latest_financial == observation.financial and current.latest_financial.basic_eps is None


def test_partial_page_budget_resumes_without_repeating_the_committed_page(tmp_path):
    index, facts = _index(tmp_path / "candidate", _baseline(tmp_path))
    source = _Source({1: _page(_observation(), page=1, pages=2, total=2),
                      2: _page(_observation(code="600002"), page=2, pages=2, total=2)})
    with SqliteSession(facts.path) as formal:
        before = ReadCompanyPublicationIdentity(formal)
    assert not _run(index, source, budget=1).families[0].result.processed_scope_complete
    with SqliteSession(facts.path) as formal:
        assert ReadCompanyPublicationIdentity(formal) == before
    assert _run(index, source).families[0].result.processed_scope_complete and source.calls == [1, 2]


@pytest.mark.parametrize("outside", (False, True))
def test_duplicate_business_key_across_pages_is_rejected_even_outside_population(tmp_path, outside):
    index, _ = _index(tmp_path / "candidate", _baseline(tmp_path))
    observation = _observation(code="600002" if outside else "600001")
    index.accept(_QUERY, _page(observation, page=1, pages=2, total=2), _CODES, _CUTOFF)
    cursor = index.cursor(_QUERY, _CODES, _CUTOFF)
    with pytest.raises(ValueError, match="duplicate_report"):
        index.accept(cursor, _page(observation, page=2, pages=2, total=2), _CODES, _CUTOFF)


def test_same_report_changed_values_or_null_without_revision_proof_never_overwrite(tmp_path):
    index, facts = _index(tmp_path / "candidate", _baseline(tmp_path))
    original = _observation()
    _run(index, _Source({1: _page(original)}))
    later = _CUTOFF + timedelta(minutes=1)
    changed = _observation(eps=None, cutoff=later)
    index.accept(_QUERY, _page(changed), _CODES, later)
    with SqliteSession(index.path) as normalized:
        assert set(ReadCompanyFinancialObservations(normalized, _CODES)) == {original, changed}
    with pytest.raises(ValueError, match="revision_unproven"):
        index.normalize(_QUERY, _CODES, later)
    with SqliteSession(facts.path) as formal:
        assert ReadCompanyFactRows(formal, _CODES)[0].latest_financial.basic_eps == 2
        assert ReadCompanyFinancialObservations(formal, _CODES) == (original,)
        identity = ReadCompanyPublicationIdentity(formal)
        assert identity is not None
        assert ReadCommittedCompanyFinancialReports(formal, _CODES, later, identity.extraction_hash) == (original.financial,)
        assert not ReadCommittedCompanyFinancialReports(
            formal, _CODES, _CUTOFF - timedelta(seconds=1), identity.extraction_hash,
        )


def test_cancelled_late_source_response_retains_accepted_input_and_never_repeats_the_page(tmp_path):
    index, _ = _index(tmp_path / "candidate", _baseline(tmp_path))
    cancelled = [False]

    class Source(_Source):
        def financial_observations(self, *args, **kwargs):
            result = super().financial_observations(*args, **kwargs)
            cancelled[0] = True
            return result

    source = Source({1: _page(_observation())})
    assert _run(index, source, cancel=lambda: cancelled[0]).cancelled
    assert index.cursor(_QUERY, _CODES, _CUTOFF) is None
    _run(index, source)
    assert source.calls == [1]


def test_formal_file_seal_failure_recovers_exact_committed_intent_without_new_source(tmp_path, monkeypatch):
    baseline = _baseline(tmp_path)
    index, facts = _index(tmp_path / "candidate", baseline)
    source = _Source({1: _page(_observation())})
    with monkeypatch.context() as patch:
        patch.setattr(facts, "publish_file", lambda: (_ for _ in ()).throw(RuntimeError("fixture_seal_failure")))
        with pytest.raises(RuntimeError, match="fixture_seal_failure"):
            _run(index, source)
    restarted, _ = _index(tmp_path / "candidate", baseline)
    assert _run(restarted, source).families[0].result.changed_codes == () and source.calls == [1]
    with SqliteSession(index.path) as normalized:
        assert normalized.run("SELECT confirmed_content_hash IS NOT NULL FROM company_round_intent").require_one()[0]
    assert SQLiteCompanyEvidenceReaderFixture(facts.path).read(_CODES)[0].latest_financial == _observation().financial


@pytest.mark.parametrize("kind", ("facts", "fields", "metadata", "receipt"))
def test_marker_recovery_rejects_changed_material_even_if_the_formal_head_was_not_changed(tmp_path, monkeypatch, kind):
    baseline = _baseline(tmp_path)
    index, facts = _index(tmp_path / "candidate", baseline)
    source = _Source({1: _page(_observation())})
    with monkeypatch.context() as patch:
        patch.setattr(facts, "publish_file", lambda: (_ for _ in ()).throw(RuntimeError("fixture_seal_failure")))
        with pytest.raises(RuntimeError, match="fixture_seal_failure"):
            _run(index, source)
    with SqliteSession(facts.path) as formal:
        head = ReadCompanyPublicationIdentity(formal)
        if kind == "facts":
            formal.run("UPDATE company_facts SET parent_profit=parent_profit+100 WHERE code='600001'")
        elif kind == "fields":
            formal.run("UPDATE company_field_result SET reason='fixture_tampered_proof' WHERE code='600001'")
        elif kind == "metadata":
            formal.run("UPDATE company_financial_observation SET observed_at=?", ((_CUTOFF + timedelta(minutes=1)).isoformat(),))
        else:
            formal.run("DELETE FROM company_input_receipt WHERE source='eastmoney_financial'")
        assert ReadCompanyPublicationIdentity(formal) == head
    restarted, writer = _index(tmp_path / "candidate", baseline)
    sealed = []
    monkeypatch.setattr(writer, "publish_file", lambda: sealed.append(True))
    with pytest.raises(ValueError, match="confirmation_changed"):
        _run(restarted, source)
    assert not sealed and source.calls == [1]
    with SqliteSession(index.path) as normalized:
        assert normalized.run("SELECT confirmed_content_hash FROM company_round_intent").require_one()[0] is None


def test_empty_cohort_does_not_upgrade_or_refresh_an_unreturned_issuer(tmp_path):
    baseline = _baseline(tmp_path)
    from trader.download.domain.company_events import CompanyDataState

    baseline = replace(baseline, facts=(replace(baseline.facts[0], financial_state=CompanyDataState.PARTIAL),))
    index, facts = _index(tmp_path / "candidate", baseline)
    _run(index, _Source({1: _page()}))
    with SqliteSession(facts.path) as formal:
        assert ReadCompanyFactRows(formal, _CODES) == baseline.facts


@pytest.mark.parametrize("target", ("normalized", "formal"))
def test_seed_file_seal_failure_resumes_the_exact_baseline_and_seals_before_completion(tmp_path, monkeypatch, target):
    baseline = _baseline(tmp_path)
    index, facts = _index(tmp_path / "candidate")
    writer = index._normalized if target == "normalized" else facts
    with monkeypatch.context() as patch:
        patch.setattr(writer, "publish_file", lambda: (_ for _ in ()).throw(RuntimeError("fixture_seed_seal_failure")))
        with pytest.raises(RuntimeError, match="fixture_seed_seal_failure"):
            index.seed(baseline)
    restarted, _ = _index(tmp_path / "candidate", baseline)
    assert SQLiteCompanyEvidenceReaderFixture(facts.path).read(_CODES) == baseline.facts
    assert restarted.cursor(_QUERY, _CODES, _CUTOFF).page == 1


def test_successor_scope_cannot_be_published_using_the_previous_scope_generation(tmp_path):
    index, facts = _index(tmp_path / "candidate", _baseline(tmp_path))
    index.accept(_QUERY, _page(_observation()), _CODES, _CUTOFF)
    index.normalize(_QUERY, _CODES, _CUTOFF)
    later = _CUTOFF + timedelta(days=180)
    financial = replace(_observation().financial, report_date=date(2026, 12, 31), published_at=later - timedelta(days=1))
    observation = CompanyFinancialSourceObservation(
        financial, "600001.SH", financial.published_at, financial.published_at, later, "CNY",
        CompanyFinancialSourceRecordHash(financial, "600001.SH", financial.published_at, financial.published_at, "CNY"), (),
    )
    query = StructuredCompanyQuery("financial", financial.published_at.date(), financial.published_at.date())
    index.accept(query, _page(observation), _CODES, later)
    index.normalize(query, _CODES, later)
    with SqliteSession(facts.path) as formal:
        before = ReadCompanyPublicationIdentity(formal)
    with pytest.raises(ValueError, match="scope_changed"):
        index.publish_normalized(_QUERY, _CODES, _CUTOFF)
    with SqliteSession(facts.path) as formal:
        assert ReadCompanyPublicationIdentity(formal) == before


@pytest.mark.parametrize("case", ("old", "future", "bool_budget", "negative_infinite_deadline", "nan_deadline"))
def test_invalid_round_request_is_rejected_before_source_or_index_access(case):
    query, budget, deadline = _QUERY, 1, 10.0
    if case == "old":
        query = replace(query, start=_CUTOFF.date() - timedelta(days=366))
    elif case == "future":
        query = replace(query, end=_CUTOFF.date() + timedelta(days=1))
    elif case == "bool_budget":
        budget = True
    elif case == "negative_infinite_deadline":
        deadline = float("-inf")
    else:
        deadline = float("nan")
    with pytest.raises(ValueError, match="company_(round_window|sync_budget)_invalid"):
        CompanySyncBudget(budget, 2048, 1, deadline)
        CompanyFinancialProducer(None, None, query, qualification_gaps=()).state(CompanySyncRoundScope(_CODES, _CUTOFF))


def test_explicit_replay_reads_the_current_source_wal_instead_of_the_old_main_file(tmp_path):
    from scripts.download.company.company_financial_replay import AuditCompanyNormalizedRound

    legacy = fixtures._index(tmp_path / "legacy")
    _contract(legacy)
    row = {
        "SECUCODE": "600001.SH", "SECURITY_CODE": "600001", "REPORT_DATE": "2026-09-30",
        "REPORT_TYPE": "三季报", "NOTICE_DATE": _NOTICE.isoformat(), "UPDATE_DATE": _NOTICE.isoformat(),
        "CURRENCY": "CNY", "EPSJB": 2, "BPS": 5, "TOTALOPERATEREVETZ": 1, "PARENTNETPROFITTZ": 3,
        "KCFJCXSYJLRTZ": 4, "ROEJQ": 5, "PARENTNETPROFIT": 100, "KCFJCXSYJLR": 90,
    }
    page = tmp_path / "source-page.json"
    page.write_text(json.dumps({"code": 0, "success": True, "result": {"count": 1, "pages": 1, "data": [row]}}))
    with SqliteSession(legacy.facts_path, SqliteConnectionOptions(journal_mode="WAL")) as writer:
        writer.run("PRAGMA wal_autocheckpoint=0")
        writer.run("UPDATE company_publication SET content_hash=?", (b"w" * 32,))
        assert legacy.facts_path.with_name(legacy.facts_path.name + "-wal").stat().st_size > 0
        PublishSqliteFile(legacy.facts_path, sqlite_policy=SqlitePolicyFixture())
        report = AuditCompanyNormalizedRound(
            collection_path=legacy.path, facts_path=legacy.facts_path, documents=legacy._documents,
            page_paths=(page,), query=_QUERY, observed_at=_CUTOFF, directory=tmp_path / "replay",
            event_policy=fixtures.CompanyEventPolicy(), sqlite_policy=SqlitePolicyFixture(), budget_seconds=30,
        )
    assert report["source_publication_hash"] == (b"w" * 32).hex()
    assert report["completed_financial_scope"] and report["consumer_equal"]


def test_explicit_replay_rejects_unsealed_source_before_text_or_new_candidate_writes(tmp_path, monkeypatch):
    from scripts.download.company.company_financial_replay import AuditCompanyNormalizedRound

    legacy = fixtures._index(tmp_path / "legacy")
    _contract(legacy)
    monkeypatch.setattr(legacy._documents, "read", lambda *args, **kwargs: pytest.fail("unsealed source must not authorize reproof"))
    directory = tmp_path / "rejected"
    with SqliteSession(legacy.facts_path, SqliteConnectionOptions(journal_mode="WAL")) as writer:
        writer.run("UPDATE company_publication SET content_hash=?", (b"w" * 32,))
        with pytest.raises(SqlitePublicationError, match="checksum"):
            AuditCompanyNormalizedRound(
                collection_path=legacy.path, facts_path=legacy.facts_path, documents=legacy._documents,
                page_paths=(tmp_path / "unused-page.json",), query=_QUERY, observed_at=_CUTOFF, directory=directory,
                event_policy=fixtures.CompanyEventPolicy(), sqlite_policy=SqlitePolicyFixture(), budget_seconds=30,
            )
    assert not directory.exists()


def test_normal_company_entrypoint_dispatches_a_finite_round_with_source_gaps(monkeypatch):
    from trader import bootstrap
    from trader.download.domain.company_sync import COMPANY_SYNC_FAMILIES
    from tests.helpers.download.company_round import CompanySyncFamilyFixture, CompanySyncRoundFixture
    from tests.helpers.shared.configuration import RuntimeSettingsFixture
    from tests.helpers.shared.reference_fixtures import PreparedFixture

    settings = RuntimeSettingsFixture()
    reference = PreparedFixture(_CUTOFF)
    expected = CompanySyncRoundFixture(_CUTOFF, families=tuple(
        CompanySyncFamilyFixture(family, gaps=("source_qualification_pending",)) for family in COMPANY_SYNC_FAMILIES
    ))
    monkeypatch.setattr(bootstrap, "LoadRuntimeSettings", lambda _: settings)
    monkeypatch.setattr(bootstrap, "_prepare_reference", lambda *args, **kwargs: reference)
    calls = []

    def execute(actual_settings, actual_scope, **kwargs):
        calls.append((actual_settings, actual_scope.codes, actual_scope.families))
        return expected

    monkeypatch.setattr(bootstrap, "ExecuteCompanySync", execute)
    result = bootstrap.ExecuteSync(settings.config_path, "company")
    assert calls == [(settings, tuple(item.code for item in reference.eligible_universe), COMPANY_SYNC_FAMILIES)]
    assert result.tasks[0].company is expected and result.tasks[0].status == "pending"
    assert result.exit_code == 1


def test_normalized_and_formal_paths_reject_same_file_before_initialization(tmp_path):
    facts = SQLiteCompanyFactsIndexFixture(tmp_path / "same.sqlite3")
    with pytest.raises(ValueError, match="paths_must_differ"):
        SQLiteCompanyNormalizedIndex(facts.path, facts, event_policy=fixtures.CompanyEventPolicy(),
            sqlite_policy=SqlitePolicyFixture(), clean_material=lambda: None)
    assert not facts.path.exists()
    facts.path.write_bytes(b"unchanged")
    other = tmp_path / "same-inode.sqlite3"
    other.hardlink_to(facts.path)
    with pytest.raises(ValueError, match="paths_must_differ"):
        SQLiteCompanyNormalizedIndex(other, facts, event_policy=fixtures.CompanyEventPolicy(),
            sqlite_policy=SqlitePolicyFixture(), clean_material=lambda: None)
    assert facts.path.read_bytes() == b"unchanged"


def test_prepared_field_snapshot_rejects_incomplete_issuer_without_any_commit(tmp_path):
    baseline = _baseline(tmp_path)
    index, facts = _index(tmp_path / "candidate", baseline)
    assert len(baseline.fields) > 1
    before = facts.path.read_bytes(), index.path.read_bytes()
    with pytest.raises(ValueError, match="coverage_incomplete"):
        index.publish_fields(_CODES, baseline.fields[:1], (), _CUTOFF, field_rules_hash=CompanyFieldProofRulesHash())
    assert (facts.path.read_bytes(), index.path.read_bytes()) == before


def test_prepared_fields_resume_after_normalized_commit_before_generation(tmp_path, monkeypatch):
    baseline = _baseline(tmp_path)
    index, facts = _index(tmp_path / "candidate", baseline)
    fields = tuple(replace(item, state="unsupported", reason="explicit_reproof_gap", evidence=()) for item in baseline.fields)
    original = index._normalized.publish
    def stop_after_stage(*args, **kwargs):
        original(*args, **kwargs)
        raise InterruptedError("after_normalized_commit")
    monkeypatch.setattr(index._normalized, "publish", stop_after_stage)
    with pytest.raises(InterruptedError, match="after_normalized_commit"):
        index.publish_fields(_CODES, fields, (), _CUTOFF, field_rules_hash=CompanyFieldProofRulesHash())
    restarted, _ = _index(tmp_path / "candidate")
    assert restarted.publish_fields(_CODES, fields, (), _CUTOFF, field_rules_hash=CompanyFieldProofRulesHash()) == _CODES
    with SqliteSession(facts.path) as formal:
        assert ReadCompanyFieldResults(formal, _CODES) == tuple(sorted(fields, key=lambda item: (item.code,item.event_id,item.field)))
        head = ReadCompanyPublicationIdentity(formal)
    assert restarted.publish_fields(_CODES, fields, (), _CUTOFF, field_rules_hash=CompanyFieldProofRulesHash()) == ()
    with SqliteSession(facts.path) as formal:
        assert ReadCompanyPublicationIdentity(formal) == head


def test_local_field_reproof_cannot_stamp_a_missing_global_rule_receipt(tmp_path):
    baseline = _baseline(tmp_path)
    first = baseline.facts[0]
    second = replace(first, code="600002",
        financial=replace(first.financial,code="600002") if first.financial else None,
        latest_financial=replace(first.latest_financial,code="600002") if first.latest_financial else None)
    fields = tuple(replace(field,state="unsupported",reason="explicit_reproof_gap",evidence=()) for field in baseline.fields)
    others = tuple(replace(field,code="600002") for field in fields)
    writer = SQLiteCompanyFactsIndexFixture(tmp_path / "formal.sqlite3")
    writer.initialize()
    head = writer.publish((first,second),dict(baseline.sources),None,_CUTOFF,field_results=(*fields,*others)).identity
    with pytest.raises(ValueError, match="full_reproof_required"):
        writer.publish((),{},head,_CUTOFF,field_results=fields,field_rules_hash=CompanyFieldProofRulesHash())
    with SqliteSession(writer.path) as formal:
        assert ReadCompanyPublicationIdentity(formal) == head
        assert formal.run("SELECT rules_hash FROM company_field_proof_rule").fetchone() is None
    marked = writer.publish((),{},head,_CUTOFF,field_results=(*fields,*others),field_rules_hash=CompanyFieldProofRulesHash()).identity
    # A normal writer without a current-rule attestation revokes it in the same commit.
    revoked = writer.publish((first,),dict(baseline.sources),marked,_CUTOFF,field_results=fields).identity
    assert marked != revoked
    with SqliteSession(writer.path) as formal:
        assert formal.run("SELECT rules_hash FROM company_field_proof_rule").fetchone() is None
