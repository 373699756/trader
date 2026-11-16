"""Preparation, atomic formal commit and collection acknowledgement remain distinct."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from tests.helpers.download.company import company_evidence as fixtures
from tests.helpers.download.company.company_annual_income import _Sources, _contract
from tests.helpers.download.company_resources import SQLiteCompanyFactsIndexFixture
from tests.helpers.shared.reference_fixtures import PreparedFixture
from trader.download.application.company_evidence import CompanyEvidenceDependencies, _SyncState, _publish_batch
from trader.download.application.company_operations import CompanyOperations
from trader.download.application.company_publication import CompanyPublicationConflict, PublishCompanyEvidence
from trader.download.application.sync_data import SyncData
from trader.entrypoints.cli import Main
from trader.download.infra import company_publication_commit
from trader.download.infra.company_fact_rows import ReadCompanyFactRows
from trader.download.infra.company_facts_index import ReadCompanyPublicationIdentity
from trader.download.infra.company_field_result_rows import ReadCompanyFieldResults
from trader.download.domain.company_evidence import DisclosurePage, DisclosurePartition
from trader.infra.sqlite_connection import SqliteSession


def _identity(index):
    with SqliteSession(index.facts_path) as formal:
        return ReadCompanyPublicationIdentity(formal)


def _prepare(tmp_path):
    index = fixtures._index(tmp_path)
    _contract(index)
    item = fixtures._disclosure("new-risk", "关于收到中国证监会立案告知书的公告")
    index.commit_page(DisclosurePartition("sse", fixtures.NOW.date(), fixtures.NOW.date()),
                      DisclosurePage((item,), 1, False), fixtures.NOW)
    index.commit_document(item, fixtures._extraction("本公司收到中国证监会立案告知书，公司涉嫌信息披露违法违规。"), fixtures.NOW)
    return index, item


def _advance_formal(index):
    with SqliteSession(index.facts_path) as formal:
        parent = ReadCompanyPublicationIdentity(formal)
        facts = ReadCompanyFactRows(formal, ("600001",))
        fields = ReadCompanyFieldResults(formal, ("600001",))
        sources = {row[0]: "cninfo" for row in formal.run("SELECT source_key FROM company_evidence_ref")}
    at = fixtures.NOW + timedelta(seconds=1)
    writer = SQLiteCompanyFactsIndexFixture(index.facts_path)
    return writer.publish(tuple(replace(fact, published_at=at, checked_at=at) for fact in facts), sources, parent, at,
                          field_results=fields)


def _acknowledged(index, item):
    with SqliteSession(index.path) as collected:
        return collected.run("SELECT count(*) FROM document_publications p JOIN disclosures d ON d.id=p.disclosure_id "
                             "WHERE d.source_key=?", (item.identity,)).require_one()[0]


def test_preparation_reads_text_but_commit_and_confirmation_do_not(tmp_path, monkeypatch):
    index, item = _prepare(tmp_path)
    read = index._documents.read
    phase, reads = ["prepare"], []

    def checked_read(*args, **kwargs):
        assert phase[0] == "prepare", "formal commit/confirmation must consume the immutable candidate"
        reads.append(args[0])
        return read(*args, **kwargs)

    monkeypatch.setattr(index._documents, "read", checked_read)
    before = _identity(index)
    with index.publication(item.codes, fixtures.NOW) as session:
        assert session is not None and reads and _identity(index) == before
        assert _acknowledged(index, item) == 0
        phase[0] = "commit"
        assert session.commit() == item.codes
        assert _identity(index) != before and _acknowledged(index, item) == 0
        phase[0] = "confirm"
        session.confirm()
    assert _acknowledged(index, item) == 1


def test_source_change_after_preparation_rejects_the_original_candidate(tmp_path):
    index, item = _prepare(tmp_path)
    before = _identity(index)
    with index.publication(item.codes, fixtures.NOW) as session:
        assert session is not None
        with SqliteSession(index.path) as other:
            other.run("UPDATE financials SET eps=eps+1 WHERE code='600001'")
        with pytest.raises(CompanyPublicationConflict, match="company_publication_source_changed"):
            session.commit()
    assert _identity(index) == before and _acknowledged(index, item) == 0


def test_parent_change_after_preparation_is_not_overwritten(tmp_path):
    index, item = _prepare(tmp_path)
    with index.publication(item.codes, fixtures.NOW) as session:
        assert session is not None
        other = _advance_formal(index)
        with pytest.raises(CompanyPublicationConflict, match="company_publication_parent_changed"):
            session.commit()
    assert _identity(index) == other.identity and _acknowledged(index, item) == 0


def test_formal_writer_returns_its_own_transaction_identity_when_another_writer_wins_before_return(tmp_path, monkeypatch):
    index, item = _prepare(tmp_path)
    seal = index._facts.publish_file
    other = []

    def intervening_writer():
        seal()
        other.append(_advance_formal(index))

    monkeypatch.setattr(index._facts, "publish_file", intervening_writer)
    with index.publication(item.codes, fixtures.NOW) as session:
        assert session is not None
        session.commit()
        assert len(other) == 1 and _identity(index) == other[0].identity
        with pytest.raises(RuntimeError, match="company_publication_confirmation_changed"):
            session.confirm()
    assert _acknowledged(index, item) == 0
    with SqliteSession(index.path) as collected:
        assert collected.run("SELECT count(*) FROM company_dirty_codes WHERE code='600001'").require_one()[0] == 1


def test_failed_confirmation_preserves_formal_receipts_and_restart_recovers_without_new_input(tmp_path, monkeypatch):
    index, item = _prepare(tmp_path)

    def fail(*args):
        raise RuntimeError("fixture_confirmation_failed")

    with monkeypatch.context() as patch:
        patch.setattr(company_publication_commit, "WriteCompanyFieldAttempts", fail)
        with pytest.raises(RuntimeError, match="fixture_confirmation_failed"):
            PublishCompanyEvidence(index, item.codes, fixtures.NOW)
    assert _acknowledged(index, item) == 0
    with SqliteSession(index.facts_path) as formal:
        assert formal.run("SELECT count(*) FROM company_input_receipt WHERE source_key=? AND outcome='valid'",
                          (item.identity,)).require_one()[0] == 1
    restarted = fixtures._index(tmp_path)
    restarted.prepare_and_publish(item.codes, fixtures.NOW)
    assert _acknowledged(restarted, item) == 1


def test_preparation_initialization_no_longer_publishes_in_a_hidden_callback(tmp_path):
    index, item = _prepare(tmp_path)
    before = _identity(index)
    assert index.prepare_publication(item.codes, fixtures.NOW)
    assert _identity(index) == before and _acknowledged(index, item) == 0
    PublishCompanyEvidence(index, item.codes, fixtures.NOW, changed_only=True)
    assert _acknowledged(index, item) == 1


def test_session_rejects_repeated_or_out_of_context_operations(tmp_path):
    index, item = _prepare(tmp_path)
    with index.publication(item.codes, fixtures.NOW) as session:
        assert session is not None
        session.commit()
        with pytest.raises(RuntimeError, match="already_committed"):
            session.commit()
        session.confirm()
        with pytest.raises(RuntimeError, match="not_committed"):
            session.confirm()
    with pytest.raises(RuntimeError, match="already_committed"):
        session.commit()


def test_followups_are_bounded_and_pending_is_cleared_only_by_a_later_real_checkpoint(tmp_path, monkeypatch):
    index, item = _prepare(tmp_path)
    source = fixtures.Sources()
    events = []
    dependencies = CompanyEvidenceDependencies(index, source, source, lambda: 0, events.append)
    state = _SyncState(CompanyOperations(events.append, lambda: 0, ()), 1, failures=("financial:timeout",))
    supersessions = company_publication_commit.PublishCompanySupersessions
    rounds = []

    def repeated_dirty(*args):
        rounds.append(fixtures.NOW)
        return item.codes

    with monkeypatch.context() as patch:
        patch.setattr(company_publication_commit, "PublishCompanySupersessions", repeated_dirty)
        assert _publish_batch(dependencies, item.codes, fixtures.NOW, 0, state) == 1
    assert len(rounds) == 2 and state.publication_pending_codes == item.codes
    assert state.failures == ("financial:timeout", "publication:pending")
    assert state.publication_required
    pending_event = [event for event in events if event.component == "publish"][-1]
    assert pending_event.status == "deferred" and pending_event.pending == 1
    assert pending_event.failure == "publication:pending"
    with SqliteSession(index.path) as collected:
        assert collected.run("SELECT count(*) FROM company_dirty_codes WHERE code='600001'").require_one()[0] == 1
    assert company_publication_commit.PublishCompanySupersessions is supersessions
    assert _publish_batch(dependencies, item.codes, fixtures.NOW, 0, state) == 1
    assert state.publication_pending_codes == () and state.failures == ("financial:timeout",)
    assert not state.publication_required
    assert [event for event in events if event.component == "publish"][-1].status == "complete"


def test_real_sync_pending_publication_remains_visible_and_cli_requests_retry(tmp_path, monkeypatch, capsys):
    index, item = _prepare(tmp_path)
    contract = _contract(index)

    class Source(_Sources):
        def fetch(self, query, **kwargs):
            if isinstance(query, DisclosurePartition):
                rows = tuple(row for row in (contract, item) if query.market == "sse"
                             and query.start <= row.published_at.date() <= query.end)
                return DisclosurePage(rows, len(rows), False)
            return super().fetch(query, **kwargs)

    source, events = Source(contract), []
    monkeypatch.setattr(company_publication_commit, "PublishCompanySupersessions", lambda *args: item.codes)
    dependencies = CompanyEvidenceDependencies(index, source, source, lambda: 0, events.append)
    company = fixtures._sync_company_evidence(dependencies, item.codes, fixtures.NOW)
    assert company.failures == ("publication:pending",) and company.needs_retry
    terminal = events[-1]
    assert terminal.status == "deferred" and terminal.failure == "publication:pending"

    def forbidden(_reference):
        raise AssertionError("company sync must not execute unrelated tasks")

    result = SyncData(lambda: PreparedFixture(fixtures.NOW), forbidden, forbidden,
                      lambda _: company, lambda: False).execute("company")
    assert result.tasks[0].status == "pending" and result.exit_code == 1
    monkeypatch.setattr("trader.bootstrap.ExecuteSync", lambda *args, **kwargs: result)
    config = Path(__file__).resolve().parents[4] / "config/runtime.json"
    assert Main(["--config", str(config), "sync", "company"]) == 1
    assert '"publication:pending"' in capsys.readouterr().out
