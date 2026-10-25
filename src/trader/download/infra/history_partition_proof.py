"""Logical snapshot proofs for appendable SQLite monthly prices."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass

from trader.download.infra.history_revision_codec import RevisionFromColumns
from trader.download.infra.price_columns import PRICE_COLUMNS
from trader.infra.sqlite_connection import SqliteConnection


@dataclass(frozen=True)
class HistoryLogicalProof:
    logical_hash: str
    row_count: int


def CalculateHistoryProof(
    connection: SqliteConnection,
    sequence: int,
    *,
    validate_payloads: bool,
    dates: tuple[str, str],
    heartbeat: Callable[[int], None] = lambda count: None,
    check: Callable[[], None] | None = None,
) -> HistoryLogicalProof:
    """Hash visible keys and content identities; unrelated appends cannot invalidate this proof."""
    payload = "," + ",".join(f"r.{name}" for name in PRICE_COLUMNS) if validate_payloads else ""
    rows = connection.run(
        "SELECT o.trade_date,o.code,o.revision_id,r.first_seen_sequence,r.board"
        + payload
        + ",r.content_hash"
        + " FROM daily_observations o JOIN daily_records r ON "
        "r.trade_date=o.trade_date AND r.code=o.code AND r.revision_id=o.revision_id "
        "WHERE o.trade_date>=? AND o.trade_date<? AND o.sync_sequence=(SELECT MAX(i.sync_sequence) "
        "FROM daily_observations i INDEXED BY sqlite_autoindex_daily_observations_1 "
        "WHERE i.trade_date=o.trade_date AND i.code=o.code AND i.sync_sequence<=?) "
        "ORDER BY o.trade_date,o.code",
        (*dates, sequence),
    )
    digest = hashlib.sha256(b"history-logical-prices\n")
    count = 0
    for row in rows:
        if validate_payloads:
            RevisionFromColumns(tuple(row))
        digest.update(json.dumps((*row[:5], row[-1]), ensure_ascii=True, separators=(",", ":")).encode("ascii"))
        digest.update(b"\n")
        count += 1
        if count % 4096 == 0:
            if check is not None:
                check()
            heartbeat(count)
    heartbeat(count)
    return HistoryLogicalProof(digest.hexdigest(), count)
