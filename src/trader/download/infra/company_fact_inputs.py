"""Read consistent raw inputs for compact company fact publication."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal, cast

from trader.download.domain.company_event_extraction import CompanyDocumentRiskRelevant
from trader.download.domain.company_evidence import (
    CompanyDocument,
    CompanyEvidenceInput,
    CompanyFinancial,
    CompanyRatio,
    Disclosure,
    EvidenceMarket,
)
from trader.download.infra.company_facts_protocol import REVIEW_REASONS, DecodeCompanyLabel, EncodeCompanyReason
from trader.infra.sqlite_connection import SqliteConnection


def DecodeCompanyDisclosure(row: sqlite3.Row) -> Disclosure:
    return Disclosure(
        row["source"],
        row["source_id"],
        (row["code"],),
        row["title"],
        datetime.fromisoformat(row["published_at"]),
        row["url"],
        row["digest"],
        cast(Literal["announcement", "news"], row["kind"]),
    )


@dataclass(frozen=True)
class CompanySnapshotRows:
    disclosures: tuple[sqlite3.Row, ...]
    financials: tuple[sqlite3.Row, ...]
    ratios: tuple[sqlite3.Row, ...]
    checks: tuple[sqlite3.Row, ...]


@dataclass(frozen=True)
class CompanySnapshotShared:
    coverage: tuple[sqlite3.Row, ...]
    checks: tuple[sqlite3.Row, ...]
    errors: tuple[sqlite3.Row, ...]
    pledge_day: str | None
    unlock_snapshot: sqlite3.Row | None


def ReadCompanySnapshotShared(connection: SqliteConnection, now: datetime) -> CompanySnapshotShared:
    cutoff = (now.date() - timedelta(days=365)).isoformat()
    pledge = connection.run("SELECT start FROM ratio_snapshots WHERE component='pledge'").fetchone()
    return CompanySnapshotShared(
        tuple(connection.run("SELECT start,end,market,checked_at FROM coverage ORDER BY start")),
        tuple(connection.run("SELECT component,checked_at,failure FROM component_checks WHERE scope='market'")),
        tuple(
            connection.run(
                "SELECT DISTINCT CASE WHEN failure='' THEN 'pending' ELSE failure END AS failure,market "
                "FROM partitions WHERE end>=? AND (failure<>'' OR complete=0)",
                (cutoff,),
            )
        ),
        pledge[0] if pledge else None,
        connection.run("SELECT checked_at,end FROM ratio_snapshots WHERE component='unlock'").fetchone(),
    )


def ReadCompanySnapshotRows(
    connection: SqliteConnection, codes: tuple[str, ...], now: datetime, extraction_hash: bytes
) -> dict[str, CompanySnapshotRows]:
    placeholders = ",".join("?" for _ in codes)
    disclosures = connection.run(
        "SELECT d.*,c.code,EXISTS(SELECT 1 FROM document_publications p WHERE p.disclosure_id=d.id "
        "AND p.code=c.code AND p.digest=d.digest AND p.extraction_hash=?) AS published_for_code "
        "FROM disclosures d JOIN disclosure_codes c ON c.disclosure_id=d.id "
        f"WHERE c.code IN ({placeholders}) AND (d.published_at>=? OR d.kind='announcement') "
        "AND d.published_at<=? ORDER BY d.published_at,d.id",
        (extraction_hash, *codes, (now.date() - timedelta(days=365)).isoformat(), now.isoformat()),
    )
    documents: dict[str, list[sqlite3.Row]] = {code: [] for code in codes}
    for row in disclosures:
        documents[row["code"]].append(row)
    financials: dict[str, list[sqlite3.Row]] = {code: [] for code in codes}
    for row in connection.run(
        f"SELECT * FROM financials WHERE code IN ({placeholders}) AND report_date>=? AND published_at<=? ORDER BY report_date",
        (*codes, (now.date() - timedelta(days=550)).isoformat(), now.isoformat()),
    ):
        financials[row["code"]].append(row)
    ratios: dict[str, list[sqlite3.Row]] = {code: [] for code in codes}
    for row in connection.run(
        f"SELECT code,day,ratio_pct,component FROM ratios WHERE code IN ({placeholders}) ORDER BY day", codes
    ):
        ratios[row["code"]].append(row)
    checks: dict[str, list[sqlite3.Row]] = {code: [] for code in codes}
    for row in connection.run(
        f"SELECT component,checked_at,failure,scope FROM component_checks WHERE scope IN ({placeholders})", codes
    ):
        checks[row["scope"]].append(row)
    return {
        code: CompanySnapshotRows(
            tuple(documents[code]), tuple(financials[code]), tuple(ratios[code]), tuple(checks[code])
        )
        for code in codes
    }


def BuildCompanySnapshot(
    code: str,
    now: datetime,
    inputs: CompanySnapshotRows,
    shared: CompanySnapshotShared,
    *,
    listed_on: date | None,
    read_document: Callable[[bytes], str],
) -> CompanyEvidenceInput:
    start, end = now.date() - timedelta(days=365), now.date()
    rows = inputs.disclosures
    documents = tuple(
        CompanyDocument(
            DecodeCompanyDisclosure(row),
            read_document(row["body_hash"]) if row["body_retained"] else "",
            bool(row["body_checked"]) and bool(row["body_retained"]),
        )
        for row in rows
        if row["kind"] == "announcement" and not row["published_for_code"]
    )
    ranges = tuple(row for row in shared.coverage if row["market"] == EvidenceMarket(code))
    complete = CompanyCoverageComplete(ranges, start, end) and all(
        item.checked or not CompanyDocumentRiskRelevant(item.disclosure.title) for item in documents
    )
    financials = tuple(
        CompanyFinancial(row[0], date.fromisoformat(row[1]), datetime.fromisoformat(row[2]), *tuple(row)[3:])
        for row in inputs.financials
    )
    pledge_rows = tuple(row for row in inputs.ratios if row["component"] == "pledge" and row["day"] <= end.isoformat())
    pledge_row = pledge_rows[-1] if pledge_rows else None
    pledge = CompanyRatio(pledge_row[0], date.fromisoformat(pledge_row[1]), pledge_row[2]) if pledge_row else None
    if pledge is None and shared.pledge_day:
        pledge = CompanyRatio(code, date.fromisoformat(shared.pledge_day), 0.0)
    unlocks = tuple(
        CompanyRatio(row[0], date.fromisoformat(row[1]), row[2])
        for row in inputs.ratios
        if row["component"] == "unlock" and end.isoformat() <= row["day"] <= (end + timedelta(days=180)).isoformat()
    )
    checks = (*shared.checks, *inputs.checks)
    failures = tuple(f"{row[0]}:{row[2]}" for row in checks if row[2])
    for component in ("financial", "pledge", "unlock"):
        if not any(
            row[0] == component and row[1] and datetime.fromisoformat(row[1]) >= now - timedelta(days=1) and not row[2]
            for row in checks
        ):
            failures += (f"{component}:not_current",)
    if pledge is not None and pledge.day < end - timedelta(days=10):
        failures += ("pledge:source_stale",)
    partition_errors = tuple(row for row in shared.errors if row["market"] == EvidenceMarket(code))
    failures += tuple(f"announcements:{row[0]}" for row in partition_errors)
    if listed_on is None or listed_on > end or not CompanyCoverageComplete(ranges, listed_on, end):
        failures += ("announcements:initial_risk_state_unverified",)
    complete = complete and not partition_errors
    if not complete:
        failures += ("announcements:window_incomplete",)
    failures = tuple(
        sorted(
            {
                f"{scope}:{DecodeCompanyLabel(REVIEW_REASONS, EncodeCompanyReason(reason))}"
                for failure in failures
                for scope, _, reason in (failure.partition(":"),)
            }
        )
    )
    news = tuple(
        DecodeCompanyDisclosure(row)
        for row in rows
        if row["kind"] == "news" and datetime.fromisoformat(row["published_at"]) >= now - timedelta(days=7)
    )
    snapshot = CompanyEvidenceInput(
        code,
        now,
        start,
        end,
        "",
        documents,
        complete,
        financials,
        pledge,
        unlocks,
        not any(item.startswith("unlock:") for item in failures),
        news,
        any(
            row[0] == "news" and row[1] and datetime.fromisoformat(row[1]) >= now - timedelta(days=1) and not row[2]
            for row in checks
        ),
        failures,
        source_checked_at=max(
            (
                datetime.fromisoformat(value)
                for value in (*(row["checked_at"] for row in ranges), *(row[1] for row in checks))
                if value
            ),
            default=None,
        ),
        unlock_checked_at=datetime.fromisoformat(shared.unlock_snapshot[0]) if shared.unlock_snapshot else None,
        unlock_coverage_end=date.fromisoformat(shared.unlock_snapshot[1]) if shared.unlock_snapshot else None,
    )
    return snapshot


def CompanyCoverageComplete(ranges: Sequence[sqlite3.Row], start: date, end: date) -> bool:
    frontier = start
    for row in ranges:
        if date.fromisoformat(row[0]) <= frontier:
            frontier = max(frontier, date.fromisoformat(row[1]) + timedelta(days=1))
    return frontier > end


__all__ = [
    "BuildCompanySnapshot",
    "CompanyCoverageComplete",
    "CompanySnapshotRows",
    "CompanySnapshotShared",
    "DecodeCompanyDisclosure",
    "ReadCompanySnapshotRows",
    "ReadCompanySnapshotShared",
]
