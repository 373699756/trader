from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from trader.domain.research.history_control import (
    HistoryActiveSnapshot,
    HistoryCalendarIdentity,
    HistoryControlState,
    HistoryReminderState,
    HistorySecurityIdentity,
    HistorySnapshotPartition,
    HistorySourceIdentity,
    HistorySyncCheckpoint,
    HistoryTrainingDueState,
    HistoryUniverseIdentity,
)

NOW = datetime(2026, 9, 10, 20, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


def _source() -> HistorySourceIdentity:
    return HistorySourceIdentity("baostock", "baostock_daily", "python-sdk", NOW)


def _calendar(source: HistorySourceIdentity) -> HistoryCalendarIdentity:
    return HistoryCalendarIdentity(
        (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10)),
        source.content_hash,
    )


def _universe(source: HistorySourceIdentity) -> HistoryUniverseIdentity:
    return HistoryUniverseIdentity(
        (
            HistorySecurityIdentity("600001", "浦发银行", "main", date(1999, 11, 10), None),
            HistorySecurityIdentity("300001", "特锐德", "chinext", date(2009, 10, 30), None),
        ),
        source.content_hash,
    )


def _snapshot(
    source: HistorySourceIdentity,
    calendar: HistoryCalendarIdentity,
    universe: HistoryUniverseIdentity,
) -> HistoryActiveSnapshot:
    return HistoryActiveSnapshot(
        sequence=1,
        data_cutoff=date(2026, 9, 10),
        label_cutoff=date(2026, 9, 9),
        calendar_hash=calendar.content_hash,
        universe_hash=universe.content_hash,
        source_identity_hash=source.content_hash,
        partitions=(HistorySnapshotPartition(f"partitions/2026/09/{'a' * 64}.sqlite3", "a" * 64, 6),),
    )


def test_history_control_values_are_immutable_and_hash_bound() -> None:
    source = _source()
    calendar = _calendar(source)
    universe = _universe(source)
    snapshot = _snapshot(source, calendar, universe)
    checkpoint = HistorySyncCheckpoint("sync-20260910", 1, "running", NOW, 2, 10, None)
    due = HistoryTrainingDueState(
        "due-20260910",
        "cadence_due",
        date(2026, 8, 12),
        date(2026, 9, 9),
        20,
        False,
        NOW,
    )
    reminder = HistoryReminderState("due-20260910", date(2026, 9, 10), "sent", NOW, None)
    state = HistoryControlState(
        (source,),
        (calendar,),
        (universe,),
        (checkpoint,),
        (due,),
        (reminder,),
        (snapshot,),
        snapshot.content_hash,
    )

    assert due.training_due is True
    assert state.active_snapshot == snapshot
    assert len({source.content_hash, calendar.content_hash, universe.content_hash, snapshot.content_hash}) == 4
    with pytest.raises(FrozenInstanceError):
        snapshot.sequence = 2  # type: ignore[misc]
    with pytest.raises(ValueError, match="Shanghai"):
        replace(source, observed_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="active snapshot"):
        replace(state, active_snapshot_hash="b" * 64)


def test_history_control_rejects_invalid_progress_due_and_reminder_states() -> None:
    with pytest.raises(ValueError, match="partition"):
        HistorySnapshotPartition("partitions/2026/09.sqlite3", "a" * 64, 6)
    source = _source()
    calendar = _calendar(source)
    universe = _universe(source)
    with pytest.raises(ValueError, match="snapshot"):
        HistoryActiveSnapshot(
            1,
            date(2026, 9, 10),
            date(2026, 9, 9),
            calendar.content_hash,
            universe.content_hash,
            source.content_hash,
            (
                HistorySnapshotPartition(f"partitions/2026/09/{'a' * 64}.sqlite3", "a" * 64, 6),
                HistorySnapshotPartition(f"partitions/2026/09/{'b' * 64}.sqlite3", "b" * 64, 6),
            ),
        )
    with pytest.raises(ValueError, match="checkpoint"):
        HistorySyncCheckpoint("sync-20260910", 1, "completed", NOW, 9, 10, None)
    with pytest.raises(ValueError, match="checkpoint"):
        HistorySyncCheckpoint("sync-20260910", 1, "running", NOW, 9, 10, "supplier_failed")
    with pytest.raises(ValueError, match="due"):
        HistoryTrainingDueState(
            "due-20260910",
            "not_due",
            date(2026, 8, 12),
            date(2026, 9, 9),
            20,
            False,
            NOW,
        )
    with pytest.raises(ValueError, match="reminder"):
        HistoryReminderState("due-20260910", date(2026, 9, 10), "sent", NOW, "notify_failed")
    with pytest.raises(ValueError, match="reminder"):
        HistoryReminderState("due-20260910", date(2026, 9, 9), "sent", NOW, None)


def test_history_control_state_canonicalizes_caller_owned_collections() -> None:
    source = _source()
    calendar = _calendar(source)
    universe = _universe(source)
    snapshot = _snapshot(source, calendar, universe)
    caller_sources = [source]

    state = HistoryControlState(caller_sources, [calendar], [universe], [], [], [], [snapshot], None)  # type: ignore[arg-type]
    caller_sources.clear()

    assert state.sources == (source,)
    assert state.calendars == (calendar,)
    assert state.active_snapshot is None
