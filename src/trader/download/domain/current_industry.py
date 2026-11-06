"""Current industry identities admitted by the download publication boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


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
            or self.classification != "中国上市公司协会上市公司行业分类标准"
            or not self.source.strip()
            or not self.data_version.strip()
        ):
            raise ValueError("current industry reference identity is invalid")
