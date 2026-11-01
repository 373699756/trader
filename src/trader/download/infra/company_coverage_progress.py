"""Derive daily discovery/processing frontiers; never use them as a second checkpoint."""

from datetime import date, datetime, timedelta

from trader.download.domain.company_evidence import CompanyCoverageWatermark, EvidenceMarket
from trader.infra.sqlite_connection import SqliteConnection


def ReadCompanyCoverageProgress(
    raw: SqliteConnection, formal: SqliteConnection, cutoff: datetime, extraction_hash: bytes
) -> tuple[CompanyCoverageWatermark, ...]:
    """Caller has selected pending inputs and the exact requested population."""
    start, end = cutoff.date() - timedelta(days=365), cutoff.date()
    markets = sorted({EvidenceMarket(row[0]) for row in raw.run("SELECT code FROM temp.requested_codes")})
    result: list[CompanyCoverageWatermark] = []
    for market in markets:
        frontier = start
        for row in raw.run("SELECT start_date,end_date FROM coverage WHERE market=? ORDER BY start_date,end_date", (market,)):
            lower, upper = date.fromisoformat(row[0]), date.fromisoformat(row[1])
            if lower > frontier:
                break
            frontier = max(frontier, upper + timedelta(days=1))
        discovered = min(end, frontier - timedelta(days=1)) if frontier > start else None
        processed = discovered
        if discovered is not None:
            for row in raw.run(
                "SELECT d.source,d.source_key,d.record_hash,d.published_at,c.code,p.unextractable,r.retry_policy_id,r.failure "
                "FROM disclosures d JOIN disclosure_codes c ON c.disclosure_id=d.id "
                "JOIN temp.requested_codes u ON u.code=c.code LEFT JOIN temp.pending_documents p ON p.id=d.id "
                "LEFT JOIN document_retries r ON r.disclosure_id=d.id AND r.record_hash=d.record_hash "
                "WHERE (p.id IS NOT NULL OR d.body_retained=1) AND d.published_at>=? AND d.published_at<? "
                "ORDER BY d.published_at,d.id",
                (start.isoformat(), (discovered + timedelta(days=1)).isoformat()),
            ):
                if EvidenceMarket(row[4]) != market:
                    continue
                receipt = formal.run(
                    "SELECT outcome,policy_id FROM company_input_receipt WHERE source=? AND source_key=? AND code=? "
                    "AND record_hash=? AND extraction_hash=?",
                    (row[0], row[1], row[4], row[2], extraction_hash),
                ).fetchone()
                if receipt is not None and (receipt[0] == "valid" or row[5] and (receipt[0], receipt[1]) == (row[7], row[6])):
                    continue
                last_day = datetime.fromisoformat(row[3]).date() - timedelta(days=1)
                processed = last_day if last_day >= start else None
                break
        result.append(CompanyCoverageWatermark(market, start, discovered, processed))
    return tuple(result)


__all__ = ["ReadCompanyCoverageProgress"]
