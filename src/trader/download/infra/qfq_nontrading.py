"""Nontrading observations in the existing market writer transaction."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import cast

from trader.download.domain.published_history import PublishedHistoryWindow
from trader.download.domain.qfq_planning import QfqNonTradingFact, QfqNonTradingKind
from trader.download.domain.price_review import PriceReviewRequestBudget
from trader.infra.sqlite_codec import DecodeDate, EncodeDate
from trader.infra.sqlite_connection import SqliteConnection


def ReadNonTradingFacts(
    connection: SqliteConnection, codes: Sequence[str], *, review_budget: PriceReviewRequestBudget | None = None
) -> tuple[QfqNonTradingFact, ...]:
    if not codes:
        return ()
    result: list[QfqNonTradingFact] = []
    for start in range(0, len(codes), 128):
        if review_budget is not None:
            review_budget.check_deadline()
        batch = tuple(codes[start:start + 128])
        placeholders = ",".join("?" for _ in batch)
        for code, day, kind, checked_on, source in connection.run(
            f"SELECT code,trade_date,kind,checked_on,source FROM price_nontrading WHERE code IN ({placeholders}) ORDER BY code,trade_date",
            batch,
        ):
            if review_budget is not None and len(result) % 128 == 0:
                review_budget.check_deadline()
            result.append(QfqNonTradingFact(
                code, DecodeDate(day), cast(QfqNonTradingKind, kind),
                DecodeDate(checked_on) if checked_on is not None else None, source,
            ))
    return tuple(result)


def ObservedNonTradingFacts(
    window: PublishedHistoryWindow, source: str, checked_on: date | None
) -> tuple[QfqNonTradingFact, ...]:
    return tuple(
        QfqNonTradingFact(
            window.code, cell.trade_date,
            "legacy_unverified" if checked_on is None else (
                "confirmed" if cell.status == "supplier_marked_suspended" else "inferred"
            ),
            checked_on, source if checked_on is not None else None,
        )
        for cell in window.cells
        if cell.status in ("supplier_marked_suspended", "tencent_inferred_suspended")
    )


def ReplaceNonTradingFacts(
    connection: SqliteConnection, code: str, facts: tuple[QfqNonTradingFact, ...]
) -> bool:
    previous = ReadNonTradingFacts(connection, (code,))
    if previous == facts:
        return False
    connection.run("DELETE FROM price_nontrading WHERE code=?", (code,))
    connection.run_many(
        "INSERT INTO price_nontrading(code,trade_date,kind,checked_on,source) VALUES(?,?,?,?,?)",
        ((code, EncodeDate(item.trade_date), item.kind,
          EncodeDate(item.checked_on) if item.checked_on is not None else None, item.source) for item in facts),
    )
    return True
