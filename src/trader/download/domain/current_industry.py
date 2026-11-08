"""Current industry identities admitted by the download publication boundary."""

from __future__ import annotations

import re
from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from trader.infra.serialization.canonical_codec import CanonicalDigest

_CLASSIFICATION = "中国上市公司协会上市公司行业分类标准"


@dataclass(frozen=True)
class CurrentIndustryReference:
    industry_id: str
    classification: str
    effective_date: date
    source: str
    data_version: str

    def __post_init__(self) -> None:
        if (
            not self.industry_id.startswith("capco:") or not self.industry_id[6:].strip()
            or self.classification != _CLASSIFICATION
            or not self.source.strip()
            or not self.data_version.strip()
        ):
            raise ValueError("current industry reference identity is invalid")


@dataclass(frozen=True)
class CurrentIndustryMember:
    code: str
    reference: CurrentIndustryReference
    name: str

    def __post_init__(self) -> None:
        if (
            re.fullmatch(r"[0-9]{6}", self.code) is None
            or not isinstance(self.reference, CurrentIndustryReference)
            or not self.name.strip() or self.name != self.name.strip()
            or re.search(r"[\u4e00-\u9fff]", self.name) is None
            or re.fullmatch(r"capco:[A-S][0-9]{2}" + re.escape(self.name), self.reference.industry_id) is None
        ):
            raise ValueError("current_industry_member_invalid")


@dataclass(frozen=True)
class CurrentIndustryGap:
    code: str
    reason: str

    def __post_init__(self) -> None:
        if re.fullmatch(r"[0-9]{6}", self.code) is None or re.fullmatch(r"[a-z][a-z0-9_]*", self.reason) is None:
            raise ValueError("current_industry_gap_invalid")


@dataclass(frozen=True)
class CurrentIndustrySnapshot:
    """One immutable current grouping, with publication evidence outside its semantic identity."""

    members: tuple[CurrentIndustryMember, ...]
    gaps: tuple[CurrentIndustryGap, ...]
    observed_at: datetime
    refresh_after: datetime
    source: str
    classification: str
    publication_hash: str
    content_hash: str = field(init=False)
    _codes: tuple[str, ...] = field(init=False, repr=False, compare=False)
    _gap_codes: tuple[str, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            type(self.members) is not tuple or type(self.gaps) is not tuple
            or any(not isinstance(item, CurrentIndustryMember) for item in self.members)
            or any(not isinstance(item, CurrentIndustryGap) for item in self.gaps)
            or self.observed_at.utcoffset() != timedelta(hours=8)
            or self.refresh_after.utcoffset() != timedelta(hours=8)
            or not self.observed_at < self.refresh_after <= self.observed_at + timedelta(days=7)
            or not self.source.strip() or self.classification != _CLASSIFICATION
            or re.fullmatch(r"[0-9a-f]{64}", self.publication_hash) is None
            or any(item.reference.classification != self.classification or item.reference.source != self.source for item in self.members)
        ):
            raise ValueError("current_industry_snapshot_invalid")
        codes = tuple(item.code for item in self.members)
        gap_codes = tuple(item.code for item in self.gaps)
        if codes != tuple(sorted(set(codes))) or gap_codes != tuple(sorted(set(gap_codes))) or set(codes) & set(gap_codes):
            raise ValueError("current_industry_snapshot_population_invalid")
        object.__setattr__(self, "_codes", codes)
        object.__setattr__(self, "_gap_codes", gap_codes)
        object.__setattr__(self, "content_hash", CanonicalDigest((
            self.classification,
            tuple((item.code, item.reference.industry_id, item.name) for item in self.members),
            tuple((item.code, item.reason) for item in self.gaps),
        )))

    def member(self, code: str) -> CurrentIndustryMember | None:
        position = bisect_left(self._codes, code)
        return self.members[position] if position < len(self._codes) and self._codes[position] == code else None

    def project(self, codes: tuple[str, ...]) -> CurrentIndustrySnapshot:
        """Select a sorted price population without inventing classifications for absent codes."""
        if (
            type(codes) is not tuple or codes != tuple(sorted(set(codes)))
            or any(re.fullmatch(r"[0-9]{6}", code) is None for code in codes)
        ):
            raise ValueError("current_industry_snapshot_population_invalid")
        members: list[CurrentIndustryMember] = []
        gaps: list[CurrentIndustryGap] = []
        for code in codes:
            member = self.member(code)
            if member is not None:
                members.append(member)
                continue
            position = bisect_left(self._gap_codes, code)
            gaps.append(self.gaps[position] if position < len(self._gap_codes) and self._gap_codes[position] == code
                        else CurrentIndustryGap(code, "current_industry_not_published"))
        return CurrentIndustrySnapshot(
            members=tuple(members),
            gaps=tuple(gaps),
            observed_at=self.observed_at,
            refresh_after=self.refresh_after,
            source=self.source,
            classification=self.classification,
            publication_hash=self.publication_hash,
        )


__all__ = [
    "CurrentIndustryGap",
    "CurrentIndustryMember",
    "CurrentIndustryReference",
    "CurrentIndustrySnapshot",
]
