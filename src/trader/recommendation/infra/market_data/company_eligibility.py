"""In-memory qualification using reference ST and download-owned non-ST facts."""

from __future__ import annotations

import hashlib
import sqlite3
from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from threading import RLock

from trader.download.application.company_exclusions import PublishCompanyExclusions
from trader.download.domain.company_exclusions import CompanyExclusion, CompanyExclusionReason, CompanyExclusionSource
from trader.recommendation.domain.market.eligibility import (
    IssuerEligibilityFact,
    IssuerEligibilityFactHash,
    IssuerEligibilityFreeze,
    IssuerEligibilityReason,
    IssuerEligibilityReasonCount,
    IssuerEligibilitySnapshot,
    ManualBlacklistFact,
    ResolveIssuerEligibility,
)

_REASONS = {
    IssuerEligibilityReason.MANUAL_PERMANENT_BLACKLIST: CompanyExclusionReason.MANUAL_BLACKLIST,
    IssuerEligibilityReason.HISTORICAL_DELISTING_WARNING: CompanyExclusionReason.DELISTING,
}
_SOURCES = {
    "strategy_config": CompanyExclusionSource.STRATEGY_CONFIG,
    "market_quote": CompanyExclusionSource.MARKET_QUOTE,
    "exchange_security_master": CompanyExclusionSource.SECURITY_MASTER,
}


class CompanyEligibility:
    def __init__(self, publication: PublishCompanyExclusions, *, read_only: bool = False) -> None:
        self._publication = publication
        self._read_only = read_only
        self._lock = RLock()
        self._initialized = False
        self._facts: dict[tuple[str, IssuerEligibilityReason, str], IssuerEligibilityFact] = {}
        self._integrity_ok = True
        self._errors = 0
        self._last_error: str | None = None

    def _initialize(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        try:
            facts = self._publication.read()
            reasons = {value: key for key, value in _REASONS.items()}
            sources = {value: key for key, value in _SOURCES.items()}
            for item in facts:
                fact = IssuerEligibilityFact(
                    item.code,
                    reasons[item.reason],
                    item.effective_at,
                    item.source_key,
                    sources[item.source],
                    item.evidence_hash.hex(),
                )
                self._facts[fact.identity] = fact
        except (OSError, RuntimeError, ValueError, sqlite3.Error):
            self._integrity_ok = False
            self._last_error = "company_exclusions_unavailable"
            self._errors += 1

    def record(self, facts: Sequence[IssuerEligibilityFact]) -> int:
        if not facts:
            return 0
        with self._lock:
            self._initialize()
            incoming = tuple(fact for fact in facts if fact.identity not in self._facts)
            for fact in facts:
                previous = self._facts.get(fact.identity)
                if previous is not None and previous != fact:
                    raise ValueError("company_exclusion_evidence_conflict")
                if fact.reason not in (*_REASONS, IssuerEligibilityReason.HISTORICAL_ST):
                    raise ValueError("company_exclusion_reason_inactive")
            # Newly observed ST remains in memory. Reference maintenance owns its persistence.
            persistent = tuple(
                CompanyExclusion(
                    fact.code,
                    _REASONS[fact.reason],
                    fact.effective_at,
                    _SOURCES[fact.source],
                    fact.evidence_id,
                    bytes.fromhex(fact.evidence_hash),
                )
                for fact in incoming
                if fact.reason in _REASONS
            )
            for fact in incoming:
                self._facts[fact.identity] = fact
            if persistent:
                if self._read_only:
                    raise RuntimeError("company_exclusions_read_only")
                try:
                    self._publication.execute(persistent)
                except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
                    self._errors += 1
                    self._last_error = "company_exclusions_persistence_failed"
                    raise RuntimeError(self._last_error) from exc
            return len(incoming)

    def record_manual_blacklist(self, codes: Sequence[str], effective_at: datetime, config_hash: str) -> int:
        existing = {
            fact.code
            for fact in self.facts()
            if fact.reason is IssuerEligibilityReason.MANUAL_PERMANENT_BLACKLIST and fact.source == "strategy_config"
        }
        return self.record(
            tuple(ManualBlacklistFact(code, effective_at, config_hash) for code in set(codes) - existing)
        )

    def facts(self) -> tuple[IssuerEligibilityFact, ...]:
        with self._lock:
            self._initialize()
            return tuple(sorted(self._facts.values()))

    def exclusions(self, observed_at: datetime) -> tuple[IssuerEligibilityFreeze, ...]:
        selected: dict[str, IssuerEligibilityFact] = {}
        for fact in self.facts():
            if fact.effective_at <= observed_at:
                selected.setdefault(fact.code, fact)
        return tuple(ResolveIssuerEligibility((fact,), code, observed_at) for code, fact in sorted(selected.items()))

    def filter_codes(self, codes: Sequence[str], observed_at: datetime) -> tuple[str, ...]:
        excluded = {item.code for item in self.exclusions(observed_at)}
        if not self._integrity_ok:
            return ()
        return tuple(code for code in dict.fromkeys(codes) if code not in excluded)

    def status(self) -> IssuerEligibilitySnapshot:
        facts = self.facts()
        counts = Counter(fact.reason for fact in facts)
        digest = hashlib.sha256("\n".join(IssuerEligibilityFactHash(fact) for fact in facts).encode()).hexdigest()
        with self._lock:
            return IssuerEligibilitySnapshot(
                "company_exclusions",
                len(facts),
                len({fact.code for fact in facts}),
                tuple(IssuerEligibilityReasonCount(reason, count) for reason, count in sorted(counts.items())),
                digest,
                self._integrity_ok,
                self._errors,
                self._last_error,
                refresh_state="ready" if self._integrity_ok else "failed",
            )


__all__ = ["CompanyEligibility"]
