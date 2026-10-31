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


_LATEST_PROOF_ROWS_SQL = """
WITH latest AS (
    SELECT trade_date, code, MAX(sync_sequence) AS sync_sequence
    FROM daily_price_observation
    WHERE trade_date >= ? AND trade_date < ? AND sync_sequence <= ?
    GROUP BY trade_date, code
)
SELECT r.trade_date, r.code, r.revision_id, r.first_seen_sequence, r.board{payload}, r.content_hash
FROM latest AS latest
JOIN daily_price_observation AS observations
  ON observations.trade_date = latest.trade_date
 AND observations.code = latest.code
 AND observations.sync_sequence = latest.sync_sequence
JOIN daily_price_revision AS r
  ON r.trade_date = observations.trade_date
 AND r.code = observations.code
 AND r.revision_id = observations.revision_id
ORDER BY r.trade_date, r.code
"""


def CalculateHistoryProof(
    connection: SqliteConnection,
    sequence: int,
    *,
    validate_payloads: bool,
    dates: tuple[int, int],
    heartbeat: Callable[[int], None] = lambda count: None,
    check: Callable[[], None] | None = None,
) -> HistoryLogicalProof:
    """Hash one visible month; unrelated appends cannot invalidate this proof."""
    return CalculateHistoryProofs(
        connection,
        sequence,
        validate_payloads=validate_payloads,
        date_ranges=(dates,),
        heartbeat=heartbeat,
        check=check,
    )[0]


def CalculateHistoryProofs(
    connection: SqliteConnection,
    sequence: int,
    *,
    validate_payloads: bool,
    date_ranges: tuple[tuple[int, int], ...],
    heartbeat: Callable[[int], None] = lambda count: None,
    check: Callable[[], None] | None = None,
) -> tuple[HistoryLogicalProof, ...]:
    """Hash ordered visible identities for multiple month ranges in one annual scan."""
    if not date_ranges or any(start >= end for start, end in date_ranges) or any(
        previous_end > start for (_, previous_end), (start, _) in zip(date_ranges, date_ranges[1:])
    ):
        raise ValueError("history logical proof ranges are invalid")
    payload = "," + ",".join(f"r.{name}" for name in PRICE_COLUMNS) if validate_payloads else ""
    rows = connection.run(
        _LATEST_PROOF_ROWS_SQL.format(payload=payload),
        (date_ranges[0][0], date_ranges[-1][1], sequence),
    )
    digests = tuple(hashlib.sha256(b"history-logical-prices\n") for _ in date_ranges)
    counts = [0 for _ in date_ranges]
    encoder = json.JSONEncoder(ensure_ascii=True, separators=(",", ":"))
    range_index = 0
    count = 0
    for row in rows:
        trade_date = row[0]
        while range_index < len(date_ranges) and trade_date >= date_ranges[range_index][1]:
            range_index += 1
        if range_index == len(date_ranges):
            break
        if trade_date < date_ranges[range_index][0]:
            continue
        if validate_payloads:
            RevisionFromColumns(tuple(row))
        digests[range_index].update(
            encoder.encode((DecodeDate(trade_date).isoformat(), *row[1:5], DecodeHash(row[-1]))).encode("ascii")
        )
        digests[range_index].update(b"\n")
        counts[range_index] += 1
        count += 1
        if count % 4096 == 0:
            if check is not None:
                check()
            heartbeat(count)
    heartbeat(count)
    return tuple(HistoryLogicalProof(digest.hexdigest(), row_count) for digest, row_count in zip(digests, counts))
