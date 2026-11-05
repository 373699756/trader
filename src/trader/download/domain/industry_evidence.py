"""Reviewed official industry evidence and explicit classification mapping."""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import re

from trader.infra.serialization.canonical_codec import CanonicalDigest

_HASH = re.compile(r"[0-9a-f]{64}")
_CATEGORY = re.compile(r"[A-S][0-9]{2}")


@dataclass(frozen=True)
class IndustryCategoryMapping:
    native_classification: str
    native_code: str
    native_name: str
    target_industry: str
    reference_url: str
    reference_sha256: str
    reference_page: int
    reference_excerpt: str
    rationale: str

    def __post_init__(self) -> None:
        if (
            not self.native_classification or len(self.native_classification) > 200
            or _CATEGORY.fullmatch(self.native_code) is None
            or not self.native_name or len(self.native_name) > 100
            or not self.target_industry.startswith(self.native_code)
            or len(self.target_industry) > 200
            or not self.reference_url.startswith("https://")
            or _HASH.fullmatch(self.reference_sha256) is None
            or type(self.reference_page) is not int or not 1 <= self.reference_page <= 1000
            or not self.reference_excerpt or len(self.reference_excerpt) > 2000
            or not self.rationale or len(self.rationale) > 1000
        ):
            raise ValueError("industry_mapping_invalid")


@dataclass(frozen=True)
class OfficialIndustryEvidence:
    code: str
    source_url: str
    document_sha256: str
    disclosed_at: datetime
    effective_from: date
    effective_to: date
    received_at: datetime
    page: int
    excerpt: str
    mapping: IndustryCategoryMapping
    identity: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            re.fullmatch(r"[0-9]{6}", self.code) is None
            or not self.source_url.startswith("https://")
            or _HASH.fullmatch(self.document_sha256) is None
            or any(value.utcoffset() != timedelta(hours=8) for value in (self.disclosed_at, self.received_at))
            or self.disclosed_at > self.received_at
            or type(self.effective_from) is not date or type(self.effective_to) is not date
            or not self.disclosed_at.date() <= self.effective_from < self.effective_to
            or type(self.page) is not int or not 1 <= self.page <= 1000
            or not self.excerpt or len(self.excerpt) > 2000
        ):
            raise ValueError("official_industry_evidence_invalid")

        object.__setattr__(self, "identity", CanonicalDigest(self))

    def applies(self, day: date) -> bool:
        return self.disclosed_at.date() <= self.effective_from <= day < self.effective_to


__all__ = [
    "IndustryCategoryMapping",
    "OfficialIndustryEvidence",
]
