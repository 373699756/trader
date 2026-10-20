"""One immutable official reference input shared by a manual download wave."""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from trader.download.domain.baostock_daily import BaoStockCalendar, BaoStockSecurity
from trader.download.domain.history_reference import HistoryStEvidence
from trader.download.domain.security_eligibility import IsStSecurityName
from trader.infra.serialization.canonical_codec import CanonicalDigest


@dataclass(frozen=True)
class PreparedReference:
    official_universe: tuple[BaoStockSecurity, ...]
    st_evidence: tuple[HistoryStEvidence, ...]
    checked_at: datetime
    open_dates: tuple[date, ...]
    industry_version: str | None = None
    identity: str = field(init=False)

    def __post_init__(self) -> None:
        codes = tuple(item.code for item in self.official_universe)
        evidence_codes = tuple(item.code for item in self.st_evidence)
        if (
            self.checked_at.tzinfo is None
            or self.checked_at.utcoffset() != timedelta(hours=8)
            or not codes
            or len(codes) > 12_000
            or codes != tuple(sorted(set(codes)))
            or evidence_codes != codes
            or any(item.checked_on != self.checked_at.date() for item in self.st_evidence)
            or not self.open_dates
            or len(self.open_dates) > 20_000
            or self.open_dates != tuple(sorted(set(self.open_dates)))
            or self.open_dates[-1] < self.checked_at.date()
        ):
            raise ValueError("prepared_reference_incomplete")
        object.__setattr__(
            self,
            "identity",
            CanonicalDigest(
                {
                    "official": tuple(item.content_hash for item in self.official_universe),
                    "st": tuple((item.code, item.checked_on.isoformat(), item.status) for item in self.st_evidence),
                    "checked_on": self.checked_at.date().isoformat(),
                    "open_dates": tuple(day.isoformat() for day in self.open_dates),
                    "industry_version": self.industry_version,
                }
            ),
        )

    @property
    def eligible_universe(self) -> tuple[BaoStockSecurity, ...]:
        return tuple(
            security
            for security, evidence in zip(self.official_universe, self.st_evidence, strict=True)
            if evidence.status == "clear" and not IsStSecurityName(security.name)
        )

    def calendar(self, as_of: date, sessions: int) -> BaoStockCalendar:
        dates = tuple(day for day in self.open_dates if day <= as_of)
        if not 1 <= sessions <= 2000 or len(dates) < sessions or as_of > self.open_dates[-1]:
            raise ValueError("prepared_calendar_incomplete")
        return BaoStockCalendar(dates[-sessions:])


__all__ = ["PreparedReference"]
