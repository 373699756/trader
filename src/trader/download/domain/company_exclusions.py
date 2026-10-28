"""Non-ST permanent exclusions published by the download boundary."""

from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum


class CompanyExclusionReason(IntEnum):
    MANUAL_BLACKLIST = 1
    DELISTING = 2


class CompanyExclusionSource(IntEnum):
    STRATEGY_CONFIG = 1
    MARKET_QUOTE = 2
    SECURITY_MASTER = 3


@dataclass(frozen=True, slots=True)
class CompanyExclusion:
    code: str
    reason: CompanyExclusionReason
    effective_at: datetime
    source: CompanyExclusionSource
    source_key: str
    evidence_hash: bytes

    def __post_init__(self) -> None:
        if (
            len(self.code) != 6
            or not self.code.isascii()
            or not self.code.isdigit()
            or self.effective_at.utcoffset() is None
            or not 1 <= len(self.source_key) <= 240
            or not isinstance(self.evidence_hash, bytes)
            or len(self.evidence_hash) != 32
        ):
            raise ValueError("company_exclusion_invalid")


__all__ = ["CompanyExclusion", "CompanyExclusionReason", "CompanyExclusionSource"]
