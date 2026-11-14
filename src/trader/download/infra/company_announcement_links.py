"""Version-bound announcement-number aliases for the existing event link owner."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import datetime

from trader.download.domain.company_announcement_numbers import CompanyAnnouncementNumbers
from trader.download.domain.company_event_extraction import CompanyLinkCandidate
from trader.download.infra.company_pipeline_queries import MarkCompanyDirty
from trader.infra.sqlite_connection import SqliteConnection

_RULE = "issuer_announcement_number:1"


def WriteCompanyAnnouncementNumbers(
    connection: SqliteConnection,
    disclosure_id: int,
    record_hash: str,
    body_hash: bytes,
    numbers: CompanyAnnouncementNumbers,
) -> None:
    codes = tuple(row[0] for row in connection.run(
        "SELECT c.code FROM disclosure_codes c JOIN disclosures d ON d.id=c.disclosure_id "
        "WHERE d.id=? AND d.record_hash=? ORDER BY c.code", (disclosure_id, record_hash),
    ))
    entries = tuple((number, 1) for number in numbers.references)
    if numbers.own is not None:
        entries = ((numbers.own, 0), *entries)
    if not codes or not set(codes).issubset(numbers.issuer_codes):
        entries = ()
    values = tuple((record_hash, body_hash, number, role, _RULE) for number, role in entries)
    current = tuple(tuple(row) for row in connection.run(
        "SELECT record_hash,body_hash,number,role,rule_id FROM document_announcement_numbers "
        "WHERE disclosure_id=? ORDER BY role,number", (disclosure_id,),
    ))
    if current == tuple(sorted(values, key=lambda row: (row[3], row[2]))):
        return
    connection.run("DELETE FROM document_announcement_numbers WHERE disclosure_id=?", (disclosure_id,))
    connection.run_many(
        "INSERT INTO document_announcement_numbers VALUES(?,?,?,?,?,?)",
        ((disclosure_id, *value) for value in values),
    )
    MarkCompanyDirty(connection, codes)


def LinkCompanyAnnouncementNumbers(
    connection: SqliteConnection, code: str, candidates: tuple[CompanyLinkCandidate, ...]
) -> tuple[CompanyLinkCandidate, ...]:
    """Only an explicit, unique older reference within one issuer/source can link.

    Missing metadata never authorizes another download. Existing candidates
    keep their original source-ID references, and event/subject validation
    remains with ResolveCompanyLinks.
    """
    aliases: dict[tuple[str, str], list[tuple[str, datetime]]] = defaultdict(list)
    mentions: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for row in connection.run(
        "SELECT d.source,d.source_key,d.published_at,n.number,n.role "
        "FROM document_announcement_numbers n JOIN disclosures d ON d.id=n.disclosure_id "
        "AND d.record_hash=n.record_hash AND d.body_hash=n.body_hash "
        "JOIN disclosure_codes c ON c.disclosure_id=d.id "
        "WHERE c.code=? AND n.rule_id=? AND d.body_checked=1", (code, _RULE),
    ):
        if row[4] == 0:
            aliases[(row[0], row[3])].append((row[1], datetime.fromisoformat(row[2])))
        else:
            mentions[row[1]].append((row[0], row[3]))
    linked: list[CompanyLinkCandidate] = []
    for candidate in candidates:
        targets: set[str] = set()
        for source, number in mentions[candidate.evidence_id]:
            matches = tuple(
                key for key, at in aliases[(source, number)]
                if key != candidate.evidence_id and at < candidate.published_at
            )
            if len(matches) != 1:
                targets.clear()
                break
            targets.update(matches)
        linked.append(replace(candidate, references=tuple(sorted(set(candidate.references) | targets)))
                      if len(targets) == 1 else candidate)
    return tuple(linked)


__all__ = ["LinkCompanyAnnouncementNumbers", "WriteCompanyAnnouncementNumbers"]
