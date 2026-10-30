"""In-memory qualification using reference ST and download-owned non-ST facts."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from datetime import datetime
from itertools import islice
from threading import Lock, RLock

from trader.download.application.company_exclusions import PublishCompanyExclusions
from trader.download.domain.company_exclusions import CompanyExclusion, CompanyExclusionReason, CompanyExclusionSource
from trader.infra.shutdown import ShutdownDeadline, ShutdownStep
from trader.infra.workers import ManagedWorkerExecutor
from trader.recommendation.domain.market.eligibility import (
    IssuerEligibilityFact,
    IssuerEligibilityFactHash,
    IssuerEligibilityFreeze,
    IssuerEligibilityReason,
    IssuerEligibilityReasonCount,
    IssuerEligibilitySnapshot,
    IssuerEligibilityView,
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
    def __init__(
        self,
        publication: PublishCompanyExclusions,
        *,
        read_only: bool = False,
        workers: ManagedWorkerExecutor | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._publication = publication
        self._read_only = read_only
        self._workers = workers
        self._monotonic = monotonic
        self._lock = RLock()
        self._write_lock = Lock()
        self._initialized = False
        self._facts: dict[tuple[str, IssuerEligibilityReason, str], IssuerEligibilityFact] = {}
        self._pending: dict[tuple[str, IssuerEligibilityReason, str], CompanyExclusion] = {}
        self._ordered: tuple[IssuerEligibilityFact, ...] | None = None
        self._reason_counts: tuple[IssuerEligibilityReasonCount, ...] = ()
        self._digest = ""
        self._excluded_count = 0
        self._view: IssuerEligibilityView | None = None
        self._valid_from: datetime | None = None
        self._valid_until: datetime | None = None
        self._running = False
        self._closing = False
        self._inflight = False
        self._retry_at = 0.0
        self._stop_deadline: ShutdownDeadline | None = None
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
        with self._lock:
            self._initialize()
            if self._closing:
                raise RuntimeError("company_exclusions_stopped")
            incoming: dict[tuple[str, IssuerEligibilityReason, str], IssuerEligibilityFact] = {}
            for fact in facts:
                previous = incoming.get(fact.identity, self._facts.get(fact.identity))
                if previous is not None and previous != fact:
                    raise ValueError("company_exclusion_evidence_conflict")
                if fact.reason not in (*_REASONS, IssuerEligibilityReason.HISTORICAL_ST):
                    raise ValueError("company_exclusion_reason_inactive")
                if fact.reason in _REASONS and fact.source not in _SOURCES:
                    raise ValueError("company_exclusion_source_invalid")
                if previous is None:
                    incoming[fact.identity] = fact
            # Newly observed ST remains in memory. Reference maintenance owns its persistence.
            persistent = {
                fact.identity: CompanyExclusion(
                    fact.code,
                    _REASONS[fact.reason],
                    fact.effective_at,
                    _SOURCES[fact.source],
                    fact.evidence_id,
                    bytes.fromhex(fact.evidence_hash),
                )
                for fact in incoming.values()
                if fact.reason in _REASONS
            }
            if persistent and self._read_only:
                raise RuntimeError("company_exclusions_read_only")
            self._facts.update(incoming)
            self._pending.update(persistent)
            if incoming:
                self._ordered = None
                self._view = None
            self._schedule_locked()
        if self._workers is None and not self._read_only:
            self._flush(raise_on_error=True)
        return len(incoming)

    def start(self) -> bool:
        if self._workers is None:
            return False
        with self._lock:
            if self._running or self._closing:
                return False
            self._workers.start()
            self._running = True
            self._schedule_locked()
            return True

    def stop(self, *, wait: bool, deadline: ShutdownDeadline | None = None) -> ShutdownStep:
        with self._lock:
            if deadline is not None:
                self._stop_deadline = deadline
            if not self._closing:
                self._schedule_locked(force=True)
            self._closing = True
        if self._workers is not None and wait:
            report = self._workers.stop(wait=True, cancel_futures=False, deadline=deadline)
            with self._lock:
                self._running = False
                if report.completed:
                    self._inflight = False
            if not report.completed:
                return report
        with self._lock:
            remaining = len(self._pending)
            completed = not remaining and not self._inflight
        return ShutdownStep(
            "company_exclusions",
            completed,
            not completed and deadline is not None and deadline.expired,
            detail=f"pending company exclusions: {remaining}" if not completed else "",
        )

    @property
    def write_cancelled(self) -> bool:
        with self._lock:
            return self._stop_deadline is not None and self._stop_deadline.expired

    def _schedule_locked(self, *, force: bool = False) -> None:
        if (
            self._workers is None or not self._running or self._closing or self._inflight or not self._pending
            or (not force and self._monotonic() < self._retry_at)
        ):
            return
        self._inflight = True
        future = self._workers.submit(self._flush, raise_on_error=False)
        if future is None:
            self._inflight = False
            self._last_error = "company_exclusions_writer_unavailable"
            self._retry_at = self._monotonic() + 60.0
            return
        future.add_done_callback(self._written)

    def _written(self, future: Future[None]) -> None:
        with self._lock:
            self._inflight = False
            if future.cancelled() or future.exception() is not None:
                self._errors += 1
                self._last_error = "company_exclusions_worker_failed"
                self._retry_at = self._monotonic() + 60.0
            self._schedule_locked()

    def _flush(self, *, raise_on_error: bool) -> None:
        with self._write_lock:
            while True:
                with self._lock:
                    batch = tuple(islice(self._pending.items(), 512))
                if not batch:
                    return
                try:
                    self._publication.execute(tuple(fact for _, fact in batch))
                except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
                    with self._lock:
                        self._errors += 1
                        self._last_error = "company_exclusions_persistence_failed"
                        self._retry_at = self._monotonic() + 60.0
                    if raise_on_error:
                        raise RuntimeError("company_exclusions_persistence_failed") from exc
                    return
                with self._lock:
                    for identity, fact in batch:
                        if self._pending.get(identity) == fact:
                            del self._pending[identity]
                    if self._integrity_ok:
                        self._last_error = None
                    self._retry_at = 0.0

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
            if self._ordered is None:
                self._ordered = tuple(sorted(self._facts.values()))
                counts = Counter(fact.reason for fact in self._ordered)
                self._reason_counts = tuple(
                    IssuerEligibilityReasonCount(reason, count) for reason, count in sorted(counts.items())
                )
                self._excluded_count = len({fact.code for fact in self._ordered})
                self._digest = hashlib.sha256(
                    "\n".join(IssuerEligibilityFactHash(fact) for fact in self._ordered).encode()
                ).hexdigest()
            return self._ordered

    def view(self, observed_at: datetime) -> IssuerEligibilityView:
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("issuer eligibility observation time must be timezone-aware")
        with self._lock:
            if (
                self._view is not None
                and (self._valid_from is None or self._valid_from <= observed_at)
                and (self._valid_until is None or observed_at < self._valid_until)
            ):
                return self._view
            effective: list[IssuerEligibilityFact] = []
            future: list[IssuerEligibilityFact] = []
            for fact in self.facts():
                (effective if fact.effective_at <= observed_at else future).append(fact)
            selected: dict[str, IssuerEligibilityFact] = {}
            for fact in effective:
                selected.setdefault(fact.code, fact)
            reasons = {code: fact.reason for code, fact in selected.items()}
            self._view = IssuerEligibilityView(
                tuple(selected.values()),
                reasons,
                frozenset(code for code, reason in reasons.items() if reason in {
                    IssuerEligibilityReason.HISTORICAL_ST, IssuerEligibilityReason.HISTORICAL_DELISTING_WARNING,
                }),
                frozenset((fact.code, fact.reason) for fact in effective),
            )
            self._valid_from = max((fact.effective_at for fact in effective), default=None)
            self._valid_until = min((fact.effective_at for fact in future), default=None)
            return self._view

    def exclusions(self, observed_at: datetime) -> tuple[IssuerEligibilityFreeze, ...]:
        return tuple(ResolveIssuerEligibility((fact,), fact.code, observed_at) for fact in self.view(observed_at).facts)

    def filter_codes(self, codes: Sequence[str], observed_at: datetime) -> tuple[str, ...]:
        excluded = self.view(observed_at).reasons
        if not self._integrity_ok:
            return ()
        return tuple(code for code in dict.fromkeys(codes) if code not in excluded)

    def status(self) -> IssuerEligibilitySnapshot:
        with self._lock:
            facts = self.facts()
            return IssuerEligibilitySnapshot(
                "company_exclusions",
                len(facts),
                self._excluded_count,
                self._reason_counts,
                self._digest,
                self._integrity_ok,
                self._errors,
                self._last_error,
                refresh_state=("failed" if not self._integrity_ok or self._last_error else "pending" if self._pending else "ready"),
                pending_persistence_count=len(self._pending),
            )


__all__ = ["CompanyEligibility"]
