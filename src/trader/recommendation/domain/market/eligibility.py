"""Pure issuer-level permanent eligibility rules applied before per-stock I/O."""

from __future__ import annotations

import re
from bisect import bisect_left
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Literal

from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.recommendation.domain.evidence.pipeline import StageReasonAggregate
from trader.recommendation.domain.market.models import MarketQuote
from trader.recommendation.domain.market.static import StaticIssuer, StaticMarketReference

_CODE = re.compile(r"^[0-9]{6}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:/@+-]{1,240}$")


class IssuerEligibilityReason(str, Enum):
    # Retained only to verify and audit immutable snapshots written by the retired rule.
    HISTORICAL_AUDITED_LOSS = "historical_audited_loss"
    HISTORICAL_ST = "historical_st"
    HISTORICAL_DELISTING_WARNING = "historical_delisting_warning"
    CONFIRMED_FINANCIAL_FRAUD = "confirmed_financial_fraud"
    CONFIRMED_MAJOR_ILLEGAL = "confirmed_major_illegal"
    CONFIRMED_FUND_OCCUPATION = "confirmed_fund_occupation"
    CONFIRMED_ILLEGAL_GUARANTEE = "confirmed_illegal_guarantee"
    CONFIRMED_FORCED_DELISTING = "confirmed_forced_delisting"
    MANUAL_PERMANENT_BLACKLIST = "manual_permanent_blacklist"


class IssuerEligibilityState(str, Enum):
    ELIGIBLE_UNVERIFIED = "eligible_unverified"
    QUALIFICATION_PENDING = "qualification_pending"
    PERMANENTLY_EXCLUDED = "permanently_excluded"


_PERMANENT_EXCLUSION_REASONS = frozenset(
    {
        IssuerEligibilityReason.HISTORICAL_ST,
        IssuerEligibilityReason.HISTORICAL_DELISTING_WARNING,
        IssuerEligibilityReason.MANUAL_PERMANENT_BLACKLIST,
    }
)


def IsPermanentExclusionReason(reason: IssuerEligibilityReason) -> bool:
    return reason in _PERMANENT_EXCLUSION_REASONS


@dataclass(frozen=True)
class HistoricalStEligibilitySnapshot:
    """Fixed published reference population used as the online ST qualification gate."""

    status: Literal["ready", "unavailable"]
    source_identity: str | None
    eligible_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        codes = tuple(self.eligible_codes)
        ready = self.status == "ready"
        if (
            self.status not in {"ready", "unavailable"}
            or ready != (self.source_identity is not None)
            or ready != bool(codes)
            or codes != tuple(sorted(set(codes)))
            or any(_CODE.fullmatch(code) is None for code in codes)
            or (
                self.source_identity is not None
                and (
                    len(self.source_identity) != 64
                    or any(char not in "0123456789abcdef" for char in self.source_identity)
                )
            )
        ):
            raise ValueError("historical ST eligibility snapshot is invalid")
        object.__setattr__(self, "eligible_codes", codes)

    @classmethod
    def unavailable(cls) -> HistoricalStEligibilitySnapshot:
        return cls("unavailable", None)

    @classmethod
    def ready(cls, source_identity: str, eligible_codes: tuple[str, ...]) -> HistoricalStEligibilitySnapshot:
        return cls("ready", source_identity, eligible_codes)

    def eligible(self, code: str) -> bool:
        position = bisect_left(self.eligible_codes, code)
        return position < len(self.eligible_codes) and self.eligible_codes[position] == code


@dataclass(frozen=True, order=True)
class IssuerEligibilityFact:
    code: str
    reason: IssuerEligibilityReason
    effective_at: datetime
    evidence_id: str
    source: str
    evidence_hash: str

    def __post_init__(self) -> None:
        if _CODE.fullmatch(self.code) is None:
            raise ValueError("issuer eligibility code must be a normalized six-digit code")
        if self.effective_at.tzinfo is None or self.effective_at.utcoffset() is None:
            raise ValueError("issuer eligibility effective time must be timezone-aware")
        if _IDENTIFIER.fullmatch(self.evidence_id) is None:
            raise ValueError("issuer eligibility evidence id is invalid")
        if _IDENTIFIER.fullmatch(self.source) is None:
            raise ValueError("issuer eligibility source is invalid")
        if _HASH.fullmatch(self.evidence_hash) is None:
            raise ValueError("issuer eligibility evidence hash must be SHA-256")

    @property
    def identity(self) -> tuple[str, IssuerEligibilityReason, str]:
        return self.code, self.reason, self.evidence_id


@dataclass(frozen=True)
class IssuerEligibilityFreeze:
    code: str
    state: IssuerEligibilityState
    observed_at: datetime
    reason: IssuerEligibilityReason | None = None
    effective_at: datetime | None = None
    evidence_hash: str | None = None

    def __post_init__(self) -> None:
        if _CODE.fullmatch(self.code) is None:
            raise ValueError("issuer eligibility freeze code is invalid")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("issuer eligibility freeze time must be timezone-aware")
        excluded = self.state is IssuerEligibilityState.PERMANENTLY_EXCLUDED
        if excluded != (self.reason is not None and self.effective_at is not None and self.evidence_hash is not None):
            raise ValueError("permanent issuer exclusion must bind its complete fact identity")


@dataclass(frozen=True)
class IssuerEligibilityView:
    facts: tuple[IssuerEligibilityFact, ...]
    reasons: Mapping[str, IssuerEligibilityReason]
    st_excluded_codes: frozenset[str]
    known_reasons: frozenset[tuple[str, IssuerEligibilityReason]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "reasons", MappingProxyType(dict(self.reasons)))


@dataclass(frozen=True)
class IssuerEligibilityReasonCount:
    reason: IssuerEligibilityReason
    count: int

    def __post_init__(self) -> None:
        if self.count < 0:
            raise ValueError("issuer eligibility reason count cannot be negative")


@dataclass(frozen=True)
class IssuerEligibilitySnapshot:
    schema_version: str
    fact_count: int
    excluded_count: int
    reason_counts: tuple[IssuerEligibilityReasonCount, ...]
    manifest_hash: str
    integrity_ok: bool
    persistence_error_count: int
    last_error: str | None = None
    refresh_state: str = "not_ready"
    last_refresh_at: datetime | None = None
    next_refresh_at: datetime | None = None
    refresh_failure_count: int = 0
    pending_persistence_count: int = 0

    def __post_init__(self) -> None:
        if self.fact_count < 0 or self.excluded_count < 0 or self.persistence_error_count < 0:
            raise ValueError("issuer eligibility status counts cannot be negative")
        if self.refresh_failure_count < 0 or self.pending_persistence_count < 0:
            raise ValueError("issuer eligibility refresh failure count cannot be negative")
        if self.excluded_count > self.fact_count:
            raise ValueError("issuer exclusion count cannot exceed fact count")
        if _HASH.fullmatch(self.manifest_hash) is None:
            raise ValueError("issuer eligibility manifest hash must be SHA-256")
        for value in (self.last_refresh_at, self.next_refresh_at):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError("issuer eligibility refresh time must be timezone-aware")


@dataclass(frozen=True)
class IssuerEligibilityBatch:
    """Point-in-time level-one result for one full-market population."""

    input_count: int
    eligible_count: int
    reason_counts: tuple[IssuerEligibilityReasonCount, ...] = ()
    pending_count: int = 0
    company_reasons: tuple[StageReasonAggregate, ...] = ()

    def __post_init__(self) -> None:
        if (
            self.input_count < 0
            or not 0 <= self.eligible_count <= self.input_count
            or not 0 <= self.pending_count <= self.input_count - self.eligible_count
        ):
            raise ValueError("issuer eligibility batch counts are invalid")
        if len({item.reason for item in self.reason_counts}) != len(self.reason_counts):
            raise ValueError("issuer eligibility batch reasons must be unique")
        if (
            sum(item.count for item in self.reason_counts) + sum(item.count for item in self.company_reasons)
            != self.input_count - self.eligible_count - self.pending_count
        ):
            raise ValueError("issuer eligibility batch reasons must explain every exclusion")


@dataclass(frozen=True)
class _FactEvidence:
    evidence_id: str
    source: str
    material: tuple[tuple[str, str], ...] = ()


def ResolveIssuerEligibility(
    facts: tuple[IssuerEligibilityFact, ...],
    code: str,
    observed_at: datetime,
) -> IssuerEligibilityFreeze:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("issuer eligibility observation time must be timezone-aware")
    effective_exclusions = tuple(
        sorted(
            fact
            for fact in facts
            if fact.code == code and fact.effective_at <= observed_at and IsPermanentExclusionReason(fact.reason)
        )
    )
    if not effective_exclusions:
        return IssuerEligibilityFreeze(code, IssuerEligibilityState.ELIGIBLE_UNVERIFIED, observed_at)
    fact = effective_exclusions[0]
    return IssuerEligibilityFreeze(
        code,
        IssuerEligibilityState.PERMANENTLY_EXCLUDED,
        observed_at,
        fact.reason,
        fact.effective_at,
        fact.evidence_hash,
    )


def EligibilityFactsFromQuote(
    quote: MarketQuote,
    *,
    observed_at: datetime,
) -> tuple[IssuerEligibilityFact, ...]:
    facts: list[IssuerEligibilityFact] = []
    quote_identity = f"quote:{quote.code}:{observed_at.isoformat()}"
    evidence_material = (("data_version", quote.data_version), ("source", quote.source))
    if quote.is_st or "ST" in quote.name.upper():
        facts.append(
            _fact(
                quote.code,
                IssuerEligibilityReason.HISTORICAL_ST,
                observed_at,
                _FactEvidence(f"{quote_identity}:st", "market_quote", evidence_material),
            )
        )
    if "退" in quote.name:
        facts.append(
            _fact(
                quote.code,
                IssuerEligibilityReason.HISTORICAL_DELISTING_WARNING,
                observed_at,
                _FactEvidence(f"{quote_identity}:delisting", "market_quote", evidence_material),
            )
        )
    if quote.is_blacklisted:
        facts.append(
            _fact(
                quote.code,
                IssuerEligibilityReason.MANUAL_PERMANENT_BLACKLIST,
                observed_at,
                _FactEvidence(f"{quote_identity}:blacklist", "market_quote", evidence_material),
            )
        )
    return tuple(sorted(facts))


def EligibilityFactsFromStaticIssuer(
    issuer: StaticIssuer,
    *,
    observed_at: datetime,
) -> tuple[IssuerEligibilityFact, ...]:
    if not issuer.is_st:
        return ()
    return (
        _fact(
            issuer.code,
            IssuerEligibilityReason.HISTORICAL_ST,
            observed_at,
            _FactEvidence(
                f"official-static:{issuer.code}:{observed_at.date().isoformat()}",
                "exchange_security_master",
                (("name", issuer.name),),
            ),
        ),
    )


def ManualBlacklistFact(code: str, effective_at: datetime, config_hash: str) -> IssuerEligibilityFact:
    evidence_version = config_hash
    return _fact(
        code,
        IssuerEligibilityReason.MANUAL_PERMANENT_BLACKLIST,
        effective_at,
        _FactEvidence(
            f"config:blacklist:{code}:{evidence_version}",
            "strategy_config",
            (("config_hash", config_hash),),
        ),
    )


def IssuerEligibilityFactHash(fact: IssuerEligibilityFact) -> str:
    return _sha256(
        {
            "code": fact.code,
            "reason": fact.reason.value,
            "effective_at": fact.effective_at.isoformat(),
            "evidence_id": fact.evidence_id,
            "source": fact.source,
            "evidence_hash": fact.evidence_hash,
        }
    )


def _fact(
    code: str,
    reason: IssuerEligibilityReason,
    effective_at: datetime,
    evidence: _FactEvidence,
) -> IssuerEligibilityFact:
    evidence_hash = _sha256(
        {
            "code": code,
            "reason": reason.value,
            "effective_at": effective_at.isoformat(),
            "evidence_id": evidence.evidence_id,
            "source": evidence.source,
            "evidence_material": "\x1f".join(f"{key}\x1e{value}" for key, value in sorted(evidence.material)),
        }
    )
    return IssuerEligibilityFact(
        code,
        reason,
        effective_at,
        evidence.evidence_id,
        evidence.source,
        evidence_hash,
    )


def _sha256(payload: dict[str, str]) -> str:
    return CanonicalDigest(payload, ensure_ascii=False)


def SelectQuotePopulation(
    reference: StaticMarketReference | None,
    exclusions: tuple[IssuerEligibilityFreeze, ...],
    historical_st: HistoricalStEligibilitySnapshot,
) -> tuple[str, ...]:
    """Apply the existing ST-only admission before requesting current quotes."""
    if reference is None:
        return ()
    st_reasons = {IssuerEligibilityReason.HISTORICAL_ST, IssuerEligibilityReason.HISTORICAL_DELISTING_WARNING}
    excluded = {item.code for item in exclusions if item.reason in st_reasons}
    codes = tuple(issuer.code for issuer in reference.records if not issuer.is_st and issuer.code not in excluded)
    return tuple(code for code in codes if historical_st.eligible(code)) if historical_st.status == "ready" else codes


__all__ = [
    "SelectQuotePopulation",
    "HistoricalStEligibilitySnapshot",
    "IssuerEligibilityBatch",
    "IssuerEligibilityFreeze",
    "IssuerEligibilityView",
    "IssuerEligibilityFact",
    "IssuerEligibilityReason",
    "IssuerEligibilityReasonCount",
    "IssuerEligibilitySnapshot",
    "IssuerEligibilityState",
    "IsPermanentExclusionReason",
    "EligibilityFactsFromQuote",
    "EligibilityFactsFromStaticIssuer",
    "IssuerEligibilityFactHash",
    "ManualBlacklistFact",
    "ResolveIssuerEligibility",
]
