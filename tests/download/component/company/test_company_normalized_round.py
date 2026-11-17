"""The candidate round freezes inputs before pure publication and exact recovery."""

from dataclasses import replace
from datetime import date, timedelta
import json

import pytest

from tests.helpers.download.company import company_evidence as fixtures
from tests.helpers.download.company.company_annual_income import _contract
from tests.helpers.download.company_resources import SQLiteCompanyEvidenceReaderFixture, SQLiteCompanyFactsIndexFixture
from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.download.application.company_financial_producer import CompanyFinancialProducer
from trader.download.application.company_sync import RunCompanySyncRound
from trader.download.domain.company_sync import CompanySyncBudget, CompanySyncFamily, CompanySyncRoundScope
from trader.download.infra.company_sync_journal import SQLiteCompanySyncRoundJournal
from trader.download.domain.company_evidence import CompanyFinancial, DisclosurePage, DisclosurePartition, StructuredCompanyQuery
from trader.download.domain.company_source_records import (
    CompanyFinancialMissingField, CompanyFinancialSourceObservation, CompanyFinancialSourcePage,
    CompanyFinancialSourceRecordHash, CompanyFinancialSourceValues,
)
from trader.download.infra.company_fact_rows import ReadCompanyFactRows
from trader.download.infra.company_field_result_rows import ReadCompanyFieldResults
from trader.download.infra.company_field_publication import CompanyFieldProofRulesHash, PrepareCompanyFieldResults
from trader.download.infra.company_facts_index import CompanyInputReceipt, ReadCompanyPublicationIdentity
from trader.download.infra.company_facts_protocol import DecodeCompanyLabel, EVIDENCE_SOURCES, RequireCompanyTime
from trader.download.infra.company_financial_observations import ReadCompanyFinancialObservations
from trader.download.infra.company_normalized_index import CompanyNormalizationBaseline, SQLiteCompanyNormalizedIndex
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


def _observation(*, code="600001", at=_NOTICE, cutoff=_CUTOFF, eps=2.0, parent=100.0):
    financial = CompanyFinancial(code, date(2026, 9, 30), at, eps, 5, 1, 3, 4, 5, parent, 90)
    source_key = code + ".SH"
    return CompanyFinancialSourceObservation(
        financial, source_key, at, at, cutoff, "CNY",
        CompanyFinancialSourceRecordHash(financial, source_key, at, at, "CNY"),
        tuple(CompanyFinancialMissingField(field) for field, value in CompanyFinancialSourceValues(financial) if value is None),
    )


def _page(*observations, page=1, pages=1, total=None):
    return CompanyFinancialSourcePage(page, pages, len(observations) if total is None else total, len(observations), tuple(observations), "a" * 64)


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
    _run(index, _Source({1: _page(_observation())}))
    later = _CUTOFF + timedelta(minutes=1)
    with pytest.raises(ValueError, match="revision_unproven"):
        index.accept(_QUERY, _page(_observation(eps=None, cutoff=later)), _CODES, later)
    with SqliteSession(facts.path) as formal:
        assert ReadCompanyFactRows(formal, _CODES)[0].latest_financial.basic_eps == 2


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


@pytest.mark.parametrize("case", ("old", "future", "bool_budget", "infinite_deadline"))
def test_invalid_round_request_is_rejected_before_source_or_index_access(case):
    query, budget, deadline = _QUERY, 1, 10.0
    if case == "old":
        query = replace(query, start=_CUTOFF.date() - timedelta(days=366))
    elif case == "future":
        query = replace(query, end=_CUTOFF.date() + timedelta(days=1))
    elif case == "bool_budget":
        budget = True
    else:
        deadline = float("inf")
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
    from trader.download.domain.company_sync import CompanySyncFamily
    from tests.helpers.download.company_round import CompanySyncFamilyFixture, CompanySyncRoundFixture
    from tests.helpers.shared.configuration import RuntimeSettingsFixture
    from tests.helpers.shared.reference_fixtures import PreparedFixture

    settings = RuntimeSettingsFixture()
    reference = PreparedFixture(_CUTOFF)
    expected = CompanySyncRoundFixture(_CUTOFF, families=tuple(
        CompanySyncFamilyFixture(family, gaps=("source_qualification_pending",)) for family in CompanySyncFamily
    ))
    monkeypatch.setattr(bootstrap, "LoadRuntimeSettings", lambda _: settings)
    monkeypatch.setattr(bootstrap, "_prepare_reference", lambda *args, **kwargs: reference)
    calls = []

    def execute(actual_settings, actual_scope, **kwargs):
        calls.append((actual_settings, actual_scope.codes, kwargs["fill"]))
        return expected

    monkeypatch.setattr(bootstrap, "ExecuteCompanySync", execute)
    result = bootstrap.ExecuteSync(settings.config_path, "company")
    assert calls == [(settings, tuple(item.code for item in reference.eligible_universe), False)]
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
