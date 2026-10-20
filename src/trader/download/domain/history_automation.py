"""Read-only projection of previously persisted history automation state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from trader.download.domain.history_control import (
    HistoryActiveSnapshot,
    HistoryAutomationControlState,
    HistoryTrainingDueReason,
)

HistoryAutomationState = Literal["ready", "data_incomplete"]
HistoryReminderPhase = Literal["not_due", "pending", "claimed", "sent", "notification_degraded"]

_SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class HistoryAutomationStatus:
    state: HistoryAutomationState
    reason: str | None
    archive_root: Path
    active_snapshot_hash: str | None
    data_cutoff: date | None
    label_cutoff: date | None
    due_identity: str | None
    matured_label_days_since_training: int
    training_due: bool
    training_due_reason: HistoryTrainingDueReason
    reminder_date: date
    reminder_state: HistoryReminderPhase
    reminder_error_code: str | None
    automatic_model_update: bool

    def __post_init__(self) -> None:
        if (
            self.matured_label_days_since_training < 0
            or self.automatic_model_update
            or self.training_due
            != (self.training_due_reason in {"initial_training_required", "cadence_due", "input_revision_due"})
            or (self.training_due and self.due_identity is None)
            or (not self.training_due and self.reminder_state != "not_due")
            or (self.reminder_state == "notification_degraded") != (self.reminder_error_code is not None)
        ):
            raise ValueError("history automation status is inconsistent")


def DeriveHistoryAutomationStatus(
    control: HistoryAutomationControlState | None,
    observed_at: datetime,
    *,
    archive_root: Path,
) -> HistoryAutomationStatus:
    """Project the already-persisted due/reminder state without recalculation."""

    if observed_at.tzinfo != _SHANGHAI:
        raise ValueError("history automation observation must use Asia/Shanghai")
    reminder_date = observed_at.date()
    if control is None:
        return _incomplete_status(archive_root, reminder_date, "history_control_unavailable")
    if control.active_snapshot is None:
        return _incomplete_status(archive_root, reminder_date, "history_snapshot_unavailable")
    active = control.active_snapshot
    matching = tuple(item for item in control.due_states if item.current_label_cutoff == active.label_cutoff)
    if not matching:
        return _incomplete_status(
            archive_root,
            reminder_date,
            "history_due_state_unavailable",
            active,
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
        reminder_state: HistoryReminderPhase = "not_due"
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
    return HistoryAutomationStatus(
        "data_incomplete" if due.reason == "data_incomplete" else "ready",
        "history_data_incomplete" if due.reason == "data_incomplete" else None,
        archive_root,
        active.content_hash,
        active.data_cutoff,
        active.label_cutoff,
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
    archive_root: Path,
    reminder_date: date,
    reason: str,
    active: HistoryActiveSnapshot | None = None,
) -> HistoryAutomationStatus:
    return HistoryAutomationStatus(
        "data_incomplete",
        reason,
        archive_root,
        active.content_hash if active is not None else None,
        active.data_cutoff if active is not None else None,
        active.label_cutoff if active is not None else None,
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
    "HistoryAutomationStatus",
    "HistoryReminderPhase",
    "DeriveHistoryAutomationStatus",
]
