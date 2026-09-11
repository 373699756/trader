from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from trader.application.research.history_automation import derive_history_automation_status
from trader.domain.research.history_control import (
    HistoryActiveSnapshot,
    HistoryAutomationControlState,
    HistoryCalendarIdentity,
    HistoryReminderClaim,
    HistoryReminderState,
    HistorySecurityIdentity,
    HistorySnapshotPartition,
    HistorySourceIdentity,
    HistoryTrainingDueState,
    HistoryUniverseIdentity,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 9, 10, 20, 30, tzinfo=SHANGHAI)


def _control_state(
    *,
    claims: tuple[HistoryReminderClaim, ...] = (),
    reminders: tuple[HistoryReminderState, ...] = (),
) -> HistoryAutomationControlState:
    source = HistorySourceIdentity("baostock", "baostock_daily", "python-sdk", NOW)
    calendar = HistoryCalendarIdentity((date(2026, 9, 9), date(2026, 9, 10)), source.content_hash)
    universe = HistoryUniverseIdentity(
        (HistorySecurityIdentity("600001", "浦发银行", "main", date(1999, 11, 10), None),),
        source.content_hash,
    )
    snapshot = HistoryActiveSnapshot(
        1,
        date(2026, 9, 10),
        date(2026, 9, 10),
        calendar.content_hash,
        universe.content_hash,
        source.content_hash,
        (HistorySnapshotPartition(f"partitions/2026/09/{'a' * 64}.sqlite3", "a" * 64, 2),),
    )
    due = HistoryTrainingDueState(
        "due-20260910",
        "initial_training_required",
        None,
        date(2026, 9, 10),
        0,
        False,
        NOW,
    )
    return HistoryAutomationControlState(
        snapshot,
        (due,),
        reminders,
        claims,
    )


def test_due_projection_uses_only_persisted_state_and_tracks_daily_reminder_phase() -> None:
    pending = derive_history_automation_status(_control_state(), NOW)
    claim = HistoryReminderClaim("due-20260910", NOW.date(), NOW)
    claimed = derive_history_automation_status(_control_state(claims=(claim,)), NOW)
    reminder = HistoryReminderState("due-20260910", NOW.date(), "sent", NOW, None)
    sent = derive_history_automation_status(_control_state(claims=(claim,), reminders=(reminder,)), NOW)

    assert pending.training_due is True
    assert pending.training_due_reason == "initial_training_required"
    assert pending.reminder_state == "pending"
    assert claimed.reminder_state == "claimed"
    assert sent.reminder_state == "sent"
    assert sent.automatic_model_update is False


def test_unresolved_due_becomes_pending_again_on_the_next_shanghai_date() -> None:
    claim = HistoryReminderClaim("due-20260910", NOW.date(), NOW)
    reminder = HistoryReminderState("due-20260910", NOW.date(), "sent", NOW, None)

    status = derive_history_automation_status(
        _control_state(claims=(claim,), reminders=(reminder,)),
        NOW.replace(day=11),
    )

    assert status.reminder_date == date(2026, 9, 11)
    assert status.reminder_state == "pending"


def test_missing_control_state_fails_closed_without_fabricating_due_count() -> None:
    status = derive_history_automation_status(None, NOW)

    assert status.state == "data_incomplete"
    assert status.reason == "history_control_unavailable"
    assert status.matured_label_days_since_training == 0
    assert status.training_due is False
    assert status.reminder_state == "not_due"
