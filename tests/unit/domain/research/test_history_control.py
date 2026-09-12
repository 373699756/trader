from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from trader.domain.research.history_control import (
    HistoryActiveSnapshot,
    HistoryCalendarIdentity,
    HistoryControlState,
    HistoryReminderClaim,
    HistoryReminderState,
    HistorySecurityIdentity,
    HistorySnapshotPartition,
    HistorySourceIdentity,
    HistorySyncCheckpoint,
    HistoryTrainingDueRequest,
    HistoryTrainingDueState,
    HistoryUniverseIdentity,
    calculate_history_training_cache_invalidation_dates,
    calculate_history_training_due,
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
        partitions=(HistorySnapshotPartition("partitions/2026/09.sqlite3", "a" * 64, 6),),
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
    reminder_claim = HistoryReminderClaim("due-20260910", date(2026, 9, 10), NOW)
    state = HistoryControlState(
        (source,),
        (calendar,),
        (universe,),
        (checkpoint,),
        (due,),
        (reminder,),
        (reminder_claim,),
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
        HistorySnapshotPartition(f"partitions/2026/09/{'a' * 64}.sqlite3", "a" * 64, 6)
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
                HistorySnapshotPartition("partitions/2026/09.sqlite3", "a" * 64, 6),
                HistorySnapshotPartition("partitions/2026/09.sqlite3", "a" * 64, 6),
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
    with pytest.raises(ValueError, match="reminder claim"):
        HistoryReminderClaim("due-20260910", date(2026, 9, 9), NOW)


def test_history_control_state_canonicalizes_caller_owned_collections() -> None:
    source = _source()
    calendar = _calendar(source)
    universe = _universe(source)
    snapshot = _snapshot(source, calendar, universe)
    caller_sources = [source]

    state = HistoryControlState(caller_sources, [calendar], [universe], [], [], [], [], [snapshot], None)  # type: ignore[arg-type]
    caller_sources.clear()

    assert state.sources == (source,)
    assert state.calendars == (calendar,)
    assert state.active_snapshot is None


def test_training_due_counts_matured_exchange_sessions_not_calendar_days() -> None:
    dates = tuple(date(2026, 8, 1) + timedelta(days=index) for index in range(25))
    state = calculate_history_training_due(
        HistoryTrainingDueRequest(
            due_identity="due-cadence",
            baseline_label_cutoff=dates[0],
            current_label_cutoff=dates[-1],
            calendar_dates=dates,
            input_revision=False,
            observed_at=NOW,
        )
    )

    assert state.reason == "cadence_due"
    assert state.matured_label_days_since_training == 24
    assert state.training_due is True


def test_training_due_changes_from_not_due_on_the_twentieth_mature_label_day() -> None:
    dates = tuple(date(2026, 8, 1) + timedelta(days=index) for index in range(21))
    nineteenth = calculate_history_training_due(
        HistoryTrainingDueRequest(
            due_identity="due-nineteenth",
            baseline_label_cutoff=dates[0],
            current_label_cutoff=dates[19],
            calendar_dates=dates,
            input_revision=False,
            observed_at=NOW,
        )
    )
    twentieth = calculate_history_training_due(
        HistoryTrainingDueRequest(
            due_identity="due-twentieth",
            baseline_label_cutoff=dates[0],
            current_label_cutoff=dates[20],
            calendar_dates=dates,
            input_revision=False,
            observed_at=NOW,
        )
    )

    assert nineteenth.reason == "not_due"
    assert nineteenth.matured_label_days_since_training == 19
    assert twentieth.reason == "cadence_due"
    assert twentieth.matured_label_days_since_training == 20


def test_training_due_prefers_revision_and_does_not_advance_on_incomplete_data() -> None:
    dates = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))
    revised = calculate_history_training_due(
        HistoryTrainingDueRequest(
            due_identity="due-revision",
            baseline_label_cutoff=dates[0],
            current_label_cutoff=dates[-1],
            calendar_dates=dates,
            input_revision=True,
            observed_at=NOW,
        )
    )
    incomplete = calculate_history_training_due(
        HistoryTrainingDueRequest(
            due_identity="due-incomplete",
            baseline_label_cutoff=dates[0],
            current_label_cutoff=dates[-1],
            calendar_dates=dates,
            input_revision=False,
            observed_at=NOW,
            data_complete=False,
        )
    )

    assert revised.reason == "input_revision_due"
    assert incomplete.reason == "data_incomplete"
    assert incomplete.matured_label_days_since_training == 0

    ahead = calculate_history_training_due(
        HistoryTrainingDueRequest(
            due_identity="due-ahead",
            baseline_label_cutoff=dates[-1],
            current_label_cutoff=dates[-2],
            calendar_dates=dates,
            input_revision=False,
            observed_at=NOW,
        )
    )
    assert ahead.reason == "data_incomplete"


def test_training_due_rebuilds_when_the_execution_contract_changes() -> None:
    dates = tuple(date(2026, 7, 1) + timedelta(days=index) for index in range(40))

    state = calculate_history_training_due(
        HistoryTrainingDueRequest(
            due_identity="due-contract",
            baseline_label_cutoff=dates[-2],
            current_label_cutoff=dates[-1],
            calendar_dates=dates,
            input_revision=False,
            observed_at=NOW,
            training_contract_changed=True,
        )
    )

    assert state.reason == "training_contract_due"
    assert state.training_due is True
    assert state.input_revision is False


def test_training_due_requires_initial_training_without_a_bundle_baseline() -> None:
    dates = (date(2026, 9, 8), date(2026, 9, 9))
    state = calculate_history_training_due(
        HistoryTrainingDueRequest(
            due_identity="due-initial",
            baseline_label_cutoff=None,
            current_label_cutoff=dates[-1],
            calendar_dates=dates,
            input_revision=False,
            observed_at=NOW,
        )
    )

    assert state.reason == "initial_training_required"
    assert state.training_due is True


def test_training_revision_invalidates_t1_label_and_sixty_feature_dependants() -> None:
    dates = tuple(date(2026, 1, 1) + timedelta(days=index) for index in range(80))

    invalidated = calculate_history_training_cache_invalidation_dates(dates, (dates[10], dates[30]))

    assert invalidated == dates[9:]
