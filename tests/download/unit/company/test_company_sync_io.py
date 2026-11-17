"""The company deadline covers SQL opening, lock waits and progress callbacks."""

import sqlite3

import pytest

from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.download.domain.company_sync import CompanySyncInterrupted
from trader.download.infra.company_sync_io import CompanySyncIoBudget
from trader.infra.sqlite_connection import SqliteConnectionOptions


def test_expired_budget_does_not_create_database_and_remaining_timeout_is_bounded(tmp_path):
    policy = SqlitePolicyFixture()
    active = CompanySyncIoBudget(deadline=1, monotonic=lambda: 0.75, cancel_requested=lambda: False)
    assert active.remaining_policy(policy).connection_timeout_seconds == 0.25
    expired = CompanySyncIoBudget(deadline=1, monotonic=lambda: 1, cancel_requested=lambda: False)
    target = tmp_path / "never-opened.sqlite3"
    with pytest.raises(CompanySyncInterrupted), expired.session(target, SqliteConnectionOptions(), sqlite_policy=policy):
        raise AssertionError("expired_session_must_not_open")
    assert not target.exists()


def test_sql_progress_cancel_becomes_known_control_but_unknown_sql_failure_keeps_cause(tmp_path):
    stopped = [False]
    budget = CompanySyncIoBudget(deadline=10, monotonic=lambda: 0, cancel_requested=lambda: stopped[0])
    target = tmp_path / "progress.sqlite3"
    with pytest.raises(CompanySyncInterrupted) as interrupted:
        with budget.session(target, SqliteConnectionOptions(), sqlite_policy=SqlitePolicyFixture()) as connection:
            stopped[0] = True
            connection.run("WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<1000000) SELECT SUM(x) FROM n").require_one()
    assert isinstance(interrupted.value.__cause__, sqlite3.OperationalError)
    stopped[0] = False
    with pytest.raises(sqlite3.OperationalError, match="no such table"), budget.session(
        target, SqliteConnectionOptions(), sqlite_policy=SqlitePolicyFixture(),
    ) as connection:
        connection.run("SELECT * FROM missing_company_table").all()
