"""Resolve the training cadence from the active history and bundle identities."""

from __future__ import annotations

import calendar as month_calendar
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path, PurePosixPath

from trader.domain.research.h1_point_in_time import canonical_hash
from trader.domain.research.history_control import (
    HistoryActiveSnapshot,
    HistoryTrainingDueState,
    calculate_history_training_cache_invalidation_dates,
    calculate_history_training_due,
)
from trader.infra.research.history_control_repository import (
    HistoryControlError,
    SQLiteHistoryControlRepository,
)
from trader.infra.research.history_month_archive import (
    HistoryMonthlyArchiveError,
    SQLiteHistoryMonthlyArchive,
    route_history_months,
)
from trader.infra.scoring.profiles.v3.bundle_store import (
    ActiveTomorrowBundle,
    inspect_active_tomorrow_bundle,
)


@dataclass(frozen=True)
class HistoryTrainingDueEvaluation:
    archive_root: Path
    active_snapshot: HistoryActiveSnapshot
    state: HistoryTrainingDueState
    bundle: ActiveTomorrowBundle | None
    revised_dates: tuple[date, ...]
    invalidated_cache_dates: tuple[date, ...]


def evaluate_history_training_due(
    archive_root: Path,
    training_root: Path,
    observed_at: datetime,
) -> HistoryTrainingDueEvaluation | None:
    """Read and persist one immutable due observation.

    A missing active snapshot is intentionally returned as ``None``: callers
    project that condition as ``history_manifest_unavailable`` instead of
    fabricating a cadence baseline.
    """

    control = SQLiteHistoryControlRepository(archive_root / "control.sqlite3")
    try:
        state = control.load_state()
    except HistoryControlError:
        return None
    active = state.active_snapshot
    if active is None:
        return None
    calendar = next((item for item in state.calendars if item.content_hash == active.calendar_hash), None)
    if calendar is None:
        return None

    bundle, bundle_invalid = _active_bundle(training_root)
    if bundle_invalid:
        due_identity = "due-" + canonical_hash((active.content_hash, "invalid_bundle"))[:32]
        due = calculate_history_training_due(
            due_identity=due_identity,
            baseline_label_cutoff=None,
            current_label_cutoff=active.label_cutoff,
            calendar_dates=calendar.open_dates,
            input_revision=False,
            observed_at=observed_at,
            data_complete=False,
        )
        return HistoryTrainingDueEvaluation(archive_root, active, due, None, (), ())
    baseline_cutoff = bundle.label_cutoff if bundle is not None else None
    baseline = (
        next((item for item in state.snapshots if item.content_hash == bundle.training_input_hash), None)
        if bundle is not None
        else None
    )
    data_complete = bundle is None or baseline is not None
    revised_dates: tuple[date, ...] = ()
    if bundle is not None and baseline is not None and baseline.content_hash != active.content_hash:
        try:
            revised_dates = _revised_dates_since_bundle(
                archive_root,
                baseline,
                active,
                calendar.open_dates,
                bundle.label_cutoff,
            )
        except (HistoryMonthlyArchiveError, OSError, ValueError):
            data_complete = False
    input_revision = bool(revised_dates)
    invalidated_dates = calculate_history_training_cache_invalidation_dates(
        calendar.open_dates,
        revised_dates,
    )
    due_identity = (
        "due-"
        + canonical_hash(
            (
                active.content_hash,
                bundle.training_input_hash if bundle is not None else None,
                baseline_cutoff,
                active.label_cutoff,
                revised_dates,
                data_complete,
            )
        )[:32]
    )
    due = calculate_history_training_due(
        due_identity=due_identity,
        baseline_label_cutoff=baseline_cutoff,
        current_label_cutoff=active.label_cutoff,
        calendar_dates=calendar.open_dates,
        input_revision=input_revision,
        observed_at=observed_at,
        data_complete=data_complete,
    )
    previous = next((item for item in state.due_states if item.due_identity == due.due_identity), None)
    if previous is None:
        control.save_due_state(due)
    else:
        due = previous
    return HistoryTrainingDueEvaluation(
        archive_root,
        active,
        due,
        bundle,
        revised_dates,
        invalidated_dates,
    )


def _revised_dates_since_bundle(
    archive_root: Path,
    baseline: HistoryActiveSnapshot,
    active: HistoryActiveSnapshot,
    calendar_dates: tuple[date, ...],
    baseline_label_cutoff: date,
) -> tuple[date, ...]:
    start = calendar_dates[0]
    end = min(baseline_label_cutoff, baseline.data_cutoff, active.data_cutoff)
    if start > end:
        return ()
    baseline_months = {_partition_month(item.relative_path): item.sha256 for item in baseline.partitions}
    active_months = {_partition_month(item.relative_path): item.sha256 for item in active.partitions}
    archive = SQLiteHistoryMonthlyArchive(archive_root)
    revisions: set[date] = set()
    for year, month in route_history_months(start, end):
        if (year, month) not in baseline_months or (year, month) not in active_months:
            raise HistoryMonthlyArchiveError("history snapshot comparison month is missing")
        month_start = max(start, date(year, month, 1))
        month_end = min(end, date(year, month, month_calendar.monthrange(year, month)[1]))
        if month_start > month_end or baseline_months[(year, month)] == active_months[(year, month)]:
            continue
        before = {
            (item.trade_date, item.code): item.revision_id
            for item in archive.iter_range(month_start, month_end, baseline)
        }
        after = {
            (item.trade_date, item.code): item.revision_id
            for item in archive.iter_range(month_start, month_end, active)
        }
        revisions.update(
            day for day, code in set(before) | set(after) if before.get((day, code)) != after.get((day, code))
        )
    return tuple(sorted(revisions))


def _partition_month(relative_path: str) -> tuple[int, int]:
    parts = PurePosixPath(relative_path).parts
    return int(parts[1]), int(parts[2])


def _active_bundle(training_root: Path) -> tuple[ActiveTomorrowBundle | None, bool]:
    pointer = training_root / "tomorrow-v3" / "active-bundle.json"
    if not pointer.exists():
        return None, False
    try:
        return inspect_active_tomorrow_bundle(training_root / "tomorrow-v3"), False
    except (OSError, TypeError, ValueError):
        return None, True


__all__ = ["HistoryTrainingDueEvaluation", "evaluate_history_training_due"]
