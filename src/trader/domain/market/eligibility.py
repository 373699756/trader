"""Pure issuer-level permanent eligibility rules applied before per-stock I/O."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from trader.domain.market.models import MarketQuote
from trader.domain.market.research import CorporateRiskCategory, ResearchObservation

_CODE = re.compile(r"^[0-9]{6}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:/@+-]{1,240}$")


class IssuerEligibilityReason(str, Enum):
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
class IssuerEligibilityDecision:
    code: str
    state: IssuerEligibilityState
    observed_at: datetime
    reason: IssuerEligibilityReason | None = None
    effective_at: datetime | None = None
    evidence_hash: str | None = None

    def __post_init__(self) -> None:
        if _CODE.fullmatch(self.code) is None:
            raise ValueError("issuer eligibility decision code is invalid")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("issuer eligibility decision time must be timezone-aware")
        excluded = self.state is IssuerEligibilityState.PERMANENTLY_EXCLUDED
        if excluded != (self.reason is not None and self.effective_at is not None and self.evidence_hash is not None):
            raise ValueError("permanent issuer exclusion must bind its complete fact identity")


@dataclass(frozen=True)
class IssuerEligibilityReasonCount:
    reason: IssuerEligibilityReason
    count: int

    def __post_init__(self) -> None:
        if self.count < 0:
            raise ValueError("issuer eligibility reason count cannot be negative")


@dataclass(frozen=True)
class IssuerEligibilityRegistryStatus:
    schema_version: str
    fact_count: int
    excluded_count: int
    reason_counts: tuple[IssuerEligibilityReasonCount, ...]
    manifest_hash: str
    integrity_ok: bool
    persistence_error_count: int
    last_error: str | None = None

    def __post_init__(self) -> None:
        if self.fact_count < 0 or self.excluded_count < 0 or self.persistence_error_count < 0:
            raise ValueError("issuer eligibility status counts cannot be negative")
        if self.excluded_count > self.fact_count:
            raise ValueError("issuer exclusion count cannot exceed fact count")
        if _HASH.fullmatch(self.manifest_hash) is None:
            raise ValueError("issuer eligibility manifest hash must be SHA-256")


@dataclass(frozen=True)
class _FactEvidence:
    evidence_id: str
    source: str
    material: tuple[tuple[str, str], ...] = ()


def resolve_issuer_eligibility(
    facts: tuple[IssuerEligibilityFact, ...],
    code: str,
    observed_at: datetime,
) -> IssuerEligibilityDecision:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("issuer eligibility observation time must be timezone-aware")
    active = tuple(sorted(fact for fact in facts if fact.code == code and fact.effective_at <= observed_at))
    if not active:
        return IssuerEligibilityDecision(code, IssuerEligibilityState.ELIGIBLE_UNVERIFIED, observed_at)
    fact = active[0]
    return IssuerEligibilityDecision(
        code,
        IssuerEligibilityState.PERMANENTLY_EXCLUDED,
        observed_at,
        fact.reason,
        fact.effective_at,
        fact.evidence_hash,
    )


def eligibility_facts_from_quote(
    quote: MarketQuote,
    *,
    observed_at: datetime,
) -> tuple[IssuerEligibilityFact, ...]:
    facts: list[IssuerEligibilityFact] = []
    quote_version = _sha256(
        {
            "code": quote.code,
            "data_version": quote.data_version,
            "source": quote.source,
        }
    )
    quote_identity = f"quote:{quote.code}:{quote_version}"
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


_PERMANENT_RISK_REASONS = {
    CorporateRiskCategory.FINANCIAL_FRAUD: IssuerEligibilityReason.CONFIRMED_FINANCIAL_FRAUD,
    CorporateRiskCategory.MAJOR_ILLEGAL: IssuerEligibilityReason.CONFIRMED_MAJOR_ILLEGAL,
    CorporateRiskCategory.FUND_OCCUPATION: IssuerEligibilityReason.CONFIRMED_FUND_OCCUPATION,
    CorporateRiskCategory.ILLEGAL_GUARANTEE: IssuerEligibilityReason.CONFIRMED_ILLEGAL_GUARANTEE,
    CorporateRiskCategory.FORCED_DELISTING: IssuerEligibilityReason.CONFIRMED_FORCED_DELISTING,
}


def eligibility_facts_from_research(
    code: str,
    observation: ResearchObservation,
) -> tuple[IssuerEligibilityFact, ...]:
    facts: list[IssuerEligibilityFact] = []
    reports = observation.financial_history or ((observation.financial,) if observation.financial is not None else ())
    for report in reports:
        if report.report_date.month != 12:
            continue
        if not any(value is not None and value < 0.0 for value in (report.parent_net_profit, report.core_net_profit)):
            continue
        evidence_id = f"financial:{code}:{report.report_date.isoformat()}"
        facts.append(
            _fact(
                code,
                IssuerEligibilityReason.HISTORICAL_AUDITED_LOSS,
                report.published_at,
                _FactEvidence(
                    evidence_id,
                    "eastmoney_financial",
                    (
                        ("parent_net_profit", repr(report.parent_net_profit)),
                        ("core_net_profit", repr(report.core_net_profit)),
                    ),
                ),
            )
        )
    for risk in observation.corporate_risk_facts:
        reason = _PERMANENT_RISK_REASONS.get(risk.category)
        if reason is None:
            continue
        facts.append(_fact(code, reason, risk.announced_at, _FactEvidence(risk.evidence_id, risk.source)))
    return tuple(sorted(facts))


def manual_blacklist_fact(code: str, effective_at: datetime, config_hash: str) -> IssuerEligibilityFact:
    evidence_version = hashlib.sha256(config_hash.encode("utf-8")).hexdigest()
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


def issuer_eligibility_fact_hash(fact: IssuerEligibilityFact) -> str:
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
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "IssuerEligibilityDecision",
    "IssuerEligibilityFact",
    "IssuerEligibilityReason",
    "IssuerEligibilityReasonCount",
    "IssuerEligibilityRegistryStatus",
    "IssuerEligibilityState",
    "eligibility_facts_from_quote",
    "eligibility_facts_from_research",
    "issuer_eligibility_fact_hash",
    "manual_blacklist_fact",
    "resolve_issuer_eligibility",
]
