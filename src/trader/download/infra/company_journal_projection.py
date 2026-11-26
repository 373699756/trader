"""Incremental views of the existing company step and request ledger on one owned connection."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from trader.download.application.company_failures import IsRetryableCompanySourceFailure
from trader.download.domain.company_evidence import IsTransientCompanyDocumentFailure
from trader.download.domain.company_sync import CompanySyncFamily, CompanySyncFamilyResult, CompanySyncStepResult
from trader.infra.sqlite_connection import SqliteConnection


DIRECTORY_RECOVERY_GAP_PREFIX = "directory_response_recovered:"


def JournalStrings(value: str) -> tuple[str, ...]:
    parsed: object = json.loads(value)
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise ValueError("company_sync_journal_strings_invalid")
    return tuple(parsed)


def CanRetryJournalRejection(processed: int, failure: str | None, gaps: tuple[str, ...]) -> bool:
    if processed:
        return False
    return (failure is None and "company_raw_capacity_unavailable" in gaps
            or IsRetryableCompanySourceFailure(failure) and "supplier_input_rejected" in gaps)


@dataclass
class _RequestCounts:
    latest_attempt: int = 0
    used: int = 0
    by_family: Counter[str] = field(default_factory=Counter)
    by_step: Counter[tuple[str, int]] = field(default_factory=Counter)
    unobserved: dict[int, str] = field(default_factory=dict)
    unknown: Counter[str] = field(default_factory=Counter)

    def read(self, connection: SqliteConnection, round_id: str) -> None:
        pending = tuple(self.unobserved)
        for offset in range(0, len(pending), 128):
            batch = pending[offset:offset + 128]
            marks = ",".join("?" for _ in batch)
            for (attempt,) in connection.run(f"SELECT attempt FROM company_sync_request WHERE observed=1 AND attempt IN ({marks})", batch):
                self.unknown[self.unobserved.pop(attempt)] -= 1
        for attempt, family, step, observed in connection.run(
            "SELECT attempt,family,step,observed FROM company_sync_request WHERE attempt>? AND round_id=? ORDER BY attempt",
            (self.latest_attempt, round_id),
        ):
            self.latest_attempt = attempt
            self.used += 1
            self.by_family[family] += 1
            self.by_step[(family, step)] += 1
            if not observed:
                self.unobserved[attempt] = family
                self.unknown[family] += 1


@dataclass
class _FamilySteps:
    refresh: bool
    plan_gaps: tuple[str, ...]
    step: int = 0
    latest: CompanySyncStepResult | None = None
    accepted: int | None = 0
    changed: set[str] = field(default_factory=set)
    gaps: Counter[str] = field(default_factory=Counter)
    errors: dict[int, str] = field(default_factory=dict)
    removable: dict[int, tuple[str, ...]] = field(default_factory=dict)
    retry_by_state: dict[str, list[int]] = field(default_factory=dict)

    def _remove(self, step: int) -> None:
        self.gaps.subtract(self.removable.pop(step, ()))
        self.errors.pop(step, None)

    def read(self, connection: SqliteConnection, round_id: str, family: CompanySyncFamily) -> None:
        for row in connection.run("""SELECT step,processed,can_continue,accepted,changed_codes,gaps,pending,failure,deferred,state_hash
            FROM company_sync_step WHERE round_id=? AND family=? AND step>? AND complete=1 ORDER BY step""",
            (round_id, family.value, self.step)):
            step, processed, can_continue, accepted, codes, reasons, pending, failure, deferred, state_hash = row
            gaps = JournalStrings(reasons)
            self.step = step
            self.accepted = None if self.accepted is None or accepted is None else self.accepted + accepted
            self.latest = CompanySyncStepResult(bool(processed), bool(can_continue), accepted_inputs=accepted,
                gaps=gaps, pending_count=pending, failure=failure, deferred=bool(deferred))
            if deferred:
                continue
            self.changed.update(JournalStrings(codes))
            if self.refresh:
                continue
            if family is CompanySyncFamily.ANNOUNCEMENTS:
                for gap in gaps:
                    if gap.startswith(DIRECTORY_RECOVERY_GAP_PREFIX):
                        self._remove(int(gap[len(DIRECTORY_RECOVERY_GAP_PREFIX):]))
            if failure is None and all(gap in self.plan_gaps for gap in gaps):
                for old in self.retry_by_state.pop(state_hash, ()):
                    self._remove(old)
            recoverable = ("announcement_document_failed" in gaps or "required_original_recovery_failed" in gaps
                or failure is not None and IsTransientCompanyDocumentFailure(failure))
            if recoverable:
                self.retry_by_state.setdefault(state_hash, []).append(step)
            if recoverable or failure == "source_attempt_unconfirmed":
                self.removable[step] = gaps
            if failure is not None:
                self.errors[step] = failure
            self.gaps.update(gaps)

    def result(self, family: CompanySyncFamily, receipts: _RequestCounts) -> CompanySyncFamilyResult | None:
        latest = self.latest
        if latest is None:
            return None
        gaps = latest.gaps
        if not self.refresh:
            reasons = set(gaps) | {gap for gap, count in self.gaps.items() if count > 0}
            # Resource blocks describe the latest dispatch. The failed step
            # remains in the ledger after capacity or request budget returns.
            for gap in ("source_budget_exhausted", "company_raw_capacity_unavailable"):
                if gap not in gaps:
                    reasons.discard(gap)
            gaps = tuple(sorted(gap for gap in reasons if not gap.startswith(DIRECTORY_RECOVERY_GAP_PREFIX)))
        unknown = receipts.unknown[family.value]
        can_retry = not unknown and CanRetryJournalRejection(latest.processed_scope_complete, latest.failure, latest.gaps)
        result = CompanySyncStepResult(latest.processed_scope_complete, latest.can_continue or can_retry,
            latest.accepted_inputs, receipts.by_step[(family.value, self.step)], tuple(sorted(self.changed)), gaps,
            latest.pending_count, next(iter(self.errors.values()), latest.failure), latest.deferred)
        return CompanySyncFamilyResult(family, self.step, self.accepted, receipts.by_family[family.value], result, unknown)


class CompanyJournalProjection:
    """Pull committed deltas inside the caller's transaction; external commits invalidate the whole view."""

    def __init__(self, connection: SqliteConnection) -> None:
        self._connection = connection
        self._round_id = ""
        self._version: int | None = None
        self._requests = _RequestCounts()
        self._families: dict[CompanySyncFamily, _FamilySteps] = {}

    def _sync(self, round_id: str) -> None:
        if not self._connection.in_transaction:
            raise ValueError("company_sync_projection_transaction_required")
        version = self._connection.run("PRAGMA data_version").require_one()[0]
        if self._round_id != round_id or self._version != version:
            self._round_id, self._version = round_id, version
            self._requests = _RequestCounts()
            self._families.clear()
        self._requests.read(self._connection, round_id)

    def request_counts(self, round_id: str) -> tuple[int, int]:
        with self._read(round_id):
            return self._requests.used, len(self._requests.unobserved)

    def family(self, round_id: str, family: CompanySyncFamily) -> CompanySyncFamilyResult | None:
        with self._read(round_id):
            plan = self._connection.run("SELECT refresh_on_input_change,gaps FROM company_sync_plan WHERE round_id=? AND family=?",
                (round_id, family.value)).fetchone()
            if plan is None:
                return None
            refresh, plan_gaps = bool(plan[0]), JournalStrings(plan[1])
            steps = self._families.get(family)
            if steps is None or steps.refresh != refresh or steps.plan_gaps != plan_gaps:
                steps = self._families[family] = _FamilySteps(refresh, plan_gaps)
            steps.read(self._connection, round_id, family)
            return steps.result(family, self._requests)

    @contextmanager
    def _read(self, round_id: str) -> Iterator[None]:
        try:
            self._sync(round_id)
            yield
        except BaseException:
            # A cancelled SQL scan may have delivered only a prefix of a row set.
            self._version = None
            raise
