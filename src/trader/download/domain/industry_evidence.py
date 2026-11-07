"""Dated CAPCO source rows and their explicitly bounded public-use intervals."""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import re

from trader.infra.serialization.canonical_codec import CanonicalDigest

_HASH = re.compile(r"[0-9a-f]{64}")
_CATEGORY = re.compile(r"[A-S][0-9]{2}")


@dataclass(frozen=True)
class OfficialIndustryEvidence:
    code: str
    source_url: str
    document_sha256: str
    disclosed_on: date
    effective_from: date
    effective_to: date
    received_at: datetime
    page: int
    excerpt: str
    native_category: str
    native_name: str
    announcement_url: str
    announcement_sha256: str
    identity: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            re.fullmatch(r"[0-9]{6}", self.code) is None
            or not self.source_url.startswith("https://")
            or not self.announcement_url.startswith("https://")
            or _HASH.fullmatch(self.document_sha256) is None
            or _HASH.fullmatch(self.announcement_sha256) is None
            or type(self.disclosed_on) is not date
            or type(self.effective_from) is not date
            or type(self.effective_to) is not date
            or not self.disclosed_on < self.effective_from < self.effective_to
            or self.received_at.utcoffset() != timedelta(hours=8)
            or self.disclosed_on > self.received_at.date()
            or type(self.page) is not int or not 1 <= self.page <= 300
            or not self.excerpt or len(self.excerpt) > 2000
            or _CATEGORY.fullmatch(self.native_category) is None
            or not self.native_name.strip() or len(self.native_name) > 100
            or self.native_name != self.native_name.strip()
        ):
            raise ValueError("official_industry_evidence_invalid")
        object.__setattr__(self, "identity", CanonicalDigest(self))

    @property
    def industry(self) -> str:
        return "capco:" + self.native_category + self.native_name

    @property
    def classification(self) -> str:
        return "中国上市公司协会上市公司行业分类标准"

    def applies(self, day: date) -> bool:
        return self.effective_from <= day < self.effective_to


__all__ = ["OfficialIndustryEvidence"]
