"""Immutable published field conclusions, distinct from document processing receipts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from math import isfinite
from typing import Literal

from trader.download.domain.company_events import CompanySubject


CompanyFieldName = Literal[
    "start_date", "end_date", "planned_pct", "completion_disclosed_at", "actual_pct", "repurchase_purpose",
    "contract_amount", "annual_revenue", "revenue_report_date", "revenue_disclosed_at", "contract_revenue_pct",
    "amount_basis",
]
CompanyFieldConclusionValue = float | str | date | datetime | None
CompanyFieldConclusionState = Literal[
    "verified", "present_unverified", "not_applicable", "pending_download", "pending_extraction",
    "pending_publication", "verification_required", "unsupported", "out_of_range", "not_disclosed",
]
CompanyFieldUnit = Literal["percent", "cny", "date", "time", "text"]

_FIELDS = frozenset({
    "start_date", "end_date", "planned_pct", "completion_disclosed_at", "actual_pct", "repurchase_purpose",
    "contract_amount", "annual_revenue", "revenue_report_date", "revenue_disclosed_at", "contract_revenue_pct",
    "amount_basis",
})
_STATES = frozenset({
    "verified", "present_unverified", "not_applicable", "pending_download", "pending_extraction",
    "pending_publication", "verification_required", "unsupported", "out_of_range", "not_disclosed",
})
_UNITS = frozenset({"percent", "cny", "date", "time", "text"})


def _identifier(value: object) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 240 and all(ord(character) >= 32 for character in value)


@dataclass(frozen=True, slots=True)
class CompanyFieldEvidence:
    """A bounded line span in the exact, hash-identified source text."""

    source: str
    source_key: str
    record_hash: str
    body_hash: bytes
    rule_id: str
    published_at: datetime
    start_line: int
    end_line: int
    quote: str
    unit: CompanyFieldUnit
    subject: CompanySubject

    def __post_init__(self) -> None:
        if not all(_identifier(value) for value in (self.source, self.source_key, self.record_hash, self.rule_id)):
            raise ValueError("company_field_evidence_identity_invalid")
        if not isinstance(self.body_hash, bytes) or len(self.body_hash) != 32:
            raise ValueError("company_field_evidence_hash_invalid")
        if not isinstance(self.published_at, datetime) or self.published_at.utcoffset() is None:
            raise ValueError("company_field_evidence_time_invalid")
        if type(self.start_line) is not int or type(self.end_line) is not int or not 1 <= self.start_line <= self.end_line:
            raise ValueError("company_field_evidence_lines_invalid")
        if not isinstance(self.quote, str) or not self.quote.strip() or len(self.quote) > 1000 or "\x00" in self.quote:
            raise ValueError("company_field_evidence_quote_invalid")
        if not isinstance(self.unit, str) or self.unit not in _UNITS:
            raise ValueError("company_field_evidence_unit_invalid")
        if not isinstance(self.subject, CompanySubject):
            raise ValueError("company_field_evidence_subject_invalid")


@dataclass(frozen=True, slots=True)
class CompanyFieldConclusion:
    """A current fact value plus its separately published evidence conclusion.

    ``value`` is a read/write validation snapshot. The SQL codec keeps its sole
    persisted owner in the existing fact columns, never in a second value table.
    Evidence presence alone does not infer a conclusion or non-disclosure.
    """

    code: str
    event_id: str
    field: CompanyFieldName
    value: CompanyFieldConclusionValue
    state: CompanyFieldConclusionState
    applicable: bool
    reason: str
    evidence: tuple[CompanyFieldEvidence, ...]

    def __post_init__(self) -> None:
        if (not isinstance(self.code, str) or len(self.code) != 6
            or not self.code.isascii() or not self.code.isdigit() or not _identifier(self.event_id)):
            raise ValueError("company_field_conclusion_identity_invalid")
        if (not isinstance(self.field, str) or self.field not in _FIELDS
            or not isinstance(self.state, str) or self.state not in _STATES or not _identifier(self.reason)):
            raise ValueError("company_field_conclusion_label_invalid")
        value = self.value
        if value is not None:
            if type(value) is float:
                if not isfinite(value):
                    raise ValueError("company_field_conclusion_value_invalid")
            elif isinstance(value, datetime):
                if value.utcoffset() is None:
                    raise ValueError("company_field_conclusion_value_time_invalid")
            elif type(value) is date:
                pass
            elif isinstance(value, str):
                if not value or len(value) > 1000 or "\x00" in value:
                    raise ValueError("company_field_conclusion_value_invalid")
            else:
                raise ValueError("company_field_conclusion_value_invalid")
        if type(self.applicable) is not bool or self.applicable != (self.state != "not_applicable"):
            raise ValueError("company_field_conclusion_applicability_invalid")
        if not isinstance(self.evidence, tuple) or any(not isinstance(item, CompanyFieldEvidence) for item in self.evidence):
            raise ValueError("company_field_conclusion_evidence_invalid")
        if self.state == "verified" and (self.value is None or not self.evidence):
            raise ValueError("company_field_conclusion_verified_proof_missing")
        if self.state == "present_unverified" and self.value is None:
            raise ValueError("company_field_conclusion_present_value_missing")
        if self.state == "not_disclosed" and (self.value is not None or not self.evidence):
            raise ValueError("company_field_conclusion_non_disclosure_proof_missing")


@dataclass(frozen=True, slots=True)
class CompanyFieldSnapshot:
    """Field conclusions read under the same immutable publication identity."""

    publication_hash: str
    results: tuple[CompanyFieldConclusion, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.publication_hash, str) or self.publication_hash and (
            len(self.publication_hash) != 64 or any(character not in "0123456789abcdefABCDEF" for character in self.publication_hash)
        ):
            raise ValueError("company_field_snapshot_identity_invalid")
        if not isinstance(self.results, tuple) or any(not isinstance(item, CompanyFieldConclusion) for item in self.results):
            raise ValueError("company_field_snapshot_results_invalid")
        if not self.publication_hash and self.results:
            raise ValueError("company_field_snapshot_publication_missing")
        keys = tuple((item.code, item.event_id, item.field) for item in self.results)
        if len(set(keys)) != len(keys):
            raise ValueError("company_field_snapshot_duplicate")


__all__ = [
    "CompanyFieldName",
    "CompanyFieldConclusionValue",
    "CompanyFieldConclusionState",
    "CompanyFieldUnit",
    "CompanyFieldEvidence",
    "CompanyFieldConclusion",
    "CompanyFieldSnapshot",
]
