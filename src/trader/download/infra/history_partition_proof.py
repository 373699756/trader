"""Logical snapshot proofs for appendable SQLite monthly prices."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from trader.download.infra.history_revision_codec import decode_history_revision


@dataclass(frozen=True)
class HistoryLogicalProof:
    logical_hash: str
    row_count: int


def calculate_history_proof(
    connection: sqlite3.Connection,
    sequence: int,
    *,
    validate_payloads: bool,
    heartbeat: Callable[[int], None] = lambda count: None,
) -> HistoryLogicalProof:
    """Hash visible keys and content identities; unrelated appends cannot invalidate this proof."""
    payload = ", r.payload_json" if validate_payloads else ""
    rows = connection.execute(
        "SELECT o.trade_date,o.code,o.revision_id,r.first_seen_sequence,r.board,r.content_hash"
        + payload
        + " FROM daily_observations o JOIN daily_records r ON "
        "r.trade_date=o.trade_date AND r.code=o.code AND r.revision_id=o.revision_id "
        "WHERE o.sync_sequence=(SELECT MAX(i.sync_sequence) FROM daily_observations i "
        "WHERE i.trade_date=o.trade_date AND i.code=o.code AND i.sync_sequence<=?) "
        "ORDER BY o.trade_date,o.code",
        (sequence,),
    )
    digest = hashlib.sha256(b"history-logical-prices\n")
    count = 0
    for row in rows:
        if validate_payloads:
            value = decode_history_revision(row[6])
            if (
                value.trade_date.isoformat(),
                value.code,
                value.revision_id,
                value.first_seen_sequence,
                value.board,
                value.content_hash,
            ) != row[:6]:
                raise ValueError("history_price_content_corrupt")
        digest.update(json.dumps(row[:6], ensure_ascii=True, separators=(",", ":")).encode("ascii"))
        digest.update(b"\n")
        count += 1
        if count % 4096 == 0:
            heartbeat(count)
    heartbeat(count)
    return HistoryLogicalProof(digest.hexdigest(), count)
