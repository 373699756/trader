"""Locate one explicit field statement in the complete, unchanged source text."""

from __future__ import annotations

import hashlib

from trader.download.domain.company_events import CompanySubject
from trader.download.domain.company_evidence import CompanyDocument
from trader.download.domain.company_field_results import CompanyFieldEvidence, CompanyFieldUnit


def BuildCompanyFieldEvidence(
    document: CompanyDocument, statement: str, rule_id: str, unit: CompanyFieldUnit, subject: CompanySubject,
) -> CompanyFieldEvidence | None:
    if not statement:
        return None
    compact = "".join(document.text.split())
    start = compact.find(statement)
    if start < 0 or compact.find(statement, start + 1) >= 0:
        return None
    offset = first = last = 0
    for index, character in enumerate(document.text):
        if character.isspace():
            continue
        if offset == start:
            first = index
        offset += 1
        if offset == start + len(statement):
            last = index + 1
            break
    quote = document.text[first:last]
    if len(quote) > 1000:
        return None
    line_offset = 0
    start_line = end_line = 1
    for number, line in enumerate(document.text.splitlines(keepends=True), 1):
        if line_offset <= first < line_offset + len(line):
            start_line = number
        if line_offset <= last - 1 < line_offset + len(line):
            end_line = number
            break
        line_offset += len(line)
    item = document.disclosure
    return CompanyFieldEvidence(
        item.source, item.identity, item.digest, hashlib.sha256(document.text.encode("utf-8")).digest(),
        rule_id, item.published_at, start_line, end_line, quote, unit, subject,
    )


__all__ = ["BuildCompanyFieldEvidence"]
