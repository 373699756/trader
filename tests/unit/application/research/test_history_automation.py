from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from trader.download.domain.history_automation import (
    derive_history_automation_status,
    weekly_history_maintenance_decision,
)
from trader.download.domain.history_control import (
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
        (HistorySnapshotPartition("partitions/2026/09.sqlite3", "a" * 64, 2),),
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


@pytest.mark.parametrize(
    "cutoff,observed,state,reason",
    (
        (None, NOW, "blocked", "history_snapshot_unavailable"),
        (date(2026, 9, 11), NOW, "blocked", "history_snapshot_future"),
        (date(2026, 9, 4), NOW, "not_due", "weekly_cadence_not_due"),
        (date(2026, 9, 3), NOW.replace(hour=15, minute=9), "not_due", "maintenance_window_not_open"),
        (date(2026, 9, 3), NOW.replace(hour=15, minute=10), "due", None),
        (date(2026, 9, 3), NOW, "due", None),
        (date(2026, 9, 3), NOW.replace(day=12), "due", None),
        (date(2026, 9, 3), NOW.replace(hour=0), "not_due", "maintenance_window_not_open"),
        (date(2026, 9, 3), NOW.replace(month=10, day=1), "due", None),
    ),
)
def test_weekly_archive_cadence_is_independent_of_training_due(cutoff, observed, state, reason) -> None:
    decision = weekly_history_maintenance_decision(cutoff, observed)

    assert (decision.state, decision.reason) == (state, reason)


def test_weekly_cadence_rejects_a_naive_business_clock() -> None:
    with pytest.raises(ValueError, match="Asia/Shanghai"):
        weekly_history_maintenance_decision(NOW.date(), NOW.replace(tzinfo=None))
