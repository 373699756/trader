"""Immutable history eligibility and dated industry facts, independent of daily prices."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from trader.download.domain.baostock_daily import BaoStockIndustryInterval
from trader.training.domain.evaluation.artifact_identity import canonical_artifact_hash


@dataclass(frozen=True)
class HistoryStEvidence:
    code: str
    checked_on: date
    status: Literal["ever_st", "clear", "unknown"]
    source: Literal["sina_company_name_history"] = "sina_company_name_history"

    def __post_init__(self) -> None:
        if (
            len(self.code) != 6
            or not self.code.isdigit()
            or type(self.checked_on) is not date
            or self.status not in {"ever_st", "clear", "unknown"}
            or self.source != "sina_company_name_history"
        ):
            raise ValueError("history ST evidence is invalid")


@dataclass(frozen=True)
class HistoryReferenceSnapshot:
    eligible_codes: tuple[str, ...]
    industry_intervals: tuple[BaoStockIndustryInterval, ...]
    st_evidence: tuple[HistoryStEvidence, ...]

    def __post_init__(self) -> None:
        codes = tuple(sorted(set(self.eligible_codes)))
        evidence = tuple(sorted(self.st_evidence, key=lambda item: item.code))
        intervals = tuple(sorted(self.industry_intervals, key=lambda item: (item.code, item.effective_from)))
        if (
            not codes
            or len(codes) != len(self.eligible_codes)
            or any(len(code) != 6 or not code.isdigit() for code in codes)
            or len({item.code for item in evidence}) != len(evidence)
            or any(item.code not in codes for item in intervals)
            or any(item.code in codes and item.status != "clear" for item in evidence)
            or not set(codes).issubset(item.code for item in evidence)
        ):
            raise ValueError("history reference eligibility is invalid")
        for left, right in zip(intervals, intervals[1:], strict=False):
            if left.code == right.code and (left.effective_to is None or left.effective_to > right.effective_from):
                raise ValueError("history industry intervals overlap")
        object.__setattr__(self, "eligible_codes", codes)
        object.__setattr__(self, "industry_intervals", intervals)
        object.__setattr__(self, "st_evidence", evidence)

    @property
    def content_hash(self) -> str:
        return canonical_artifact_hash(self)
