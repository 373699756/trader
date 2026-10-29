from datetime import date

import pytest

from trader.download.infra.price_repair_attempts import PRICE_REPAIR_ATTEMPT_SQL, PriceRepairAttempts
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction

DAY = date(2026, 10, 28)
NEXT = date(2026, 10, 29)


def test_failed_and_interrupted_attempts_are_not_automatically_repeated(tmp_path):
    path = tmp_path / "prices.sqlite3"
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        connection.run(PRICE_REPAIR_ATTEMPT_SQL)
        attempts = PriceRepairAttempts(connection)
        assert attempts.reserve("600188", (DAY,), "first") == (DAY,)
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        attempts = PriceRepairAttempts(connection)
        assert attempts.read("600188")[0].state == "requested"
        assert attempts.reserve("600188", (DAY, NEXT), "next") == (NEXT,)
        attempts.fail("600188", "first", "supplier_timeout")
        assert attempts.reserve("600188", (DAY,), "third") == ()
        assert attempts.reserve("600188", (DAY,), "manual", explicit=True) == (DAY,)
        assert attempts.reserve("600188", (DAY,), "manual", explicit=True) == ()


def test_publication_rollback_does_not_resolve_attempt(tmp_path):
    path = tmp_path / "prices.sqlite3"
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        connection.run(PRICE_REPAIR_ATTEMPT_SQL)
        PriceRepairAttempts(connection).reserve("600188", (DAY,), "first")
    with pytest.raises(RuntimeError, match="publication failed"):
        with SqliteSession(path) as connection, SqliteTransaction(connection):
            PriceRepairAttempts(connection).resolve("600188", (DAY,), "baostock")
            raise RuntimeError("publication failed")
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        attempts = PriceRepairAttempts(connection)
        assert attempts.read("600188")[0].state == "requested"
        attempts.resolve("600188", (DAY,), "baostock")
        assert attempts.read("600188")[0].state == "resolved"
        assert attempts.read("600188")[0].resolved_source == "baostock"
        attempts.fail("600188", "first", "late_failure")
        assert attempts.reserve("600188", (DAY,), "manual", explicit=True) == ()
        assert attempts.read("600188")[0].state == "resolved"


def test_receipts_require_the_publishers_transaction(tmp_path):
    with SqliteSession(tmp_path / "prices.sqlite3") as connection:
        with pytest.raises(RuntimeError, match="requires_transaction"):
            PriceRepairAttempts(connection).reserve("600188", (DAY,), "first")
