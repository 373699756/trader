"""Cooperative SQLite deadline checks for additional market reviews only."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from trader.download.domain.price_review import PriceReviewBudgetExhausted, PriceReviewRequestBudget
from trader.infra.sqlite_connection import SqliteConnection


@contextmanager
def GuardQfqReviewSqlite(connection: SqliteConnection, budget: PriceReviewRequestBudget | None) -> Iterator[None]:
    if budget is None:
        yield
        return
    exhausted: PriceReviewBudgetExhausted | None = None

    def stop_expired_review() -> int:
        nonlocal exhausted
        try:
            budget.check_deadline()
        except PriceReviewBudgetExhausted as exc:
            exhausted = exc
            return 1
        return 0

    budget.check_deadline()
    connection.observe_progress(stop_expired_review, 1000)
    try:
        yield
        # The writer exits this guard before COMMIT. Expiry still rolls the
        # transaction back; SQLite commit/fsync and cleanup are not preemptible.
        budget.check_deadline()
    except sqlite3.Error as exc:
        if exhausted is not None:
            raise exhausted from exc
        raise
    finally:
        connection.observe_progress(None, 0)
