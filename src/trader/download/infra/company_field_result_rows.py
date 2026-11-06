"""Relational codec for field conclusions; current values retain their fact-column owner."""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from trader.download.domain.company_events import CompanySubject
from trader.download.domain.company_field_plan import BuildCompanyFieldResults
from trader.download.domain.company_field_results import (
    CompanyFieldConclusion,
    CompanyFieldConclusionState,
    CompanyFieldConclusionValue,
    CompanyFieldEvidence,
    CompanyFieldName,
    CompanyFieldUnit,
)
from trader.download.infra.company_fact_rows import ReadCompanyFactRows
from trader.download.infra.company_facts_protocol import CompanyInteger, DecodeCompanyEnum, EncodeCompanyTime, RequireCompanyTime, SUBJECTS
from trader.infra.sqlite_connection import SqliteConnection


def _code(value: object) -> str:
    if not isinstance(value, str) or len(value) != 6 or not value.isascii() or not value.isdigit():
        raise ValueError("company_field_result_code_invalid")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("company_field_result_text_invalid")
    return value


def _hash(value: object) -> bytes:
    if not isinstance(value, bytes) or len(value) != 32:
        raise ValueError("company_field_result_hash_invalid")
    return value


def _bindings(
    connection: SqliteConnection, codes: Sequence[str],
) -> tuple[dict[tuple[str, str, str], CompanyFieldConclusionValue], dict[tuple[str, str, str], CompanySubject]]:
    # This pure projection supplies the sole current fact values. Its diagnostic
    # states are deliberately ignored; published conclusions have their own rows.
    values: dict[tuple[str, str, str], CompanyFieldConclusionValue] = {}
    subjects: dict[tuple[str, str, str], CompanySubject] = {}
    for fact in ReadCompanyFactRows(connection, codes):
        risks = {event.event_id: event.subject_type for event in fact.risk_events}
        positives = {event.event_id: event.subject_type for event in fact.positive_events}
        for result in BuildCompanyFieldResults(fact):
            key = (fact.code, result.event_id, result.field)
            values[key] = result.value
            subjects[key] = (risks if result.field in {
                "start_date", "end_date", "planned_pct", "completion_disclosed_at",
            } else positives)[result.event_id]
    return values, subjects


def WriteCompanyFieldResults(
    connection: SqliteConnection, code: str, results: Sequence[CompanyFieldConclusion]
) -> None:
    """Replace one issuer's conclusions inside the caller's fact publication transaction."""
    code = _code(code)
    prepared = tuple(results)
    if any(not isinstance(item, CompanyFieldConclusion) or item.code != code for item in prepared):
        raise ValueError("company_field_result_member_invalid")
    keys = tuple((item.code, item.event_id, item.field) for item in prepared)
    if len(set(keys)) != len(keys):
        raise ValueError("company_field_result_duplicate")
    if connection.run("SELECT 1 FROM company_facts WHERE code=?", (code,)).fetchone() is None:
        raise ValueError("company_field_result_fact_missing")
    current, subjects = _bindings(connection, (code,))
    for item in prepared:
        key = (code, item.event_id, item.field)
        if key not in current or type(item.value) is not type(current[key]) or item.value != current[key]:
            raise ValueError("company_field_result_value_mismatch")
        if any(proof.subject is not subjects[key] for proof in item.evidence):
            raise ValueError("company_field_result_subject_mismatch")
    if set(keys) != set(current):
        raise ValueError("company_field_result_coverage_incomplete")
    connection.run("DELETE FROM company_field_result WHERE code=?", (code,))
    for item in sorted(prepared, key=lambda result: (result.event_id, result.field)):
        connection.run(
            "INSERT INTO company_field_result(code,event_key,field,state,applicable,reason) VALUES(?,?,?,?,?,?)",
            (code, item.event_id, item.field, item.state, int(item.applicable), item.reason),
        )
        connection.run_many(
            "INSERT INTO company_field_evidence(code,event_key,field,position,source,source_key,record_hash,body_hash,"
            "rule_id,published_at,start_line,end_line,quote,unit,subject_type) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ((code, item.event_id, item.field, position, evidence.source, evidence.source_key, evidence.record_hash,
              evidence.body_hash, evidence.rule_id, EncodeCompanyTime(evidence.published_at), evidence.start_line,
              evidence.end_line, evidence.quote, evidence.unit, SUBJECTS[evidence.subject])
             for position, evidence in enumerate(item.evidence)),
        )


def ReadCompanyFieldResults(connection: SqliteConnection, codes: Sequence[str]) -> tuple[CompanyFieldConclusion, ...]:
    """Read explicit stored conclusions, joining values from the same pinned fact snapshot."""
    requested = tuple(sorted({_code(code) for code in codes}))
    results: list[CompanyFieldConclusion] = []
    for offset in range(0, len(requested), 512):
        batch = requested[offset:offset + 512]
        slots = ",".join("?" for _ in batch)
        values, subjects = _bindings(connection, batch)
        published: set[tuple[str, str, str]] = set()
        evidence_rows: dict[tuple[str, str, str], list[CompanyFieldEvidence]] = {}
        for row in connection.run(
            "SELECT code,event_key,field,position,source,source_key,record_hash,body_hash,rule_id,published_at,"
            f"start_line,end_line,quote,unit,subject_type FROM company_field_evidence WHERE code IN ({slots}) "
            "ORDER BY code,event_key,field,position", batch,
        ):
            key = (_code(row[0]), _text(row[1]), _text(row[2]))
            members = evidence_rows.setdefault(key, [])
            if CompanyInteger(row[3]) != len(members):
                raise ValueError("company_field_result_evidence_position_invalid")
            proof = CompanyFieldEvidence(
                _text(row[4]), _text(row[5]), _text(row[6]), _hash(row[7]), _text(row[8]), RequireCompanyTime(row[9]),
                CompanyInteger(row[10]), CompanyInteger(row[11]), _text(row[12]), cast(CompanyFieldUnit, _text(row[13])),
                DecodeCompanyEnum(SUBJECTS, row[14]),
            )
            if key not in subjects or proof.subject is not subjects[key]:
                raise ValueError("company_field_result_subject_mismatch")
            members.append(proof)
        for row in connection.run(
            f"SELECT code,event_key,field,state,applicable,reason FROM company_field_result WHERE code IN ({slots}) "
            "ORDER BY code,event_key,field", batch,
        ):
            key = (_code(row[0]), _text(row[1]), _text(row[2]))
            if key not in values:
                raise ValueError("company_field_result_fact_field_missing")
            published.add(key)
            applicable = CompanyInteger(row[4])
            if applicable not in (0, 1):
                raise ValueError("company_field_result_applicability_invalid")
            results.append(CompanyFieldConclusion(
                key[0], key[1], cast(CompanyFieldName, key[2]), values[key],
                cast(CompanyFieldConclusionState, _text(row[3])), bool(applicable), _text(row[5]),
                tuple(evidence_rows.pop(key, ())),
            ))
        if evidence_rows:
            raise ValueError("company_field_result_orphan_evidence")
        if published != set(values):
            raise ValueError("company_field_result_coverage_incomplete")
    return tuple(results)


__all__ = [
    "WriteCompanyFieldResults",
    "ReadCompanyFieldResults",
]
