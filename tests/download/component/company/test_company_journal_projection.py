"""The incremental journal view follows committed steps, exact receipts and external commits."""

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.download.component.company.test_company_sync_round import _SCOPE
from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.download.domain.company_sync import CompanySyncFamily, CompanySyncPhasePlan, CompanySyncPhaseState, CompanySyncStepResult
from trader.download.infra.company_journal_projection import CompanyJournalProjection
from trader.download.infra.company_sync_journal import SQLiteCompanySyncRoundJournal
from trader.infra.http_contracts import HttpAttempt, HttpRequest
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction


def test_cached_writer_and_independent_reader_follow_recovery_external_update_and_rollback(tmp_path):
    path = tmp_path / "journal.sqlite3"
    policy = SqlitePolicyFixture()
    journal = SQLiteCompanySyncRoundJournal(path, sqlite_policy=policy, publication_identity=lambda: "")
    journal.initialize()
    family = CompanySyncFamily.ANNOUNCEMENTS
    scope = replace(_SCOPE, families=(family,))
    round_id = journal.begin(scope, (CompanySyncPhasePlan(family, "a" * 64),), max_source_calls=None)
    with SqliteSession(path, sqlite_policy=policy) as owner, SqliteSession(path, sqlite_policy=policy) as external:
        candidate = CompanyJournalProjection(owner)
        def check(used, unknown, error, gaps, accepted, changed):
            reference = journal.family(round_id, family)
            with SqliteTransaction(owner):
                result = candidate.family(round_id, family)
                assert candidate.request_counts(round_id) == (used, unknown)
            assert result == reference and result is not None
            assert (result.source_calls, result.unknown_attempts, result.accepted_inputs) == (used, unknown, accepted)
            assert (result.result.failure, result.result.gaps, result.result.changed_codes) == (error, gaps, changed)

        def step(connection, seq, key, gaps, failure, accepted, codes="[]", deferred=0):
            connection.run("INSERT INTO company_sync_step VALUES(?,?,?,?,1,1,0,1,?,?,?,NULL,?,?)",
                (round_id, family.value, seq, key, accepted, codes, gaps, failure, deferred))

        with SqliteTransaction(owner):
            step(owner, 1, "first", '["source_attempt_unconfirmed"]', "source_attempt_unconfirmed", 0)
            owner.run("INSERT INTO company_sync_request VALUES(1,?,?,?,?, 'www.cninfo.com.cn','POST',0,NULL,'')",
                (round_id, family.value, 1, "x"))
        check(1, 1, "source_attempt_unconfirmed", ("source_attempt_unconfirmed",), 0, ())
        with SqliteTransaction(owner):
            owner.run("UPDATE company_sync_request SET observed=1,status=200 WHERE attempt=1")
            step(owner, 2, "recovered", '["directory_response_recovered:1"]', None, 2, '["600001"]')
        check(1, 0, None, (), 2, ("600001",))
        with SqliteTransaction(owner) as transaction:
            step(owner, 3, "aborted", '["phantom"]', "bad", 150)
            transaction.rollback()
        check(1, 0, None, (), 2, ("600001",))
        with SqliteTransaction(owner):
            step(owner, 3, "document", '["announcement_document_failed"]', "timeout", 0)
            step(owner, 4, "other", '["wait"]', None, None, '["600009"]', 1)
        check(1, 0, "timeout", ("announcement_document_failed", "wait"), None, ("600001",))
        with SqliteTransaction(owner):
            step(owner, 5, "document", "[]", None, 1, '["600002"]')
        check(1, 0, None, (), None, ("600001", "600002"))
        with SqliteTransaction(external):
            external.run("UPDATE company_sync_request SET observed=0,status=NULL WHERE attempt=1")
            external.run("UPDATE company_sync_step SET changed_codes='[\"600003\"]' WHERE step=2")
        check(1, 1, None, (), None, ("600002", "600003"))


def test_writer_rolls_back_failed_reservation_and_observes_external_changes_across_threads(tmp_path, monkeypatch):
    path = tmp_path / "journal.sqlite3"
    policy = SqlitePolicyFixture()
    reports = []
    journal = SQLiteCompanySyncRoundJournal(path, sqlite_policy=policy, publication_identity=lambda: "",
        observe_requests=reports.append)
    journal.initialize()
    family = CompanySyncFamily.FINANCIAL
    scope = replace(_SCOPE, families=(family,))
    plan = CompanySyncPhasePlan(family, "b" * 64)
    request = HttpRequest("GET", "https://example.test/one", 1)
    def abort(transaction):
        transaction.rollback()
        raise RuntimeError("simulated_commit_failure")
    with journal.writer_session():
        round_id = journal.begin(scope, (plan,), max_source_calls=2)
        journal.start(round_id, family, CompanySyncPhaseState("a" * 64, True))
        with monkeypatch.context() as patch:
            patch.setattr(SqliteTransaction, "commit", abort)
            with pytest.raises(RuntimeError, match="simulated_commit_failure"):
                journal.authorize_request(request)
        assert journal.source_calls(round_id) == 0 and [(r.used, r.unknown) for r in reports] == [(0, 0)]
        journal.authorize_request(request)
        journal.observe_attempt(HttpAttempt("example.test", "GET", 0.1, 0, 200, ""))
        journal.finish(round_id, family, CompanySyncStepResult(True, False, accepted_inputs=1, pending_count=0))
        assert journal.family(round_id, family).unknown_attempts == 0 and reports[-1].unknown == 0
        with SqliteSession(path, sqlite_policy=policy) as external:
            external.run("UPDATE company_sync_request SET observed=0,status=NULL WHERE attempt=1")
        with ThreadPoolExecutor(max_workers=1) as workers:
            independent = workers.submit(journal.family, round_id, family).result()
        current = journal.family(round_id, family)
        assert current == independent and current.unknown_attempts == current.source_calls == 1
        assert journal.begin(scope, (plan,), max_source_calls=2) == round_id and reports[-1].unknown == 1
    assert journal.family(round_id, family) == current
