from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from trader.download.domain.history_control import (
    HistoryActiveSnapshot,
    HistoryCalendarIdentity,
    HistorySecurityIdentity,
    HistorySnapshotPartition,
    HistorySourceIdentity,
    HistoryUniverseIdentity,
)
from trader.training.domain.training_due import TrainingDueState
from trader.training.domain.training_reminder import (
    DeriveTrainingReminderStatus,
    TrainingMaintenanceState,
    TrainingReminderClaim,
    TrainingReminderState,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 9, 10, 20, 30, tzinfo=SHANGHAI)


def _control_state(
    *,
    claims: tuple[TrainingReminderClaim, ...] = (),
    reminders: tuple[TrainingReminderState, ...] = (),
) -> TrainingMaintenanceState:
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
        (HistorySnapshotPartition("prices_2026.sqlite3", "a" * 64, 2, calendar_month=9),),
    )
    due = TrainingDueState(
        "due-20260910",
        "initial_training_required",
        None,
        date(2026, 9, 10),
        0,
        False,
        NOW,
    )
    return TrainingMaintenanceState(
        snapshot,
        (due,),
        reminders,
        claims,
    )


def test_due_projection_uses_only_persisted_state_and_tracks_daily_reminder_phase() -> None:
    pending = DeriveTrainingReminderStatus(_control_state(), NOW, archive_root=Path("data/market/history"))
    claim = TrainingReminderClaim("due-20260910", NOW.date(), NOW)
    claimed = DeriveTrainingReminderStatus(
        _control_state(claims=(claim,)), NOW, archive_root=Path("data/market/history")
    )
    reminder = TrainingReminderState("due-20260910", NOW.date(), "sent", NOW, None)
    sent = DeriveTrainingReminderStatus(
        _control_state(claims=(claim,), reminders=(reminder,)), NOW, archive_root=Path("data/market/history")
    )

    assert pending.training_due is True
    assert pending.training_due_reason == "initial_training_required"
    assert pending.reminder_state == "pending"
    assert claimed.reminder_state == "claimed"
    assert sent.reminder_state == "sent"
    assert sent.automatic_model_update is False


def test_unresolved_due_becomes_pending_again_on_the_next_shanghai_date() -> None:
    claim = TrainingReminderClaim("due-20260910", NOW.date(), NOW)
    reminder = TrainingReminderState("due-20260910", NOW.date(), "sent", NOW, None)

    status = DeriveTrainingReminderStatus(
        _control_state(claims=(claim,), reminders=(reminder,)),
        NOW.replace(day=11),
        archive_root=Path("data/market/history"),
    )

    assert status.reminder_date == date(2026, 9, 11)
    assert status.reminder_state == "pending"


def test_changed_training_contract_remains_visible_as_a_pending_reminder() -> None:
    control = _control_state()
    due = replace(
        control.due_states[0],
        reason="training_contract_due",
        baseline_label_cutoff=date(2026, 9, 9),
        matured_label_days_since_training=1,
    )
    status = DeriveTrainingReminderStatus(
        replace(control, due_states=(due,)), NOW, archive_root=Path("data/market/history")
    )

    assert status.state == "ready"
    assert status.training_due is True
    assert status.training_due_reason == "training_contract_due"
    assert status.reminder_state == "pending"
    assert status.automatic_model_update is False


def test_missing_control_state_fails_closed_without_fabricating_due_count() -> None:
    status = DeriveTrainingReminderStatus(None, NOW, archive_root=Path("data/market/history"))

    assert status.state == "data_incomplete"
    assert status.reason == "history_control_unavailable"
    assert status.matured_label_days_since_training == 0
    assert status.training_due is False
    assert status.reminder_state == "not_due"
