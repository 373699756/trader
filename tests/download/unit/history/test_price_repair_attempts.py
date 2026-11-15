from datetime import date

import pytest

from trader.download.infra.price_repair_attempts import PRICE_REPAIR_ATTEMPT_SQL, PriceRepairAttempts
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction

DAY = date(2026, 10, 28)
NEXT = date(2026, 10, 29)


@pytest.mark.parametrize("reason", ("supplier_query_failed", "supplier_timeout", "price_replacement_window_incomplete", "unknown_failure"))
def test_every_failed_reason_can_be_retried_by_a_new_batch(tmp_path, reason):
    path = tmp_path / "prices.sqlite3"
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        connection.run(PRICE_REPAIR_ATTEMPT_SQL)
        attempts = PriceRepairAttempts(connection)
        assert attempts.reserve("600188", (DAY,), "first") == (DAY,)
        attempts.fail("600188", "first", reason)
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        attempts = PriceRepairAttempts(connection)
        assert attempts.read("600188")[0].state == "failed"
        assert attempts.reserve("600188", (DAY, NEXT), "next") == (DAY, NEXT)
        assert attempts.reserve("600188", (DAY, NEXT), "next") == ()
        attempts.fail("600188", "first", reason)
        assert all(
            row.state == "requested" and row.batch_id == "next" and row.reason == "supplier_result_unknown"
            and row.resolved_source is None for row in attempts.read("600188")
        )


def test_new_batch_can_reclaim_interrupted_requests_without_repeating_the_same_batch(tmp_path):
    path = tmp_path / "prices.sqlite3"
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        connection.run(PRICE_REPAIR_ATTEMPT_SQL)
        attempts = PriceRepairAttempts(connection)
        assert attempts.reserve("600188", (DAY,), "first") == (DAY,)
        assert attempts.reserve("600188", (DAY,), "first") == ()
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        attempts = PriceRepairAttempts(connection)
        assert attempts.reserve("600188", (DAY, NEXT), "next") == (DAY, NEXT)
        assert attempts.reserve("600188", (DAY, NEXT), "next") == ()
        attempts.fail("600188", "first", "supplier_price_total_timeout")
        assert all(row.batch_id == "next" and row.state == "requested" for row in attempts.read("600188"))


@pytest.mark.parametrize("reason", ("supplier_price_total_timeout", "supplier_cancelled"))
def test_new_batch_retries_failed_receipts_and_ignores_late_old_failures(tmp_path, reason):
    path = tmp_path / "prices.sqlite3"
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        connection.run(PRICE_REPAIR_ATTEMPT_SQL)
        attempts = PriceRepairAttempts(connection)
        assert attempts.reserve("600188", (DAY,), "first") == (DAY,)
        attempts.fail("600188", "first", reason)
        assert attempts.reserve("600188", (DAY,), "first") == ()
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        attempts = PriceRepairAttempts(connection)
        assert attempts.reserve("600188", (DAY,), "next") == (DAY,)
        row, = attempts.read("600188")
        assert (row.batch_id, row.state, row.reason, row.resolved_source) == (
            "next", "requested", "supplier_result_unknown", None,
        )
        attempts.fail("600188", "first", reason)
        assert attempts.read("600188") == (row,)
        attempts.fail("600188", "next", reason)
        assert attempts.reserve("600188", (DAY,), "next") == ()


def test_reopened_reservation_rolls_back_with_the_callers_transaction(tmp_path):
    path = tmp_path / "prices.sqlite3"
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        connection.run(PRICE_REPAIR_ATTEMPT_SQL)
        attempts = PriceRepairAttempts(connection)
        attempts.reserve("600188", (DAY,), "first")
        attempts.fail("600188", "first", "supplier_price_total_timeout")
        before = attempts.read("600188")
    with pytest.raises(RuntimeError, match="fixture_rollback"):
        with SqliteSession(path) as connection, SqliteTransaction(connection):
            assert PriceRepairAttempts(connection).reserve("600188", (DAY,), "next") == (DAY,)
            raise RuntimeError("fixture_rollback")
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        attempts = PriceRepairAttempts(connection)
        assert attempts.read("600188") == before
        assert attempts.reserve("600188", (DAY,), "next") == (DAY,)


@pytest.mark.parametrize("source", ("tencent", "baostock"))
def test_publication_rollback_does_not_resolve_attempt(tmp_path, source):
    path = tmp_path / "prices.sqlite3"
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        connection.run(PRICE_REPAIR_ATTEMPT_SQL)
        PriceRepairAttempts(connection).reserve("600188", (DAY,), "first")
    with pytest.raises(RuntimeError, match="publication failed"):
        with SqliteSession(path) as connection, SqliteTransaction(connection):
            PriceRepairAttempts(connection).resolve("600188", (DAY,), source)
            raise RuntimeError("publication failed")
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        attempts = PriceRepairAttempts(connection)
        assert attempts.read("600188")[0].state == "requested"
        attempts.resolve("600188", (DAY,), source)
        assert attempts.read("600188")[0].state == "resolved"
        assert attempts.read("600188")[0].resolved_source == source
        attempts.fail("600188", "first", "late_failure")
        assert attempts.reserve("600188", (DAY,), "manual") == ()
        assert attempts.read("600188")[0].state == "resolved"


def test_receipts_require_the_publishers_transaction(tmp_path):
    with SqliteSession(tmp_path / "prices.sqlite3") as connection:
        with pytest.raises(RuntimeError, match="requires_transaction"):
            PriceRepairAttempts(connection).reserve("600188", (DAY,), "first")
