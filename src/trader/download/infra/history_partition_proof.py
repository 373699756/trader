"""Logical snapshot proofs for appendable SQLite monthly prices."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass

from trader.download.infra.history_revision_codec import RevisionFromColumns
from trader.download.infra.price_columns import PRICE_COLUMNS
from trader.infra.sqlite_codec import DecodeDate, DecodeHash
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
    dates: tuple[int, int],
    heartbeat: Callable[[int], None] = lambda count: None,
    check: Callable[[], None] | None = None,
) -> HistoryLogicalProof:
    """Hash visible keys and content identities; unrelated appends cannot invalidate this proof."""
    payload = "," + ",".join(f"r.{name}" for name in PRICE_COLUMNS) if validate_payloads else ""
    rows = connection.run(
        "SELECT r.trade_date,r.code,r.revision_id,r.first_seen_sequence,r.board"
        + payload
        + ",r.content_hash"
        + " FROM daily_price_revision r INDEXED BY sqlite_autoindex_daily_price_revision_1 "
        "WHERE r.trade_date>=? AND r.trade_date<? AND r.revision_id=(SELECT i.revision_id "
        "FROM daily_price_observation i INDEXED BY sqlite_autoindex_daily_price_observation_1 "
        "WHERE i.trade_date=r.trade_date AND i.code=r.code AND i.sync_sequence<=? "
        "ORDER BY i.sync_sequence DESC LIMIT 1) ORDER BY r.trade_date,r.code",
        (*dates, sequence),
    )
    digest = hashlib.sha256(b"history-logical-prices\n")
    encoder = json.JSONEncoder(ensure_ascii=True, separators=(",", ":"))
    count = 0
    for row in rows:
        if validate_payloads:
            RevisionFromColumns(tuple(row))
        digest.update(encoder.encode((DecodeDate(row[0]).isoformat(), *row[1:5], DecodeHash(row[-1]))).encode("ascii"))
        digest.update(b"\n")
        count += 1
        if count % 4096 == 0:
            if check is not None:
                check()
            heartbeat(count)
    heartbeat(count)
    return HistoryLogicalProof(digest.hexdigest(), count)
