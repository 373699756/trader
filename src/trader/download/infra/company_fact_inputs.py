"""Current raw-row codecs and explicit coverage comparisons."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Literal, cast

from trader.download.domain.company_evidence import Disclosure


def DecodeCompanyDisclosure(row: sqlite3.Row) -> Disclosure:
    return Disclosure(
        row["source"],
        row["source_key"],
        (row["code"],),
        row["title"],
        datetime.fromisoformat(row["published_at"]),
        row["url"],
        row["record_hash"],
        cast(Literal["announcement", "news"], row["kind"]),
    )

def CompanyCoverageComplete(ranges: Sequence[sqlite3.Row], start: date, end: date) -> bool:
    frontier = start
    for row in ranges:
        if date.fromisoformat(row[0]) <= frontier:
            frontier = max(frontier, date.fromisoformat(row[1]) + timedelta(days=1))
    return frontier > end

def BindCompanyEvidenceSource(sources: dict[str, str], key: str, source: str) -> None:
    previous = sources.get(key)
    if previous is not None and previous != source:
        raise ValueError("company_evidence_source_ambiguous")
    sources[key] = source

__all__ = ["DecodeCompanyDisclosure", "CompanyCoverageComplete", "BindCompanyEvidenceSource"]
