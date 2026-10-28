"""Persisted training reminder identities and their read-only status."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from trader.download.domain.history_control import HistorySnapshot
from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.training.domain.training_due import TrainingDueReason, TrainingDueState

_IDENTITY = re.compile(r"^[a-z0-9_.:-]{1,128}$")
_ERROR_CODE = re.compile(r"^[a-z0-9_]{1,64}$")
_SHANGHAI = ZoneInfo("Asia/Shanghai")


TrainingReminderOutcome = Literal["sent", "notification_degraded"]


def _require_identity(value: str, label: str) -> None:
    if _IDENTITY.fullmatch(value) is None:
        raise ValueError(f"history {label} is invalid")


def _require_shanghai(value: datetime, label: str) -> None:
    if value.tzinfo != _SHANGHAI:
        raise ValueError(f"history {label} must use Asia/Shanghai")


@dataclass(frozen=True)
class TrainingReminderClaim:
    due_identity: str
    reminder_date: date
    claimed_at: datetime
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_identity(self.due_identity, "reminder claim due identity")
        _require_shanghai(self.claimed_at, "reminder claim")
        if self.claimed_at.date() != self.reminder_date:
            raise ValueError("history reminder claim is invalid")
        object.__setattr__(self, "content_hash", CanonicalDigest(self))


@dataclass(frozen=True)
class TrainingReminderState:
    due_identity: str
    reminder_date: date
    outcome: TrainingReminderOutcome
    attempted_at: datetime
    error_code: str | None
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_identity(self.due_identity, "reminder due identity")
        _require_shanghai(self.attempted_at, "reminder attempt")
        valid = (
            self.outcome == "sent"
            and self.error_code is None
            or self.outcome == "notification_degraded"
            and self.error_code is not None
            and _ERROR_CODE.fullmatch(self.error_code) is not None
        )
        if not valid or self.attempted_at.date() != self.reminder_date:
            raise ValueError("history reminder state is invalid")
        object.__setattr__(self, "content_hash", CanonicalDigest(self))


@dataclass(frozen=True)
class TrainingMaintenanceState:
    """Bounded persisted state required by unattended synchronization status."""

    published_snapshot: HistorySnapshot | None
    due_states: tuple[TrainingDueState, ...]
    reminders: tuple[TrainingReminderState, ...]
    reminder_claims: tuple[TrainingReminderClaim, ...]

    def __post_init__(self) -> None:
        due_states = tuple(sorted(self.due_states, key=lambda item: item.due_identity))
        reminders = tuple(sorted(self.reminders, key=lambda item: (item.due_identity, item.reminder_date)))
        reminder_claims = tuple(sorted(self.reminder_claims, key=lambda item: (item.due_identity, item.reminder_date)))
        for values, identities in (
            (due_states, tuple(item.due_identity for item in due_states)),
            (reminders, tuple((item.due_identity, item.reminder_date) for item in reminders)),
            (reminder_claims, tuple((item.due_identity, item.reminder_date) for item in reminder_claims)),
        ):
            if len(values) != len(set(identities)):
                raise ValueError("history automation control contains duplicate identities")
        due_identities = {item.due_identity for item in due_states}
        unsealed_reminder = any(item.due_identity not in due_identities for item in reminders)
        unsealed_claim = any(item.due_identity not in due_identities for item in reminder_claims)
        if unsealed_reminder or unsealed_claim:
            raise ValueError("history automation control contains an unsealed due identity")
        object.__setattr__(self, "due_states", due_states)
        object.__setattr__(self, "reminders", reminders)
        object.__setattr__(self, "reminder_claims", reminder_claims)


TrainingReminderStatusState = Literal["ready", "data_incomplete"]
TrainingReminderPhase = Literal["not_due", "pending", "claimed", "sent", "notification_degraded"]


@dataclass(frozen=True)
class TrainingReminderStatus:
    state: TrainingReminderStatusState
    reason: str | None
    history_root: Path
    snapshot_hash: str | None
    data_cutoff: date | None
    label_cutoff: date | None
    due_identity: str | None
    matured_label_days_since_training: int
    training_due: bool
    training_due_reason: TrainingDueReason
    reminder_date: date
    reminder_state: TrainingReminderPhase
    reminder_error_code: str | None
    automatic_model_update: bool

    def __post_init__(self) -> None:
        if (
            self.matured_label_days_since_training < 0
            or self.automatic_model_update
            or self.training_due
            != (
                self.training_due_reason
                in {"initial_training_required", "cadence_due", "input_revision_due", "training_contract_due"}
            )
            or (self.training_due and self.due_identity is None)
            or (not self.training_due and self.reminder_state != "not_due")
            or (self.reminder_state == "notification_degraded") != (self.reminder_error_code is not None)
        ):
            raise ValueError("history automation status is inconsistent")


def DeriveTrainingReminderStatus(
    control: TrainingMaintenanceState | None,
    observed_at: datetime,
    *,
    history_root: Path,
) -> TrainingReminderStatus:
    """Project the already-persisted due/reminder state without recalculation."""

    if observed_at.tzinfo != _SHANGHAI:
        raise ValueError("history automation observation must use Asia/Shanghai")
    reminder_date = observed_at.date()
    if control is None:
        return _incomplete_status(history_root, reminder_date, "history_control_unavailable")
    if control.published_snapshot is None:
        return _incomplete_status(history_root, reminder_date, "history_snapshot_unavailable")
    current_snapshot = control.published_snapshot
    matching = tuple(item for item in control.due_states if item.current_label_cutoff == current_snapshot.label_cutoff)
    if not matching:
        return _incomplete_status(
            history_root,
            reminder_date,
            "history_due_state_unavailable",
            current_snapshot,
        )
    due = max(matching, key=lambda item: (item.observed_at, item.due_identity))
    reminder = next(
        (
            item
            for item in control.reminders
            if item.due_identity == due.due_identity and item.reminder_date == reminder_date
        ),
        None,
    )
    claimed = any(
        item.due_identity == due.due_identity and item.reminder_date == reminder_date
        for item in control.reminder_claims
    )
    if not due.training_due:
        reminder_state: TrainingReminderPhase = "not_due"
        reminder_error = None
    elif reminder is not None:
        reminder_state = reminder.outcome
        reminder_error = reminder.error_code
    elif claimed:
        reminder_state = "claimed"
        reminder_error = None
    else:
        reminder_state = "pending"
        reminder_error = None
    return TrainingReminderStatus(
        "data_incomplete" if due.reason == "data_incomplete" else "ready",
        "history_data_incomplete" if due.reason == "data_incomplete" else None,
        history_root,
        current_snapshot.content_hash,
        current_snapshot.data_cutoff,
        current_snapshot.label_cutoff,
        due.due_identity,
        due.matured_label_days_since_training,
        due.training_due,
        due.reason,
        reminder_date,
        reminder_state,
        reminder_error,
        False,
    )


def _incomplete_status(
    history_root: Path,
    reminder_date: date,
    reason: str,
    current_snapshot: HistorySnapshot | None = None,
) -> TrainingReminderStatus:
    return TrainingReminderStatus(
        "data_incomplete",
        reason,
        history_root,
        current_snapshot.content_hash if current_snapshot is not None else None,
        current_snapshot.data_cutoff if current_snapshot is not None else None,
        current_snapshot.label_cutoff if current_snapshot is not None else None,
        None,
        0,
        False,
        "data_incomplete",
        reminder_date,
        "not_due",
        None,
        False,
    )


__all__ = [
    "TrainingMaintenanceState",
    "TrainingReminderClaim",
    "TrainingReminderOutcome",
    "TrainingReminderState",
    "TrainingReminderStatus",
    "TrainingReminderPhase",
    "DeriveTrainingReminderStatus",
]
